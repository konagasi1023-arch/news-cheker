"""
domain_reports.py - 聞けていない期間の記事を「領域別」にまとめる（総括＋全件の詳報）

日次レポートは日付で切るが、何日も聴けていないときは領域ごとにまとめ直したほうが聴きやすい
（2026-11-07 ユーザー依頼：10/19 以降の486件を領域別に、なるべく詳細に）。

領域ごとに2種類の原稿を作る。
  総括 ... その領域の期間の流れ・数字・論点（テーマごと）。先に聴けば全体像がつかめる
  詳報 ... 全件を1件ずつ（日次と同じ詳しさ。SECTION_PROMPT をそのまま使う）。38分ごとに分割

    python domain_reports.py prepare [--since 時刻] [--create-pages]   # 取得して領域に振り分ける
    python domain_reports.py overview [--only 01,02]                    # 総括を書く
    python domain_reports.py detail   [--only 01,02]                    # 詳報を書く（途中から再開できる）
    python domain_reports.py finish   [--only 01,02]                    # 組み立て・照合・分割・保存・Notion

- 作業ファイルは domain_work/（git 管理外）。記事の本文は articles.json に保存するので、
  Notion を読み直さない。生成した塊は domain_work/chunks/ に残るので、止まっても続きから再開できる
- Notion のページは prepare --create-pages で**最初に**作る。ページの作成時刻が次の日次レポートの
  切り取り位置になるので、取得の直後に作れば、生成中に保存された記事を取りこぼさない
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

import main  # .env
import fact_check
import gemini_client as g
import notion_writer as nw
import run_report
import split_report

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "domain_work")
VAULT_REPORTS = "C:/Obsidian_Vault/News Checker/Reports"

# ---------------------------------------------------------------------------
# 領域の定義。保存済みのタグ・カテゴリ・題名で点をつけ、いちばん高い領域に入れる。
# タグは tag_vocab.json の語（分類のとき選ばせた語）。点は「タグ1つ＝重み」「カテゴリ＝加点」「題名の語＝1」
# ---------------------------------------------------------------------------
DOMAINS = [
    {"key": "01", "name": "広告運用・メディア・クリエイティブ",
     "tags": {"広告": 1, "動画広告": 1, "広告運用": 1.2, "電通": 1, "クリエイティブ": 1, "YouTube": 0.8, "TikTok": 0.8,
              "ターゲティング": 1, "プロモーション": 0.8, "販促": 0.8, "CTV": 1, "テレビ": 1, "媒体": 1, "博報堂": 1,
              "運用型広告": 1.2, "アドテク": 1.2, "計測": 1},
     "title": ["広告", "テレビ", "動画", "CM", "媒体", "電通", "博報堂", "運用型", "リーチ", "クリエイティブ"],
     "cats": {"マーケティング・広告": 0.6}},
    {"key": "02", "name": "ブランド・SNS・マーケティング戦略",
     "tags": {"ブランド": 1, "ブランディング": 1, "SNS": 1, "インフルエンサー": 1, "マーケティング": 0.7,
              "デジタルマーケティング": 0.7, "マーケティング戦略": 0.9, "顧客体験": 0.7, "ファン": 1, "CRM": 1,
              "コミュニケーション": 0.8, "ブランド戦略": 1.2, "ブランド体験": 1.2, "キャンペーン": 0.8},
     "title": ["ブランド", "SNS", "キャンペーン", "インフルエンサー", "ファン", "マーケティング", "コミュニケーション"],
     "cats": {"マーケティング・広告": 0.6}},
    {"key": "03", "name": "リテール・EC・決済",
     "tags": {"EC": 1, "リテール": 1, "リテールメディア": 1.5, "Amazon": 1, "決済": 1, "小売": 1, "店舗": 1, "流通": 1,
              "物流": 1, "ポイント": 1, "キャッシュレス": 1, "顧客体験": 0.7, "ショッピング": 1, "楽天": 1,
              "コンビニ": 1, "スーパー": 1, "ECサイト": 1, "オムニチャネル": 1},
     "title": ["小売", "EC", "決済", "売場", "楽天", "ドラッグストア", "コンビニ", "スーパー", "通販", "店舗", "リテール"],
     "cats": {}},
    {"key": "04", "name": "AI検索・SEO・Web集客",
     "tags": {"AI検索": 1.5, "SEO": 1.5, "GEO": 1.5, "AIO": 1.5, "検索": 1, "Web集客": 1, "コンテンツマーケティング": 1,
              "ウェブ": 0.7, "Webサイト": 1, "LLMO": 1.5},
     "title": ["SEO", "検索", "AIO", "GEO", "LLMO", "AI Overviews", "AI Mode"],
     "cats": {}},
    {"key": "05", "name": "AIモデル・プラットフォーム動向",
     "tags": {"OpenAI": 1, "Anthropic": 1, "Claude": 0.7, "Gemini": 1, "ChatGPT": 0.7, "LLM": 0.7, "AIモデル": 1.2,
              "Meta": 0.7, "NVIDIA": 1, "Microsoft": 1, "Google": 0.4, "GPT": 1, "生成AI": 0.3, "半導体": 1,
              "オープンソース": 0.5, "xAI": 1, "Apple": 0.7},
     "title": ["OpenAI", "Anthropic", "Claude", "Gemini", "GPT", "NVIDIA", "モデル", "Llama", "Grok"],
     "cats": {"AI業界動向": 1.2}},
    {"key": "06", "name": "AIエージェント・AI活用・自動化",
     "tags": {"AIエージェント": 1.2, "AI活用": 1, "自動化": 1, "AIツール": 1, "業務効率化": 1, "プロンプト": 1,
              "ワークフロー": 1, "MCP": 1, "Claude Code": 1, "DX": 0.7, "生産性": 1, "Copilot": 0.7, "コーディング": 1},
     "title": ["エージェント", "活用", "自動化", "ツール", "業務", "プロンプト", "Codex", "Cursor"],
     "cats": {"AIツール活用術": 2}},
    {"key": "07", "name": "セキュリティ・規制・技術基盤",
     "tags": {"セキュリティ": 1.5, "プライバシー": 1, "規制": 1.2, "著作権": 1.2, "API": 1, "音声認識": 1, "量子": 1.2,
              "ロボット": 1, "フィジカルAI": 1, "クラウド": 1, "データセンター": 1, "開発": 0.7, "サイバー": 1.5,
              "脆弱性": 1.5, "法": 0.7, "ガバナンス": 1},
     "title": ["セキュリティ", "攻撃", "漏えい", "漏洩", "規制", "著作権", "ロボット", "量子", "脆弱性", "API", "法"],
     "cats": {"AI開発・技術": 1.2}},
    {"key": "08", "name": "経営・新規事業・組織",
     "tags": {"経営戦略": 1, "新規事業": 1.2, "ビジネスモデル": 1, "意思決定": 1, "日本企業": 0.7, "組織": 1, "人材": 1,
              "経営": 1, "採用": 1, "M&A": 1.2, "資金調達": 1.2, "投資": 0.8, "スタートアップ": 1, "リーダーシップ": 1,
              "働き方": 1, "買収": 1.2, "提携": 1},
     "title": ["経営", "新規事業", "買収", "提携", "資金調達", "組織", "人材", "スタートアップ", "CEO", "社長"],
     "cats": {"経営・組織・人材": 2}},
    {"key": "09", "name": "調査・消費者データ",
     "tags": {"調査レポート": 1.2, "調査": 1, "消費者": 1, "データ分析": 1, "統計": 1, "市場規模": 1, "白書": 1,
              "Z世代": 1, "購買行動": 1, "消費者行動": 1, "世代": 0.7, "データ": 0.5},
     "title": ["調査", "調べ", "白書", "実態", "意識", "市場規模", "ランキング"],
     "cats": {"リサーチ・データ": 2}},
    {"key": "10", "name": "脳科学・心理・暮らし・その他",
     "tags": {"心理": 1, "脳科学": 1.5, "睡眠": 1, "健康": 1, "哲学": 1.5, "教育": 1, "ウェルビーイング": 1, "習慣": 1},
     "title": ["脳", "心理", "睡眠", "健康", "哲学", "習慣"],
     "cats": {"脳科学・心理・哲学": 3, "その他": 3, "": 1}},
]
FALLBACK_BY_CATEGORY = {"マーケティング・広告": "02", "AI業界動向": "05", "AI開発・技術": "07", "AIツール活用術": "06",
                        "経営・組織・人材": "08", "リサーチ・データ": "09", "脳科学・心理・哲学": "10", "その他": "10"}


def assign(a: dict) -> str:
    """記事を領域に入れる。点がいちばん高い領域（同点は番号の小さいほう）。点が無ければカテゴリで入れる"""
    best, best_score = None, 0.0
    tags = set(a.get("tags") or [])
    title = a.get("title") or ""
    for d in DOMAINS:
        score = sum(w for t, w in d["tags"].items() if t in tags)
        score += sum(0.8 for k in d["title"] if k in title)
        score += d["cats"].get(a.get("category") or "", 0)
        if score > best_score:
            best, best_score = d["key"], score
    return best or FALLBACK_BY_CATEGORY.get(a.get("category") or "", "10")


# ---------------------------------------------------------------------------
# 日付の読み下し・作業ファイル
# ---------------------------------------------------------------------------
def kan(n: int) -> str:
    d = "〇一二三四五六七八九"
    if n < 10:
        return d[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + d[n - 10]
    t, o = divmod(n, 10)
    return d[t] + "十" + (d[o] if o else "")


def jp_date(dt: datetime) -> str:
    return f"{kan(dt.month)}月{kan(dt.day)}日"


def jload(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def jsave(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def load_meta() -> dict:
    meta = jload(os.path.join(WORK, "meta.json"))
    if not meta:
        sys.exit("先に prepare を実行してください")
    return meta


def load_articles() -> dict:
    return {a["id"]: a for a in jload(os.path.join(WORK, "articles.json"), [])}


def domain_articles(meta: dict, arts: dict, key: str) -> list:
    ids = next(d["ids"] for d in meta["domains"] if d["key"] == key)
    return [arts[i] for i in ids]


def selected(meta: dict, only: str) -> list:
    keys = [k.strip() for k in only.split(",") if k.strip()] if only else [d["key"] for d in meta["domains"]]
    return [d for d in meta["domains"] if d["key"] in keys]


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------
def cmd_prepare(args) -> None:
    token, db = nw.get_credentials()
    if args.since:
        cutoff = args.since
    else:
        info = run_report.latest_report_time(token, db)
        if not info["ok"]:
            sys.exit(info["reason"])
        cutoff = info["cutoff"]
    cut_at = nw.parse_time(cutoff)
    print(f"切り取り: {cut_at.astimezone(nw.JST):%Y-%m-%d %H:%M}")
    t0 = datetime.now(nw.JST)
    arts = nw.fetch_recent_articles(token, db, 90, created_after=cutoff)
    failed = [a for a in arts if a.get("read_failed")]
    if failed:
        sys.exit(f"[中止] 本文を読めなかった記事が{len(failed)}件あります。時間をおいて再実行してください")
    added = run_report.load_report_additions()
    arts = [a for a in arts if (a.get("id") or "").replace("-", "") not in added]
    usable, _ = g.usable_articles(arts)
    kept = {id(a) for a in usable}
    for a in arts:
        if id(a) not in kept:
            print(f"  落とした: {(a.get('title') or '')[:40]} | {len(a.get('excerpt') or '')}字 | {(a.get('url') or '')[:50]}")
    print(f"対象 {len(usable)}件（取得 {len(arts)}件）")

    usable.sort(key=lambda a: a.get("created_time") or "")
    by = {d["key"]: [] for d in DOMAINS}
    for a in usable:
        by[assign(a)].append(a)
    times = [nw.parse_time(a["created_time"]) for a in usable if nw.parse_time(a.get("created_time"))]
    period = f"{jp_date(cut_at.astimezone(nw.JST))}から{jp_date(max(times).astimezone(nw.JST))}まで"
    short = f"{cut_at.astimezone(nw.JST):%m/%d}〜{max(times).astimezone(nw.JST):%m/%d}"
    print(f"期間: {period}\n")

    meta = {"t0": t0.isoformat(), "cutoff": cutoff, "period": period, "short": short, "count": len(usable),
            "date": t0.strftime("%Y-%m-%d"), "domains": []}
    for d in DOMAINS:
        items = by[d["key"]]
        meta["domains"].append({"key": d["key"], "name": d["name"], "ids": [a["id"] for a in items], "notion": ""})
        cats = {}
        for a in items:
            cats[a.get("category") or "未分類"] = cats.get(a.get("category") or "未分類", 0) + 1
        print(f"■ {d['key']} {d['name']}：{len(items)}件  （元のカテゴリ: "
              + "、".join(f"{k}{v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])) + "）")
        for a in items[:: max(1, len(items) // 5)][:5]:
            print(f"     ・{(a.get('title') or '')[:48]}  [{'/'.join((a.get('tags') or [])[:3])}]")

    if args.create_pages:
        # 取得の直後に作る（ページの作成時刻が、次の日次レポートの切り取り位置になる）
        for dm in meta["domains"]:
            if not dm["ids"]:
                continue
            title = f"📊 領域別まとめ {dm['key']} {dm['name']}（{len(dm['ids'])}件・{short}）"
            url = nw.save_report(title, "（作成中です）", token, db)
            dm["notion"] = nw.page_id_of(url)
            print(f"  Notion: {title} → {url}")
    jsave(os.path.join(WORK, "articles.json"), usable)
    jsave(os.path.join(WORK, "meta.json"), meta)
    print("\n作業ファイルを保存:", WORK)


# ---------------------------------------------------------------------------
# Gemini の呼び出し
# ---------------------------------------------------------------------------
SECTION_LABEL = "この期間に"


def call_model(prompt: str, use_search: bool, max_tokens: int, api_key: str) -> tuple:
    """Gemini を1回呼ぶ。flash が枠切れなら軽量モデルへ。(本文, 使ったモデル) を返す"""
    last = "?"
    for model in [g.GEMINI_MODEL] + g.CLASSIFY_MODELS:
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": {"temperature": 0.4, "maxOutputTokens": max_tokens}}
        if use_search and model == g.GEMINI_MODEL:
            payload["tools"] = [{"google_search": {}}]  # 検索は flash でしか使えない
        body = json.dumps(payload).encode("utf-8")
        for _ in range(2):
            try:
                req = urllib.request.Request(
                    f"{g.GEMINI_API_BASE}/{model}:generateContent?key={api_key}", data=body,
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=300) as r:
                    res = json.loads(r.read().decode("utf-8"))
                parts = res["candidates"][0]["content"].get("parts", [])
                text = "".join(p.get("text", "") for p in parts)
                if text.strip():
                    return text, model
                last = f"{model}: 空の応答"
                break
            except urllib.error.HTTPError as e:
                last = f"{model} HTTP {e.code}"
                if e.code == 429:
                    break  # 枠切れ。次のモデルへ
                time.sleep(15)
            except Exception as e:
                last = f"{model} {type(e).__name__}"
                time.sleep(15)
    raise RuntimeError(last)


# ---------------------------------------------------------------------------
# 総括
# ---------------------------------------------------------------------------
OVERVIEW_PROMPT = """あなたは、専門分野に精通したニュース解説者です。
以下は、ある読者が{period}に保存した「{domain}」領域の記事{count}件です。
各記事には、保存した日、題名、タグ、要約、本文抜粋が付いています。

{articles}

この領域の「期間のまとめ（総括）」を、音声で聴く日本語の解説として書いてください。
聴き手は、この期間のニュースをまだ1本も聴いていません。この領域で何が起きたのかが、これ1本でつかめることが目的です。

■ 構成
1. 冒頭に「ここからは、{domain}の領域の、{period}のまとめです。この期間に保存した記事は{count}件です。」と書く。
2. 記事を内容でテーマに分け、{themes}つ前後のテーマで語る。各テーマは次の順で、1テーマあたり1,200字から2,000字。
　・そのテーマで何が起きたか。企業名・製品名・人名・数字・日付・金額を具体的に述べる
　　（「調査によると」ではなく「993人を対象にした調査で、68パーセントが」のように）
　・複数の記事を並べて見えること。共通点、食い違い、時間の経過による変化。
　　「序盤は〜、中盤から〜、終盤には〜」のように、保存した日に触れて流れを語る
　・読者にとっての意味
3. 最後に「この領域で押さえておきたい点」を3つ、文章で述べる。

■ 守ること
・本文抜粋と要約に書かれた事実だけを材料にする。書かれていないことを足さない。推測は「〜と考えられます」と明示する。
・テーマの中心になった記事は、題名か企業名を言って紹介する。取り上げない記事があって構いません。
・「N件目」のような番号は書かない（個別の記事は別の詳報で扱います）。
・箇条書きや記号（・、-、＊、#）は使わず、自然な話し言葉で書く。見出しを記号で作らない。
　数字は「六十八パーセント」のように読み下す。URLは書かない。
・全体で{target}字前後。水増しのために同じことを言い換えない。"""


def fmt_for_overview(items: list) -> str:
    lines = []
    for a in items:
        d = nw.parse_time(a["created_time"]).astimezone(nw.JST)
        ex = re.sub(r"\s+", " ", a.get("excerpt") or "").strip()[:600]
        lines.append(f"【{d.month}/{d.day}】{(a.get('title') or '')[:100]}")
        if a.get("tags"):
            lines.append("  タグ: " + " / ".join(a["tags"][:4]))
        for s in (a.get("summary") or [])[:3]:
            lines.append("  要約: " + s)
        lines.append("  本文抜粋: " + ex)
        lines.append("")
    return "\n".join(lines)


def overview_params(n: int) -> tuple:
    target = max(5000, min(8500, 3500 + 40 * n))
    themes = 4 if n < 30 else 5 if n < 60 else 6 if n < 90 else 7
    return target, themes


def cmd_overview(args) -> None:
    meta = load_meta()
    arts = load_articles()
    api_key = os.environ["GEMINI_API_KEY"].strip()
    for dm in selected(meta, args.only):
        items = domain_articles(meta, arts, dm["key"])
        if not items:
            continue
        path = os.path.join(WORK, "overview", f"{dm['key']}.json")
        c = jload(path)
        if c and c.get("count") == len(items):
            print(f"[総括] {dm['key']} {dm['name']}: 済み（{len(c['text'])}字・{c['model']}）")
            continue
        target, themes = overview_params(len(items))
        prompt = OVERVIEW_PROMPT.format(period=meta["period"], domain=dm["name"], count=len(items), themes=themes,
                                        target=target, articles=fmt_for_overview(items)[:90000])
        best = ("", "")
        for attempt in range(2):
            try:
                raw, model = call_model(prompt, False, 30000, api_key)
            except Exception as e:
                print(f"[総括] {dm['key']} {dm['name']}: 失敗 {e}")
                continue
            text = g.clean_for_speech(raw)
            bad = re.search(r"(?m)^\d+件目", text)
            print(f"[総括] {dm['key']} {dm['name']}: {attempt + 1}回目 {len(text)}字（目安{target}字）{model}"
                  + ("　※番号が混ざった" if bad else ""))
            if len(text) > len(best[0]) and not bad:
                best = (text, model)
            if len(text) >= target * 0.6 and not bad:
                break
        if best[0]:
            jsave(path, {"count": len(items), "text": best[0], "model": best[1]})


# ---------------------------------------------------------------------------
# 詳報（全件を1件ずつ）
# ---------------------------------------------------------------------------
def min_len(a: dict) -> int:
    n = len(a.get("excerpt") or "")
    return 120 if n < 300 else 250 if n < 1000 else 330


def item_texts(out: str, n: int):
    """生成された文から、1件ずつの文章（先頭の番号を除く）を取り出す。n件そろっていなければ None"""
    m = re.search(r"(?m)^1件目", out)
    if not m:
        return None
    parts = [p.strip() for p in re.split(r"(?m)^(?=\d+件目)", out[m.start():]) if p.strip()]
    if len(parts) != n:
        return None
    texts = []
    for i, p in enumerate(parts, 1):
        if not p.startswith(f"{i}件目"):
            return None
        texts.append(re.sub(r"^\d+件目", "", p, count=1).strip())
    return texts


def gen_items(domain: str, items: list, start: int, api_key: str, cache_dir: str) -> tuple:
    """items を start 番から書かせる。件数がそろわなければ半分に割って頼み直す。(文章のリスト, モデルのリスト, 薄い項目)"""
    key = hashlib.md5("|".join(a["id"] for a in items).encode()).hexdigest()[:12]
    path = os.path.join(cache_dir, f"{key}.json")
    c = jload(path)
    if c:
        return c["texts"], [c["model"]] * len(c["texts"]), c.get("thin", [])
    prompt = g.SECTION_PROMPT.format(
        label=SECTION_LABEL, category=domain, count=len(items), start=start, end=start + len(items) - 1,
        articles=g._format_articles(items, with_excerpt=True)[:50000])
    texts, model = None, "?"
    try:
        raw, model = call_model(prompt, True, 60000, api_key)
        texts = item_texts(g.clean_for_speech(g._drop_repeated_section(domain, raw)), len(items))
    except Exception as e:
        print(f"  [{domain}] {start}〜{start + len(items) - 1}: 失敗 {e}")
    if texts is None:
        if len(items) == 1:
            raise RuntimeError(f"1件も書けなかった: {items[0].get('title', '')[:40]}")
        h = (len(items) + 1) // 2
        print(f"  [{domain}] {start}〜{start + len(items) - 1}: 件数がそろわないので {h}件ずつに割る")
        lt, lm, lth = gen_items(domain, items[:h], start, api_key, cache_dir)
        rt, rm, rth = gen_items(domain, items[h:], start + h, api_key, cache_dir)
        return lt + rt, lm + rm, lth + [h + i for i in rth]
    thin = [i for i, (t, a) in enumerate(zip(texts, items)) if len(t) < min_len(a)]
    jsave(path, {"texts": texts, "model": model, "thin": thin})
    print(f"  [{domain}] {start}〜{start + len(items) - 1}: 完了 {model}" + (f"（薄い {len(thin)}件）" if thin else ""))
    return texts, [model] * len(texts), thin


def chunks_of(items: list) -> list:
    n = g.MAX_ARTICLES_PER_CALL
    return [(i, items[i:i + n]) for i in range(0, len(items), n)]


def cmd_detail(args) -> None:
    from concurrent.futures import ThreadPoolExecutor
    meta = load_meta()
    arts = load_articles()
    api_key = os.environ["GEMINI_API_KEY"].strip()
    jobs = []
    for dm in selected(meta, args.only):
        items = domain_articles(meta, arts, dm["key"])
        for off, chunk in chunks_of(items):
            jobs.append((dm, off, chunk))
    print(f"詳報の生成: {len(jobs)}回分（3並列）")

    def run(job):
        dm, off, chunk = job
        try:
            gen_items(dm["name"], chunk, off + 1, api_key, os.path.join(WORK, "chunks", dm["key"]))
            return None
        except Exception as e:
            print(f"  [{dm['name']}] {off + 1}〜: 書けませんでした: {e}")
            return (dm["key"], off)

    with ThreadPoolExecutor(max_workers=3) as ex:
        failed = [r for r in ex.map(run, jobs) if r]
    print(f"\n完了。失敗 {len(failed)}件" + (f": {failed}（もう一度 detail を実行すると続きから再開します）" if failed else ""))


# ---------------------------------------------------------------------------
# 数字の機械チェック（漢数字で読み下した数が、元記事のどこかにあるか）
# ---------------------------------------------------------------------------
KDIG = {c: i for i, c in enumerate("〇一二三四五六七八九")}
KDIG["零"] = 0
KNUM_RE = re.compile(r"[〇零一二三四五六七八九十百千万億兆]+(?:[点・][〇一二三四五六七八九]+)?")


def _small(t: str) -> int:
    if not t:
        return 0
    if not re.search("[十百千]", t):
        return int("".join(str(KDIG[c]) for c in t))
    total, cur = 0, 0
    for c in t:
        if c in KDIG:
            cur = KDIG[c]
        else:
            mult = {"十": 10, "百": 100, "千": 1000}[c]
            total += (cur or 1) * mult
            cur = 0
    return total + cur


def kanji_value(s: str):
    """漢数字（小数の「点」「・」つき）の値。読めなければ None"""
    m = re.fullmatch(r"([〇零一二三四五六七八九十百千万億兆]+)(?:[点・]([〇一二三四五六七八九]+))?", s)
    if not m:
        return None
    body, frac = m.group(1), m.group(2)
    total = 0
    for unit, mult in (("兆", 10 ** 12), ("億", 10 ** 8), ("万", 10 ** 4)):
        if unit in body:
            head, body = body.split(unit, 1)
            total += (_small(head) if head else 1) * mult
    try:
        total += _small(body)
    except (KeyError, ValueError):
        return None
    return float(f"{total}.{''.join(str(KDIG[c]) for c in frac)}") if frac else float(total)


def source_numbers(text: str) -> set:
    """元記事に出てくる数の集合（算用数字・万億・billion/million・漢数字）"""
    import unicodedata
    t = unicodedata.normalize("NFKC", text or "")
    vals = set()
    unit = {"兆": 1e12, "億": 1e8, "万": 1e4, "千": 1e3, "百": 1e2}
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(兆|億|万|千|百)?", t):
        v = float(m.group(1).replace(",", ""))
        vals.add(round(v, 6))
        if m.group(2):
            vals.add(round(v * unit[m.group(2)], 6))
    en = {"trillion": 1e12, "billion": 1e9, "million": 1e6, "thousand": 1e3, "bn": 1e9, "t": 1e12, "b": 1e9, "m": 1e6, "k": 1e3}
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(trillion|billion|million|thousand|bn|[TBMK])\b", t, re.I):
        vals.add(round(float(m.group(1).replace(",", "")) * en[m.group(2).lower()], 6))
    for m in KNUM_RE.finditer(t):
        v = kanji_value(m.group(0))
        if v is not None:
            vals.add(round(v, 6))
    return vals


def unmatched_numbers(text: str, source: str, min_value: float = 11) -> list:
    """text の漢数字のうち、元記事に同じ値が無いもの（（文脈つき）のリスト）"""
    src = source_numbers(source)
    out = []
    for m in KNUM_RE.finditer(text):
        v = kanji_value(m.group(0))
        if v is None or (v < min_value and v == int(v)):
            continue
        if round(v, 6) not in src:
            out.append(text[max(0, m.start() - 12):m.end() + 8].replace("\n", " "))
    return out


# ---------------------------------------------------------------------------
# finish（組み立て・照合・分割・保存・Notion）
# ---------------------------------------------------------------------------
def build_detail(meta: dict, dm: dict, items: list) -> tuple:
    texts, models, thin = [], [], []
    for off, chunk in chunks_of(items):
        key = hashlib.md5("|".join(a["id"] for a in chunk).encode()).hexdigest()[:12]
        c = jload(os.path.join(WORK, "chunks", dm["key"], f"{key}.json"))
        if c is None:
            # 割って頼み直した塊は、半分ずつの塊として残っている
            sub = []
            h = (len(chunk) + 1) // 2
            for part in (chunk[:h], chunk[h:]):
                k2 = hashlib.md5("|".join(a["id"] for a in part).encode()).hexdigest()[:12]
                sub.append(jload(os.path.join(WORK, "chunks", dm["key"], f"{k2}.json")))
            if any(s is None for s in sub):
                return None, None, None
            c = {"texts": sub[0]["texts"] + sub[1]["texts"], "model": sub[0]["model"] + "/" + sub[1]["model"],
                 "thin": sub[0].get("thin", []) + [h + i for i in sub[1].get("thin", [])]}
        thin += [off + i for i in c.get("thin", [])]
        texts += c["texts"]
        models += [c["model"]] * len(c["texts"])
    opening = (f"ここからは、{dm['name']}の領域の、{meta['period']}の詳報です。"
               f"この領域の記事は全部で{len(items)}件あります。1件ずつ、順番に解説します。")
    body = "\n\n".join(f"{i}件目{t}" for i, t in enumerate(texts, 1))
    return g.clean_for_speech(opening + "\n\n" + body), models, thin


def write_parts(outdir: str, dm: dict, text: str) -> list:
    """詳報を38分ごとに分けて書く（ファイル名に領域を入れる）"""
    parts = split_report.balanced_split(text, split_report.DEFAULT_MAX_MINUTES * split_report.CHARS_PER_MINUTE)
    if os.path.isdir(outdir):
        import shutil
        shutil.rmtree(outdir)
    os.makedirs(outdir)
    total = len(split_report.ARTICLE_RE.findall(text))
    seen, names = 0, []
    for i, part in enumerate(parts, 1):
        count = len(split_report.ARTICLE_RE.findall(part))
        first, last = seen + 1, seen + count
        seen = last
        header = (f"これは「{dm['name']}」の領域の詳報、全{total}件のうち{first}件目から{last}件目までのパートです。"
                  f"番号順に、すべての記事を解説します。\n\n")
        name = f"{dm['key']}_詳報_パート{i}_{first}〜{last}件目.md"
        with open(os.path.join(outdir, name), "w", encoding="utf-8") as f:
            f.write(header + part + "\n")
        names.append((name, count, len(header + part)))
    return names


def apply_manual_fixes(key: str, detail: str, overview: str) -> tuple:
    """元記事と突き合わせて確定した直しを当てる（domain_work/manual_fixes.json）。
    no=0 は総括、それ以外は詳報のその番号の項目の中だけを置き換える。
    finish は自動修正をやり直すので、人が確かめた直しはここで必ず上書きする。"""
    fixes = [x for x in jload(os.path.join(WORK, "manual_fixes.json"), []) if x["key"] == key]
    for x in fixes:
        if x["no"] == 0:
            if x["old"] in overview:
                overview = overview.replace(x["old"], x["new"])
            else:
                print(f"  [手直し] {key} 総括に見当たらず: {x['old']}")
            continue
        seg = {n: (s, e) for n, s, e in fact_check.split_items(detail)}.get(x["no"])
        if not seg or x["old"] not in detail[seg[0]:seg[1]]:
            print(f"  [手直し] {key} {x['no']}件目に見当たらず: {x['old']}")
            continue
        s, e = seg
        detail = detail[:s] + detail[s:e].replace(x["old"], x["new"]) + detail[e:]
    return detail, overview


def cmd_finish(args) -> None:
    meta = load_meta()
    arts = load_articles()
    token, db = nw.get_credentials()
    outroot = os.path.join(VAULT_REPORTS, f"領域別まとめ_{meta['date']}")
    os.makedirs(outroot, exist_ok=True)
    summary = jload(os.path.join(WORK, "finish.json"), {})
    for dm in selected(meta, args.only):
        items = domain_articles(meta, arts, dm["key"])
        if not items:
            continue
        ov = jload(os.path.join(WORK, "overview", f"{dm['key']}.json"))
        if ov:
            # 区切り線（---）が「--」の行で残ると音声で読まれるので取り除く
            ov["text"] = re.sub(r"(?m)^[ \t]*-{2,}[ \t]*\n?", "", ov["text"]).strip()
        detail, models, thin = build_detail(meta, dm, items)
        if ov is None or detail is None:
            print(f"[{dm['key']} {dm['name']}] 総括か詳報が未完了です（overview / detail を先に）")
            continue
        check = g.verify_report(detail, len(items))
        if not (check["numbers"] == len(items) and check["sequential"]):
            print(f"[{dm['key']} {dm['name']}] 番号が合いません: {check}")
            continue
        # 照合：元記事の本文と照らす（並びは書かせた順＝items の順）
        res = fact_check.check(detail, items, items)
        detail, fixed, held = fact_check.apply_typos(detail, res)
        detail, ov["text"] = apply_manual_fixes(dm["key"], detail, ov["text"])
        by_no = {r["no"]: r for r in res}
        excerpts = {i + 1: a.get("excerpt") or "" for i, a in enumerate(items)}
        real = [h for h in held if not (KNUM_RE.search(h["wrong"]) and
                                        all(round(kanji_value(m.group(0)) or -1, 6) in source_numbers(excerpts[h["no"]])
                                            for m in KNUM_RE.finditer(h["wrong"])))]
        ov_missing = unmatched_numbers(ov["text"], "\n".join(excerpts.values()))
        stem = f"{dm['key']}_{dm['name']}"
        with open(os.path.join(outroot, f"{stem}_総括_音声用.md"), "w", encoding="utf-8") as f:
            f.write(ov["text"].strip() + "\n")
        with open(os.path.join(outroot, f"{stem}_詳報_音声用.md"), "w", encoding="utf-8") as f:
            f.write(detail.strip() + "\n")
        parts = write_parts(os.path.join(outroot, f"{stem}_詳報_分割"), dm, detail)
        fact_check.write_note(os.path.join(outroot, f"照合_{stem}.md"), meta["date"], res, fixed, held)
        with open(os.path.join(outroot, f"照合_{stem}.md"), "a", encoding="utf-8") as f:
            f.write("\n## 機械チェック（漢数字で読み下した数が、元記事に無いもの）\n\n")
            f.write("### 詳報の「疑い」のうち、元記事に同じ値が無いもの（要確認）\n\n")
            f.write("\n".join(f"- {h['no']}件目：「{h['wrong']}」→ 本文では「{h['right']}」" for h in real) or "- なし")
            f.write("\n\n### 総括の数字で、元記事に同じ値が無いもの（要確認）\n\n")
            f.write("\n".join(f"- …{x}…" for x in ov_missing) or "- なし")
            f.write("\n\n## 薄い項目（元の本文に対して短い）\n\n")
            f.write("\n".join(f"- {i + 1}件目 {items[i].get('title', '')[:40]}" for i in thin) or "- なし")
            f.write("\n")
        # Notion：総括＋詳報を1ページに
        notion_ok = False
        if dm.get("notion"):
            nw.replace_report_body(token, dm["notion"], ov["text"].strip() + "\n\n" + detail.strip())
            back = g.verify_report(nw.read_report_text(token, dm["notion"]), len(items))
            notion_ok = back["numbers"] == len(items) and back["sequential"]
            if not notion_ok:
                print(f"  [Notion] 読み戻しの検算が合いません: {back}")
        minutes = (len(ov["text"]) + len(detail)) // split_report.CHARS_PER_MINUTE
        summary[dm["key"]] = {"name": dm["name"], "count": len(items), "overview_chars": len(ov["text"]),
                              "detail_chars": len(detail), "parts": len(parts), "minutes": minutes,
                              "fixed": len(fixed), "doubts": len(held), "real_doubts": len(real),
                              "ov_numbers": len(ov_missing), "thin": len(thin), "notion": notion_ok,
                              "models": {m: models.count(m) for m in set(models)}}
        print(f"[{dm['key']} {dm['name']}] {len(items)}件・総括{len(ov['text'])}字・詳報{len(detail)}字（{len(parts)}パート）"
              f"｜自動修正{len(fixed)}・疑い{len(held)}→要確認{len(real)}・総括の数字{len(ov_missing)}・薄い{len(thin)}"
              f"｜Notion {'済' if notion_ok else '未'}")
    jsave(os.path.join(WORK, "finish.json"), summary)
    write_index(meta, summary, outroot)


def write_index(meta: dict, summary: dict, outroot: str) -> None:
    lines = ["---", f'title: "領域別まとめ {meta["date"]}"', "---", "",
             f"# 領域別まとめ（{meta['short']}・{meta['count']}件）", "",
             "聴けていなかった期間の記事を、領域ごとにまとめ直したもの。各領域に「総括」と「詳報」がある。", "",
             "**聴き方**：まず各領域の「総括」だけを聴いて期間の全体をつかみ、気になる領域の「詳報」（全件を1件ずつ）に進む。",
             "詳報は38分ごとに分けてある（1パートを1つのノートブックに入れる）。", "",
             "| 領域 | 件数 | 総括（NotebookLM に入れるファイル） | 詳報（フォルダ） | Notion |", "|---|---|---|---|---|"]
    for dm in meta["domains"]:
        s = summary.get(dm["key"])
        if not s:
            continue
        stem = f"{dm['key']}_{dm['name']}"
        lines.append(f"| {dm['name']} | {s['count']} | `{stem}_総括_音声用.md`（約{s['overview_chars'] // 350}分） | "
                     f"`{stem}_詳報_分割`（{s['parts']}パート・約{s['detail_chars'] // 350}分） | {'済' if s['notion'] else '未'} |")
    lines += ["", "照合の結果は、領域ごとの `照合_…md`。"]
    with open(os.path.join(outroot, "00_索引.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def cmd_status(args) -> None:
    meta = load_meta()
    arts = load_articles()
    for dm in meta["domains"]:
        items = domain_articles(meta, arts, dm["key"])
        ov = jload(os.path.join(WORK, "overview", f"{dm['key']}.json"))
        have = 0
        for off, chunk in chunks_of(items):
            key = hashlib.md5("|".join(a["id"] for a in chunk).encode()).hexdigest()[:12]
            h = (len(chunk) + 1) // 2
            ks = [hashlib.md5("|".join(a["id"] for a in p).encode()).hexdigest()[:12] for p in (chunk[:h], chunk[h:])]
            d = os.path.join(WORK, "chunks", dm["key"])
            if os.path.exists(os.path.join(d, f"{key}.json")) or all(os.path.exists(os.path.join(d, f"{k}.json")) for k in ks):
                have += 1
        total = len(chunks_of(items))
        print(f"{dm['key']} {dm['name']}：{len(items)}件｜総括 {'済' if ov else '未'}｜詳報 {have}/{total}塊｜Notion {'有' if dm.get('notion') else '無'}")


def main_cli() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="聞けていない期間の記事を領域別にまとめる")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("prepare")
    s.add_argument("--since", default="")
    s.add_argument("--create-pages", action="store_true")
    for name in ("overview", "detail", "finish", "status"):
        s = sub.add_parser(name)
        s.add_argument("--only", default="")
    args = p.parse_args()
    {"prepare": cmd_prepare, "overview": cmd_overview, "detail": cmd_detail, "finish": cmd_finish,
     "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main_cli()
