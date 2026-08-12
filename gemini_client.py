"""
gemini_client.py - Gemini 2.5 Flash + Google Search Grounding クライアント

入力タイプ:
  {"url": "https://..."}                         → URL分析
  {"content": "記事本文テキスト..."}               → テキスト分析
  {"image_b64": "base64...", "mime_type": "..."}  → 画像分析

環境変数:
  GEMINI_API_KEY - Google AI Studio の APIキー
"""

import json
import os
import re
import time
import urllib.request
import urllib.error
import datetime

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MODEL = "gemini-2.5-flash"

PROMPT_SUFFIX = """
あなたはあらゆる分野の第一線で活躍する専門家集団です。
以下のコンテンツ（URL・記事・画像）のテーマを正確に判定し、
そのテーマに最もふさわしい専門家の立場から、内容を日本語で解説してください。

まず最初に、以下の【3行要約】を出力してください：

━━━━━━━━━━━━━━━━━━━━
【3行要約】
━━━━━━━━━━━━━━━━━━━━
1.（このコンテンツの最重要ポイントを1文で）
2.（この分野の専門家として見た、最も注目すべき含意を1文で）
3.（読者が明日から取れる具体的アクションを1文で）

続けて、以下の構成で詳細分析を出力してください：

あなたはあらゆる分野の第一線で活躍する専門家集団です。
以下のコンテンツ（URL・記事・画像）のテーマを正確に判定し、
そのテーマに最もふさわしい専門家の立場から、内容を日本語で解説してください。

【専門家の判定基準（例）】
- 生成AI・LLM・テクノロジー         → AIエンジニア / 研究者 / CTO経験者
- マーケティング・広告・ブランド       → CMO経験者 / ブランド戦略コンサルタント
- 事業開発・スタートアップ・資金調達   → VC・M&Aアドバイザー / 連続起業家
- 経営・戦略・組織                  → 経営コンサルタント / 元事業会社CEO
- 消費者行動・リサーチ・データ        → 行動経済学者 / シニアアナリスト
- その他のテーマ                    → そのテーマの世界水準の専門家を自律的に選定すること

検索機能を使い、コンテンツに関連する最新情報・背景を補完したうえで、
以下の構成で出力してください：

━━━━━━━━━━━━━━━━━━━━
【専門家ポジション】
━━━━━━━━━━━━━━━━━━━━
（このコンテンツのテーマに対して、どんな専門家として回答するかを1行で明示する）

━━━━━━━━━━━━━━━━━━━━
【コンテンツ解説】
━━━━━━━━━━━━━━━━━━━━
（専門家の視点で、記事・画像の内容を正確かつ深く解説する。
 画像の場合はグラフ・数値・キャプチャ内のテキストも読み取って解説すること。
 一般読者が見落とす技術的・業界的な背景・文脈・用語も含め、3〜5段落で記述する）

━━━━━━━━━━━━━━━━━━━━
【熟練者だけが気づく洞察】
━━━━━━━━━━━━━━━━━━━━
・（一般読者がスルーするが、専門家には重要なポイントとその理由）
・（業界の常識・慣習と対比して見えてくること）
・（この情報が示す中長期的な意味・影響）

━━━━━━━━━━━━━━━━━━━━
【最新動向（検索補完）】
━━━━━━━━━━━━━━━━━━━━
（Google検索で確認した、このコンテンツに関連する最新の補足情報を2〜3点）

━━━━━━━━━━━━━━━━━━━━
【実務への示唆】
━━━━━━━━━━━━━━━━━━━━
（この専門家として、読者が次に取るべき具体的なアクションを2〜3点）
"""


def build_parts(payload: dict) -> list:
    """
    入力タイプ（url / content / image_b64）に応じてGemini partsリストを構築する。
    優先順位: url > image_b64 > content
    """
    if payload.get("url"):
        prompt = f"対象URL：{payload['url']}\n\n{PROMPT_SUFFIX}"
        return [{"text": prompt}]

    if payload.get("image_b64"):
        mime = payload.get("mime_type", "image/jpeg")
        return [
            {"inlineData": {"mimeType": mime, "data": payload["image_b64"]}},
            {"text": PROMPT_SUFFIX},
        ]

    # テキスト記事
    article = payload.get("content", "")
    prompt = f"【記事本文】\n{article}\n\n{PROMPT_SUFFIX}"
    return [{"text": prompt}]


def gemini_request(api_key: str, parts: list) -> str:
    """
    Gemini 2.5 Flash にリクエストを送り、テキスト回答を返す。
    Google Search Grounding を付与して最新情報を補完する。
    """
    url = f"{GEMINI_API_BASE}/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 3000,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        raise RuntimeError(f"Gemini HTTP {e.code}: {error_body}")
    except Exception as e:
        raise RuntimeError(f"Gemini request failed: {e}")

    # レスポンスからテキストを抽出
    try:
        candidates = result.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini returned no candidates")
        content = candidates[0].get("content", {})
        parts_out = content.get("parts", [])
        text = "".join(p.get("text", "") for p in parts_out)
        if not text:
            finish_reason = candidates[0].get("finishReason", "UNKNOWN")
            raise RuntimeError(f"Gemini returned empty response (finishReason: {finish_reason}). URLにアクセスできないか、コンテンツが取得できませんでした。")
        return text
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Gemini response parse error: {e}\nRaw: {result}")


def extract_summary(full_text: str) -> str:
    """【3行要約】セクションの3行を抽出して返す"""
    match = re.search(r"【3行要約】[^\S\r\n]*\n+━+\n+(.*?)(?=\n*━━)", full_text, re.DOTALL)
    if match:
        lines = [l.strip() for l in match.group(1).strip().split("\n") if l.strip()][:3]
        return "\n".join(lines)
    return ""


def extract_title(full_text: str, payload: dict) -> str:
    """
    【専門家ポジション】セクションの1行目をタイトルとして抽出する。
    失敗した場合は入力タイプに応じたフォールバックを返す。
    """
    # 【専門家ポジション】直後のテキストを取得
    match = re.search(r"【専門家ポジション】[^\S\r\n]*\n+([^\n━]+)", full_text)
    if match:
        title = match.group(1).strip()
        if title:
            return title[:100]

    # フォールバック: 入力タイプ + 日時
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    if payload.get("url"):
        try:
            from urllib.parse import urlparse
            domain = urlparse(payload["url"]).netloc
            return f"{domain} - {now}"
        except Exception:
            pass
    if payload.get("image_b64"):
        return f"画像分析 - {now}"
    return f"記事分析 - {now}"


def analyze(payload: dict) -> dict:
    """
    メインの分析関数。入力ペイロードを受け取り、Geminiで分析して返す。

    Args:
        payload: {"url": str} or {"content": str} or {"image_b64": str, "mime_type": str}

    Returns:
        {"title": str, "full_text": str}
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY が設定されていません")

    parts = build_parts(payload)
    full_text = gemini_request(api_key, parts)
    title = extract_title(full_text, payload)
    summary = extract_summary(full_text)

    return {"title": title, "full_text": full_text, "summary": summary}


# ---------------------------------------------------------------------------
# 自動カテゴリ / タグ分類
# ---------------------------------------------------------------------------

# 分類・要約は flash とは別クォータの軽量モデルを使う。
# （flash の無料枠は1日20回と少なく、記事ごとの呼び出しには不向きなため）
CLASSIFY_MODEL = "gemini-3.5-flash-lite"

# 実際に保存済みの記事363件を集計して決めたカテゴリ。
# AI関連が全体の約6割を占めるため、AIは用途で3分割している。
CATEGORIES = [
    "AIツール活用術",
    "AI開発・技術",
    "AI業界動向",
    "マーケティング・広告",
    "経営・組織・人材",
    "リサーチ・データ",
    "脳科学・心理・哲学",
    "その他",
]

FALLBACK_CATEGORY = "その他"
MAX_TAGS = 4

CLASSIFY_PROMPT = """以下の記事を分類し、要約してください。

タイトル: {title}
URL: {url}
本文・抜粋: {context}

【カテゴリ】次の中から最も適切なものを1つだけ選ぶ:
- AIツール活用術 … Claude/Gemini/ChatGPT等の使い方・プロンプト・活用事例
- AI開発・技術 … MCP・AIエージェント開発・LLMの仕組み・API・実装や自動化
- AI業界動向 … 新モデル発表・企業の戦略や提携・資金調達・市場ニュース
- マーケティング・広告 … ブランド・広告・EC・リテール・消費者・販促
- 経営・組織・人材 … 経営戦略・組織設計・チーム・リーダーシップ・働き方・採用
- リサーチ・データ … 調査結果・統計・データ分析手法・レポート・学術論文
- 脳科学・心理・哲学 … 脳科学・認知科学・心理学・進化論・哲学・思考法
- その他 … 上記のいずれにも当てはまらないもの

【タグ】記事を特徴づけるキーワードを{max_tags}個以内。
製品名・技術名・具体的なテーマを短い語で（例: Claude Code, MCP, プロンプト, SEO）。

【3行要約】記事の要点を最大3行、各行60字以内で。
与えられた情報から確実に言えることだけを書き、推測で補わない。
情報が少ない場合は1〜2行でよい。

JSON形式のみで出力: {{"category": "...", "tags": ["...", "..."], "summary": ["1行目", "2行目", "3行目"]}}"""


def classify(title: str, url: str = "", context: str = "") -> dict:
    """
    記事のタイトル（＋URL・本文抜粋）からカテゴリ・タグ・3行要約を生成する。

    保存フローを止めないことを最優先にしており、API キー未設定・通信失敗・
    解析失敗のいずれでも例外を投げず、フォールバック値を返す。

    Args:
        context: 記事本文の抜粋（SNSポスト本文や meta description）。要約の根拠になる。

    Returns:
        {"category": str, "tags": list[str], "summary": list[str], "ok": bool}
        ok が False のときは分類できずフォールバックした状態を示す。
        呼び出し側はこれを見て「分類済み」として記録しないよう判断できる。
    """
    fallback = {"category": FALLBACK_CATEGORY, "tags": [], "summary": [],
                "ok": False, "quota_exceeded": False}

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or not title:
        return fallback

    prompt = CLASSIFY_PROMPT.format(
        title=title[:300], url=url[:300], context=context[:4000] or "（なし）",
        max_tags=MAX_TAGS,
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            # gemini-2.5-flash は思考トークンも maxOutputTokens を消費する。
            # 出力自体は50トークン程度だが、思考分を見込んで余裕を持たせる。
            "maxOutputTokens": 4000,
            "responseMimeType": "application/json",
        },
    }
    body = json.dumps(payload).encode("utf-8")
    url_ = f"{GEMINI_API_BASE}/{CLASSIFY_MODEL}:generateContent?key={api_key}"

    # 無料枠はレート制限に触れやすいため、429 のときだけ間を空けて再試行する
    data = None
    for attempt in range(3):
        req = urllib.request.Request(
            url_, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            parts = result["candidates"][0]["content"]["parts"]
            data = json.loads("".join(p.get("text", "") for p in parts))
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            print(f"[classify] HTTP {e.code}: {title[:40]}")
            # 429 は割り当て切れ。呼び出し側が処理を止められるよう区別する
            return {**fallback, "quota_exceeded": e.code == 429}
        except Exception as e:
            # タイムアウト等の一時的な失敗はリトライする
            if attempt < 2:
                time.sleep(2)
                continue
            print(f"[classify] {type(e).__name__}: {title[:40]}")
            return fallback

    if data is None:
        return fallback

    category = data.get("category", "")
    if category not in CATEGORIES:
        category = FALLBACK_CATEGORY

    # Notion の multi_select はカンマを使えないため除去する
    tags = []
    for tag in data.get("tags") or []:
        if isinstance(tag, str):
            cleaned = tag.replace(",", " ").strip()[:60]
            if cleaned:
                tags.append(cleaned)

    summary = [
        s.strip()[:120] for s in (data.get("summary") or [])
        if isinstance(s, str) and s.strip()
    ][:3]

    return {"category": category, "tags": tags[:MAX_TAGS], "summary": summary,
            "ok": True, "quota_exceeded": False}


# ---------------------------------------------------------------------------
# 日次・週次レポート生成
# ---------------------------------------------------------------------------

SECTION_PROMPT = """あなたは、専門分野に精通したニュース解説者です。
以下は、ある読者が{label}保存した「{category}」分野の記事です。

{articles}

この{count}件について、音声で聴くためのニュース解説を日本語で書いてください。

■ 最重要：本文抜粋に基づいて、記事の中身を具体的に語ること
各記事には「本文抜粋」が付いています。これが一次情報なので、
**本文抜粋に書かれている事実を最優先の材料にしてください。**
本文抜粋が無い、または短い記事に限り、検索で内容を調べて補ってください。
検索するときは、タイトルに媒体名を添えると見つかります。

各記事について次を具体的に語ること。
　1. 何が起きたのか。**固有名詞・数字・日付を必ず含める**
　　 （「調査によると」ではなく「993人を対象にした調査で、EQ上位層の68パーセントが」のように）
　　 （「新モデルを発表」ではなく「パラメータ数30Bで、ベンチマークAではGPT-5を12ポイント上回った」のように）
　2. 記事が指摘している論点や結論。書き手が何を主張しているのか
　3. その背景と、読者にとっての意味

検索しても内容がわからなかった記事は、憶測で埋めず
「詳しい内容までは確認できませんでした」と正直に述べて短く切り上げてください。
事実の創作は絶対にしないこと。推測は「〜と考えられます」と明示すること。

■ 音声で聴くための書き方
箇条書きや記号（・、-、＊、#）は一切使わず、すべて自然な話し言葉の文章で書いてください。
見出しを記号で作らないこと。数字は「六十八パーセント」のように読み下すこと。
URLや記号の羅列は書かないこと。

■ 分量と構成
冒頭に「ここからは{category}の話題です。」と書き、続けて記事を1件ずつ解説してください。
**1記事あたり6文から10文**、しっかり中身を語ること。薄い紹介で終わらせないこと。
全体のまとめや次のアクションは書かないこと（別のパートで扱います）。"""

OVERVIEW_PROMPT = """以下は、ある読者が{label}保存した{count}件の記事の一覧です。

{articles}

この読者に向けて、音声で聴くニュース解説の「導入部分」を日本語で書いてください。
箇条書きや記号は使わず、自然な話し言葉で3文から4文にまとめること。
今日はどんな話題が多かったのか、そこから何が読み取れるのかを述べてください。
個別の記事の詳細には立ち入らないこと（このあと1件ずつ解説します）。"""

CLOSING_PROMPT = """以下は、ある読者が{label}保存した{count}件の記事の一覧です。

{articles}

この読者に向けて、音声で聴くニュース解説の「締めくくり」を日本語で書いてください。
箇条書きや記号は使わず、自然な話し言葉で書くこと。
まず今日の内容全体を2文から3文で振り返り、
続けてこの読者が次に取るべき行動を2つか3つ、文章の形で述べてください。"""


def clean_for_speech(text: str) -> str:
    """
    読み上げの妨げになる記号を取り除く。
    プロンプトで禁じても Markdown 記法が残ることがあるため、出力側で確実に消す。
    """
    lines = []
    for line in text.split("\n"):
        # 見出しの # は読み上げると雑音になるので落とす（内容は残す）
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        # 箇条書きの行頭記号を除去
        line = re.sub(r"^\s*[・\-\*\+]\s*", "", line)
        # 強調の ** や * を除去
        line = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", line)
        lines.append(line.rstrip())

    # 空行が続きすぎないようにまとめる
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return out.strip()


def _format_articles(articles: list, with_excerpt: bool = False) -> str:
    """記事リストをプロンプトに埋め込む形に整える"""
    lines = []
    for a in articles:
        lines.append(f"「{a.get('title', '')[:120]}」")
        if a.get("tags"):
            lines.append(f"    タグ: {' / '.join(a['tags'][:4])}")
        for s in (a.get("summary") or [])[:3]:
            lines.append(f"    要約: {s}")
        if with_excerpt and a.get("excerpt"):
            excerpt = re.sub(r"\s+", " ", a["excerpt"]).strip()[:2500]
            lines.append(f"    本文抜粋: {excerpt}")
        lines.append("")
    return "\n".join(lines)


def _call_gemini(prompt: str, api_key: str, use_search: bool, max_tokens: int) -> str:
    """
    Gemini を1回呼ぶ。flash が枠切れなら軽量モデルへフォールバックする。
    （検索は flash でしか使えないため、フォールバック時は検索なしになる）
    """
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": max_tokens},
    }
    if use_search:
        payload["tools"] = [{"google_search": {}}]
    body = json.dumps(payload).encode("utf-8")

    last_error = None
    for model in (GEMINI_MODEL, CLASSIFY_MODEL):
        if model == CLASSIFY_MODEL:
            # 軽量モデルは検索非対応なので外す
            payload.pop("tools", None)
            body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            parts_out = result["candidates"][0]["content"].get("parts", [])
            text = "".join(p.get("text", "") for p in parts_out)
            if text.strip():
                return text
            last_error = RuntimeError(
                f"{model}: empty ({result['candidates'][0].get('finishReason')})")
        except Exception as e:
            last_error = e
    raise RuntimeError(str(last_error))


def generate_report(articles: list, label: str) -> str:
    """
    記事リストからふりかえりレポートを生成する。

    カテゴリごとに分けて生成する。1回で全件を書かせると1記事あたりの分量が
    足りず、タイトルをなぞるだけの内容になってしまうため。

    Args:
        articles: [{"title": str, "category": str, "tags": list, "summary": list}, ...]
        label: "今日" "今週" など期間を表す語

    Returns:
        レポート本文。失敗時は RuntimeError。
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY が設定されていません")

    all_text = _format_articles(articles)[:20000]
    sections = []

    # 導入
    try:
        sections.append(_call_gemini(
            OVERVIEW_PROMPT.format(label=label, count=len(articles), articles=all_text),
            api_key, use_search=False, max_tokens=4000))
    except Exception as e:
        print(f"[report] 導入の生成に失敗: {e}")

    # カテゴリごとの本編（件数が多い順に扱う）
    by_category = {}
    for a in articles:
        by_category.setdefault(a.get("category") or "その他", []).append(a)

    for category, items in sorted(by_category.items(), key=lambda kv: -len(kv[1])):
        try:
            sections.append(_call_gemini(
                SECTION_PROMPT.format(
                    label=label, category=category, count=len(items),
                    articles=_format_articles(items, with_excerpt=True)[:40000]),
                api_key, use_search=True, max_tokens=32000))
            print(f"[report] {category}: {len(items)}件 完了")
        except Exception as e:
            print(f"[report] {category} の生成に失敗（この分野は飛ばします）: {e}")

    if not sections:
        raise RuntimeError("レポートを生成できませんでした")

    # 締めくくり
    try:
        sections.append(_call_gemini(
            CLOSING_PROMPT.format(label=label, count=len(articles), articles=all_text),
            api_key, use_search=False, max_tokens=4000))
    except Exception as e:
        print(f"[report] 締めの生成に失敗: {e}")

    return clean_for_speech("\n\n".join(sections))
