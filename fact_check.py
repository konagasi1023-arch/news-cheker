"""
fact_check.py - 音声用レポートの原稿を、元記事の本文と照合する

生成した解説に、元記事と食い違う固有名詞・数字が混ざることがある
（2026-09-26「Google」が「グーデ」、2026-09-25「西口一希」が「西口和克」）。
これまでは点検のときに目で見つけていた。ここで機械的に見つけ、明らかな誤記は直す。

- 照合は軽量モデル（CLASSIFY_MODELS・無料枠1日500回）に、4件ずつまとめて見せる
- 解説は英語をカタカナで、数字を漢数字で読み下す。文字列の単純な照合では誤判定だらけになるので、
  モデルに「表記ゆれは食い違いではない」と言って意味で照合させる
- 明らかな誤記（本文に対応する正しい語がある）は自動で置き換える
- 本文に根拠が見当たらない語は置き換えない（レポートは Web検索で補った情報も含むため）。
  一覧を vault に書き出して、点検で確かめる

    python fact_check.py <レポートのパス> <切り取り時刻>   # 単体で試す（書き換えない）
    ※ 切り取り時刻より後に保存された記事があると並びがずれ、題名での照合に切り替わる
"""

import difflib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import gemini_client as g

BATCH = 4
EXCERPT_LIMIT = 3000
ITEM_RE = re.compile(r"^(\d+)件目", re.M)

PROMPT = """以下は、ニュース記事の本文（一次情報）と、それをもとに書かれた音声解説の組です。
各組について、**解説の中に、本文と食い違う固有名詞・数字・日付**が無いか調べてください。

{pairs}

■ 食い違いとして扱わないもの
- 表記ゆれ：英語の名前のカタカナ表記（Google→グーグル、Snowflake→スノーフレイク）、
  算用数字の読み下し（68%→六十八パーセント、2026年→二〇二六年／二千二十六年）、全角半角の違い
- 一般的な語（AI、マーケティング、企業、顧客など）
- 解説者の意見・推測（「〜と考えられます」）

■ 挙げるもの
1. typos：本文に対応する正しい語がはっきりある**誤記**。例：本文が Google なのに解説が「グーデ」。
   wrong には解説中の誤った語を**解説の表記のまま一字一句**、right には本文に基づく正しい語（解説の表記に合わせてカタカナでよい）
2. unsupported：本文のどこにも根拠が無い固有名詞・数字（解説の表記のまま）。
   本文が短く、解説が外部の知識で補っている場合もある。確実に本文に無いものだけを挙げる

JSON だけを出力：
{{"items": [{{"no": 番号, "typos": [{{"wrong": "...", "right": "..."}}], "unsupported": ["..."]}}]}}
食い違いが無い組は typos と unsupported を空の配列にする。値に二重引用符を使わないこと。"""


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\s\"'「」『』、。・！？：|｜()（）\[\]【】]", "", s).lower()


def split_items(text: str) -> list:
    """原稿を「N件目」ごとに切る。返り値は (番号, 開始, 終了)"""
    starts = [(int(m.group(1)), m.start()) for m in ITEM_RE.finditer(text)]
    out = []
    for i, (no, s) in enumerate(starts):
        e = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        # 次の区切りの見出しや締めくくりの手前で止める
        head = re.search(r"\n\n(ここからは.{2,20}?の話題です。|以上に加えて|さて、)", text[s:e])
        out.append((no, s, s + head.start() if head else e))
    return out


def match_article(item_text: str, articles: list, with_score: bool = False):
    """解説の冒頭（題名を述べる部分）と記事の題名を照らして、元記事を決める"""
    head = _fold(item_text[:200])
    best, score = None, 0.0
    for a in articles:
        t = _fold(a.get("title", ""))[:40]
        if not t:
            continue
        r = 1.0 if t[:20] and t[:20] in head else difflib.SequenceMatcher(None, t, head[:80]).ratio()
        if r > score:
            best, score = a, r
    if score < 0.45:
        best = None
    return (best, score) if with_score else best


def _ask(prompt: str, api_key: str) -> dict:
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 4000,
                             "responseMimeType": "application/json"},
    }).encode("utf-8")
    last = ""
    for model in g.CLASSIFY_MODELS:
        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    f"{g.GEMINI_API_BASE}/{model}:generateContent?key={api_key}",
                    data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=90) as r:
                    res = json.loads(r.read().decode("utf-8"))
                txt = "".join(p.get("text", "") for p in res["candidates"][0]["content"]["parts"])
                data = json.loads(txt)
                if isinstance(data, list):
                    data = {"items": data}
                return data
            except urllib.error.HTTPError as e:
                last = f"{model} HTTP {e.code}"
                if e.code == 429:
                    break
                time.sleep(3)
            except Exception as e:
                last = f"{model} {type(e).__name__}"
                time.sleep(2)
    raise RuntimeError(f"照合できなかった: {last}")


def check(text: str, articles: list, ordered: list = None) -> list:
    """
    原稿全体を照合し、組ごとの結果を返す。
    ordered（gemini_client.report_order の並び＝n番目がn件目）を渡せば、それで元記事を決める。
    件数が合わないときだけ、題名で探す（英語の記事は題名が訳されていて見つからないことがある）。
    返り値: [{"no", "title", "typos": [...], "unsupported": [...], "matched": bool}]
    """
    api_key = os.environ["GEMINI_API_KEY"].strip()
    spans = split_items(text)
    by_order = ordered is not None and len(ordered) == len(spans)
    if ordered is not None and not by_order:
        print(f"  [照合] 記事数（{len(ordered)}）と番号の数（{len(spans)}）が違うので、題名で元記事を探す")
    items = []
    for i, (no, s, e) in enumerate(spans):
        m, score = match_article(text[s:e], articles, with_score=True)
        a = ordered[i] if by_order else m
        # モデルが同じ呼び出しの中で2件の順番を入れ替えて書くことがある（2026-09-26・59件目と60件目）。
        # 題名がはっきり別の記事を指していれば、題名のほうを信じる
        if by_order and m is not None and m is not a and score >= 0.9:
            a = m
        items.append({"no": no, "start": s, "end": e, "article": a})
    pairs = [x for x in items if x["article"] and (x["article"].get("excerpt") or "").strip()]

    def run(batch):
        blocks = []
        for x in batch:
            ex = re.sub(r"\s+", " ", x["article"]["excerpt"]).strip()[:EXCERPT_LIMIT]
            blocks.append(f"【組 {x['no']}】\n本文: {ex}\n解説: {text[x['start']:x['end']].strip()}\n")
        try:
            return _ask(PROMPT.format(pairs="\n".join(blocks)), api_key).get("items", [])
        except Exception as e:
            print(f"  [照合できず] {batch[0]['no']}〜{batch[-1]['no']}件目: {e}")
            return []

    batches = [pairs[i:i + BATCH] for i in range(0, len(pairs), BATCH)]
    with ThreadPoolExecutor(max_workers=3) as ex:
        answers = [r for rs in ex.map(run, batches) for r in rs]
    by_no = {}
    for r in answers:
        try:
            by_no[int(r.get("no"))] = r
        except Exception:
            continue
    out = []
    for x in items:
        r = by_no.get(x["no"], {})
        out.append({"no": x["no"], "title": (x["article"] or {}).get("title", ""),
                    "matched": bool(x["article"]), "checked": x["no"] in by_no,
                    "typos": [t for t in r.get("typos") or [] if isinstance(t, dict)],
                    "unsupported": [u for u in r.get("unsupported") or [] if isinstance(u, str)],
                    "start": x["start"], "end": x["end"],
                    "excerpt": ((x["article"] or {}).get("excerpt") or "")[:EXCERPT_LIMIT]})
    return out


_ASCII = re.compile(r"[A-Za-z0-9]")
_LATIN = re.compile(r"[A-Za-z]")
_NUM = re.compile(r"[0-9〇一二三四五六七八九十百千万億兆]")
_KATA = re.compile(r"^[ァ-ヶー・]+$")


def _auto_ok(wrong: str, right: str, excerpt: str) -> str:
    """
    自動で直してよい誤記かを判定する。直さない理由を返す（直してよければ空文字）。
    2026-09-26 の試験で、モデルの訂正には次の誤りが混ざった：
    - 読み下しを英数字に戻す（二千二十六年→2026年、千百八十ピクセル→1080p）。音声用の表記が崩れる
    - 数字を丸めた言い回しを書き換える（四分の割→ほぼ四分の一、四十→四十一）
    数字の食い違いは本物（一万→100万トークン）でも読み方を決めきれないので、点検に回す。
    """
    if _ASCII.search(right) and not _ASCII.search(wrong):
        return "訂正案が英数字"
    if _NUM.search(wrong):
        # 読み下しに算用数字が混ざったもの（二〇19年→二〇一九年）だけは表記の統一として直す
        if re.search(r"[0-9]", wrong) and not re.search(r"[0-9]", right):
            return ""
        return "数字"
    if _LATIN.search(wrong):
        return ""  # 生成の崩れ（イvery）
    if _fold(right) in _fold(excerpt):
        return ""
    if _KATA.match(wrong) and _KATA.match(right) and len(right) <= 16:
        return ""  # カタカナ語の言い違い（グーデ→グーグル）
    if len(right) <= len(wrong) + 4 and difflib.SequenceMatcher(None, wrong, right).ratio() >= 0.5:
        return ""
    return "言い換え"


def apply_typos(text: str, results: list) -> tuple:
    """
    明らかな誤記を、その記事の解説の範囲だけで置き換える。後ろから直すので位置はずれない。
    返り値: (直した原稿, 直した一覧, 直さず点検に回した一覧)
    """
    fixed, held = [], []
    for r in sorted(results, key=lambda r: -r["start"]):
        seg = text[r["start"]:r["end"]]
        for t in r["typos"]:
            wrong, right = (t.get("wrong") or "").strip(), (t.get("right") or "").strip()
            if not wrong or not right or wrong == right or len(wrong) > 30 or len(right) > 40:
                continue
            if wrong not in seg or wrong in right:
                continue
            why = _auto_ok(wrong, right, r.get("excerpt", ""))
            if why:
                held.append({"no": r["no"], "wrong": wrong, "right": right, "why": why})
                continue
            n = seg.count(wrong)
            seg = seg.replace(wrong, right)
            fixed.append({"no": r["no"], "wrong": wrong, "right": right, "count": n})
        text = text[:r["start"]] + seg + text[r["end"]:]
    key = lambda f: f["no"]
    return text, sorted(fixed, key=key), sorted(held, key=key)


def write_note(path: str, date: str, results: list, fixed: list, held: list = ()) -> None:
    """照合の結果を vault に書く（点検で見る）"""
    unsup = [r for r in results if r["unsupported"]]
    unchecked = [r for r in results if not r["checked"]]
    lines = ["---", f'title: "照合結果 {date}"', f"fixed: {len(fixed)}",
             f"unsupported_items: {len(unsup)}", "---", "", f"# 原稿の照合結果 {date}", "",
             "音声用レポートの解説を、元記事の本文と照らした結果。", "",
             f"## 自動で直した誤記（{len(fixed)}件）", ""]
    lines += [f"- {f['no']}件目：「{f['wrong']}」→「{f['right']}」（{f['count']}か所）" for f in fixed] or ["- なし"]
    lines += ["", f"## 食い違いの疑い・自動では直さなかったもの（{len(held)}件）", "",
              "数字や読み方が絡むので、原稿を見て判断する。", ""]
    lines += [f"- {h['no']}件目：「{h['wrong']}」→ 本文では「{h['right']}」（{h['why']}）" for h in held] or ["- なし"]
    lines += ["", f"## 本文に根拠が見当たらない語（{len(unsup)}件の記事）", "",
              "Web検索で補った情報の場合もあるので、自動では消していない。", ""]
    lines += [f"- {r['no']}件目 {r['title'][:40]}：{'、'.join(r['unsupported'][:8])}" for r in unsup] or ["- なし"]
    if unchecked:
        lines += ["", f"## 照合できなかった記事（{len(unchecked)}件）", ""]
        lines += [f"- {r['no']}件目" + ("（元記事を特定できず）" if not r["matched"] else "") for r in unchecked]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    import main  # .env
    import notion_writer as nw
    report, cutoff = sys.argv[1], sys.argv[2]
    token, db = nw.get_credentials()
    arts, _ = g.usable_articles(nw.fetch_recent_articles(token, db, 14, created_after=cutoff))
    text = open(report, encoding="utf-8").read()
    t0 = time.time()
    res = check(text, arts, g.report_order(arts))
    _, fixed, held = apply_typos(text, res)
    print(f"照合 {sum(r['checked'] for r in res)}/{len(res)}件（{time.time() - t0:.0f}秒）"
          f" / 元記事を特定できず {sum(not r['matched'] for r in res)}件")
    print("直す:", [(f["no"], f["wrong"], f["right"]) for f in fixed])
    print("直さない:", [(h["no"], h["wrong"], h["right"], h["why"]) for h in held])
    for r in res:
        if r["unsupported"]:
            print(f"  根拠なし {r['no']}件目: {r['unsupported'][:6]}")
