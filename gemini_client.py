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
