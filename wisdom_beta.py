"""
wisdom_beta.py - Wisdom-Beta（wisdom-evolution.com）の教科書フォルダを保つ

マーケティングの知識サイト Wisdom-Beta の記事を、音声で聴く教材として
Obsidian の独立したフォルダ（C:\\Obsidian_Vault\\Wisdom-Beta\\）に置く。
News Checker の日次レポートとは混ぜない（Notion にも入れない）。

    python wisdom_beta.py daily            # 新着を確認して、あれば収集・解説・追加・目次更新
    python wisdom_beta.py daily --dry-run  # 新着を数えるだけ
    python wisdom_beta.py toc              # 目次だけ作り直す

毎朝タスクスケジューラ（wisdom_task.cmd）から呼ばれる。PC が消えていた日の分は、
次に動いたときにまとめて拾う（「まだ集めていない記事」を一覧から探すので日付に依存しない）。
解説の生成に失敗した記事は「記録済み・未解説」として残り、次回また解説を試みる。

2026-09-25 に全604件を作った経緯と注意点は、プロジェクト記憶の wisdom-beta-full-report にある。
"""

import html
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime

import main  # .env の読み込みのため
import gemini_client as g
import split_report as sp

SITE = "https://wisdom-evolution.com"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "wisdom_data")
DB = os.path.join(DATA_DIR, "articles.json")      # これまでに見た全記事（パス → 記録）
ROOT = r"C:\Obsidian_Vault\Wisdom-Beta"
NEW_DIR = os.path.join(ROOT, "新着")

MIN_BODY = 300          # これ未満は動画ページの紹介文など。解説しない
PER_CALL = 2            # 1回の生成に渡す本数（超詳細にするため少なく）
BODY_LIMIT = 6000
SPLIT_MINUTES = 60      # ユーザー指定：NotebookLM の最長で作る（原稿60分・約11件/本）
MAX_LIST_PAGES = 70     # 一覧を遡る上限（全62ページ、2026-09 時点）
SLEEP = 1.0             # サイトへの間隔（robots.txt は /test/ 以外許可）

FOOTERS = ("会員になると、既読やブックマーク（また読みたい記事）の管理ができます。",
           "まだ会員登録されていない方へ")
CODE = re.compile(r"^(\d+)-(\d+)-(\d+)")

PROMPT = """あなたは、マーケティングの実務と理論に精通した解説者です。
以下は、マーケティングの知識サイト「Wisdom-Beta」の記事で、「{section}」に属するものです。

{articles}

この{count}件について、音声で聴いて学ぶための解説を日本語で書いてください。
聴き手はこの内容を**超詳細に**学びたいと考えています。

■ 最重要：本文に書かれている中身を、要約しすぎずに具体的に語ること
各記事の本文が一次情報です。本文にある定義・手順・枠組み・事例・数字・固有名詞・
人名・書名・年号・注意点・例外を、できるだけ落とさずに話してください。
「〜について解説されています」のように中身に触れない書き方はしないこと。
本文に無いことを足さないこと。推測は「〜と考えられます」と明示すること。

■ 各記事の話し方
1. その記事が何を扱い、何のための知識なのか
2. 本文の中身を、書かれている順に沿って具体的に（ここがいちばん長くなってよい）
3. 事例があれば、誰が・いつ・何をして・どうなったかを数字つきで
4. 実務でどう使うか、使うときの注意点（本文に書かれている範囲で）

**1記事あたり25文から40文**。本文が短い記事は無理に伸ばさず、本文の分だけでよい。
同じことを言い換えて水増ししないこと。

■ 音声で聴くための書き方
箇条書きや記号（・、-、＊、#）は使わず、すべて話し言葉の文章で書くこと。
数字は「六十八パーセント」のように読み下すこと。URLは書かないこと。

■ 構成（必ず守ること）
冒頭に「ここからは{section}の話題です。この分野は{count}件あります。」と書く。
続けて1件ずつ解説する。各記事の冒頭には必ず通し番号を付ける。
「{start}件目。」から始め、1件ごとに1つずつ増やす。番号のあとに記事の題名を述べてから解説に入ること。
渡した記事を1件も飛ばさず、まとめて扱わず、必ず1件ずつ独立して解説すること。
全体のまとめは書かないこと。"""


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def load_db() -> dict:
    return json.load(open(DB, encoding="utf-8")) if os.path.exists(DB) else {}


def save_db(db: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DB + ".tmp"
    json.dump(db, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, DB)


# ---------------------------------------------------------------------------
# 収集
# ---------------------------------------------------------------------------

def arti_body(h: str) -> str:
    """このサイトの本文は <article class="arti-body ..."> の中にまとまっている。丸ごと取る"""
    m = re.search(r'<article[^>]*class="[^"]*arti-body[^"]*"[^>]*>(.*?)</article>', h, re.S)
    if not m:
        return ""
    x = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", "", m.group(1), flags=re.S)
    x = re.sub(r"<(br|/p|/li|/h[1-6]|/tr|/div)[^>]*>", "\n", x, flags=re.I)
    x = html.unescape(re.sub(r"<[^>]+>", "", x))
    lines = [re.sub(r"[ \t\u3000]+", " ", l).strip() for l in x.split("\n")]
    body = "\n".join(l for l in lines if l)
    for f in FOOTERS:
        if f in body:
            body = body[:body.index(f)].rstrip()
    return body


def section_of(title: str) -> str:
    m = CODE.match(title)
    return f"第{m.group(1)}章{m.group(2)}節" if m else "連載・その他の記事"


def fetch_article(path: str) -> dict:
    url = SITE + path
    raw, ct, fin = main._download_raw(url)
    if not raw:
        raise RuntimeError("取得できなかった（0バイト）")
    h = main._decode_html(raw, ct)
    title = main._extract_meta(h, [("property", "og:title")]) or \
        (re.search(r"<title[^>]*>(.*?)</title>", h, re.S) or [None, ""])[1]
    title = re.sub(r"\s*\|\s*Wisdom(-Beta)?\s*$", "", html.unescape(title).strip())
    body = arti_body(h) or main.extract_article_body(h)
    return {"url": url, "date": "/".join(path.split("/")[2:5]), "title": title,
            "body": body, "section": section_of(title), "skipped": len(body) < MIN_BODY}


def find_new_paths(db: dict) -> list:
    """一覧を新しい順に見ていき、既知の記事だけのページが出たら止める"""
    new = []
    for p in range(1, MAX_LIST_PAGES + 1):
        u = f"{SITE}/article/" + ("" if p == 1 else f"?page={p}")
        raw, ct, fin = main._download_raw(u)
        if not raw:
            raise RuntimeError(f"記事一覧を取得できなかった: {u}")
        paths = list(dict.fromkeys(re.findall(
            r'href=["\'](/article/\d{4}/\d{2}/\d{2}/\d+\.html)', main._decode_html(raw, ct))))
        if not paths:
            break
        fresh = [x for x in paths if x not in db and x not in new]
        new += fresh
        if not fresh:
            break
        time.sleep(SLEEP)
    return new


# ---------------------------------------------------------------------------
# 解説の生成
# ---------------------------------------------------------------------------

def generate(items: list, api_key: str) -> str:
    """items を節ごとに PER_CALL 本ずつ生成してつなげる（番号は1から。あとで振り直す）"""
    parts, n, i = [], 1, 0
    while i < len(items):
        sec = items[i]["section"]
        chunk = [items[i]]
        while len(chunk) < PER_CALL and i + len(chunk) < len(items) \
                and items[i + len(chunk)]["section"] == sec:
            chunk.append(items[i + len(chunk)])
        arts = "\n".join(
            f"「{a['title']}」（{a['date']} 公開）\n    本文: "
            f"{re.sub(r'\s+', ' ', a['body']).strip()[:BODY_LIMIT]}\n" for a in chunk)
        prompt = PROMPT.format(section=sec, count=len(chunk), start=n, articles=arts)
        for attempt in range(3):
            try:
                text = g._drop_repeated_section(
                    sec, g._call_gemini(prompt, api_key, use_search=False, max_tokens=16000))
                got = len(re.findall(r"^[0-9〇一二三四五六七八九十百]+(?:件目|番目)", text, re.M))
                if got >= len(chunk):
                    break
                log(f"  [再試行] 番号が足りない（{got}/{len(chunk)}）")
            except Exception as e:
                log(f"  [再試行] {type(e).__name__}: {str(e)[:80]}")
                time.sleep(10)
        else:
            raise RuntimeError(f"「{chunk[0]['title'][:30]}」ほかの解説を生成できなかった")
        parts.append(text)
        n += len(chunk)
        i += len(chunk)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 目次
# ---------------------------------------------------------------------------

def heading(v: dict) -> str:
    return f"新着（{v['added']}）" if v.get("added") else v["section"]


def write_toc(db: dict) -> None:
    items = sorted((v for v in db.values() if v.get("no")), key=lambda v: v["no"])
    skipped = sum(1 for v in db.values() if v.get("skipped"))
    counts = Counter(heading(v) for v in items)
    full = next((f for f in os.listdir(ROOT) if f.endswith("_音声用.md")), "")
    lines = ["---", 'title: "Wisdom-Beta 教科書 目次"', "type: 教科書", f"count: {len(items)}",
             f"updated: {datetime.now():%Y-%m-%d}", "---", "",
             "# Wisdom-Beta 教科書 目次", "",
             "マーケティングの知識サイト「Wisdom-Beta」（https://wisdom-evolution.com/）の記事を、",
             "章の順に1件ずつ解説した音声用の教材。NotebookLM にはパートごとに1ノートブックで入れる。", "",
             f"- 全{len(items)}件（本文が{MIN_BODY}字未満の動画ページなど{skipped}本は除外）",
             f"- 全文：[[{os.path.splitext(full)[0]}]]" if full else "- 全文：（なし）",
             "- NotebookLM の指示文：`C:\\Obsidian_Vault\\プロンプト\\Wisdom-Beta音声解説用プロンプト.md`",
             "- 新着記事は毎朝自動で `新着\\` フォルダに追加される（`wisdom_beta.py daily`）", ""]
    cur = None
    for v in items:
        h = heading(v)
        if h != cur:
            cur = h
            lines += ["", f"## {h}（{counts[h]}件）", ""]
        link = f"[[{v['file']}|{v.get('part_label', 'パート')}]]" if v.get("file") else "（未収録）"
        lines.append(f"- {v['no']}. {v['title']}（{v['date']}）— {link} ／ [元記事]({v['url']})")
    os.makedirs(ROOT, exist_ok=True)
    with open(os.path.join(ROOT, "00_目次.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 毎日の処理
# ---------------------------------------------------------------------------

def daily(dry_run: bool) -> int:
    db = load_db()
    if not db:
        log("[NG] 記事の記録（wisdom_data/articles.json）が無い。初期化されていない")
        return 1

    new_paths = find_new_paths(db)
    pending_before = [k for k, v in db.items() if not v.get("no") and not v.get("skipped")]
    log(f"新着 {len(new_paths)}本 / 前回からの持ち越し（記録済み・未解説） {len(pending_before)}本")
    if dry_run:
        for p in new_paths + pending_before:
            log(f"  {p}")
        return 0

    for p in reversed(new_paths):          # 古い順に処理する
        try:
            a = fetch_article(p)
        except Exception as e:
            log(f"  [取得失敗・次回また試す] {p}: {e}")
            continue
        db[p] = a
        log(f"  [{'除外' if a['skipped'] else '収集'}] 本文{len(a['body'])}字: {a['title'][:50]}")
        time.sleep(SLEEP)
    save_db(db)

    # 解説するのは「記録済み・番号なし・除外でない」もの。前回失敗した分もここで拾う
    pending = sorted((k for k, v in db.items() if not v.get("no") and not v.get("skipped")),
                     key=lambda k: db[k]["date"])
    if not pending:
        write_toc(db)
        log("[OK] 解説する新着なし")
        return 0

    items = [db[k] for k in pending]
    api_key = os.environ["GEMINI_API_KEY"].strip()
    text = g.clean_for_speech(generate(items, api_key))       # 番号は1から振られる
    check = g.verify_report(text, len(items))
    log(f"検算: {check}")
    if not check["ok"]:
        log("[NG] 検算が合わない。書き出さずに止める（記事は記録済み・未解説として残り、次回やり直す）")
        return 1

    start = max((v.get("no") or 0) for v in db.values()) + 1
    today = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(NEW_DIR, exist_ok=True)
    pieces = sp.balanced_split(text, SPLIT_MINUTES * sp.CHARS_PER_MINUTE)
    idx = 0
    for k, piece in enumerate(pieces, 1):
        cnt = len(sp.ARTICLE_RE.findall(piece))
        a_no, b_no = start + idx, start + idx + cnt - 1
        # 1から振られた番号を、教科書全体の通し番号に直す
        counter = [a_no - 1]

        def renum(m):
            counter[0] += 1
            return f"{m.group(1)}{counter[0]}件目"
        piece = re.sub(r"(\A|\n)\d+件目", renum, piece.strip())
        name = f"{today}_新着_{a_no}〜{b_no}件目" + (f"_{k}" if len(pieces) > 1 else "")
        header = (f"これはWisdom-Betaの新着記事、{a_no}件目から{b_no}件目までのパートです。"
                  f"番号順に、すべての記事を解説します。\n\n")
        with open(os.path.join(NEW_DIR, name + ".md"), "w", encoding="utf-8") as f:
            f.write(header + piece + "\n")
        for j in range(cnt):
            db[pending[idx + j]].update({"no": a_no + j, "file": name,
                                         "part_label": f"新着 {today}", "added": today})
        idx += cnt
        log(f"[OK] 新着\\{name}.md（{cnt}件・{len(piece):,}字・約{len(piece) // sp.CHARS_PER_MINUTE}分）")
    save_db(db)
    write_toc(db)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if cmd == "daily":
        sys.exit(daily("--dry-run" in sys.argv))
    if cmd == "toc":
        write_toc(load_db())
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
