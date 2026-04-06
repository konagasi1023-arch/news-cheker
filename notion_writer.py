"""
notion_writer.py - Notion API 統合（News Cheker データベースへの書き込み）

send_notion.py（既存）から notion_request() / split_text() パターンを流用。
★差分: "parent": {"database_id": ...} でDBに構造化レコードとして追加する。

環境変数:
  NOTION_TOKEN       - インテグレーションのシークレットキー
  NOTION_DATABASE_ID - News Cheker データベースのID（32文字）
"""

import json
import os
import urllib.request
import urllib.error
import re

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2022-06-28"


# ---------------------------------------------------------------------------
# ユーティリティ（send_notion.py から移植）
# ---------------------------------------------------------------------------

def notion_request(method: str, endpoint: str, token: str, data: dict = None) -> dict:
    """Notion API にリクエストを送る"""
    url = f"{NOTION_API_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION,
    }
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        raise RuntimeError(f"Notion HTTP {e.code}: {error_body}")
    except Exception as e:
        raise RuntimeError(f"Notion request failed: {e}")


def split_text(text: str, max_len: int = 1900) -> list:
    """Notion の 2000 文字制限に合わせてテキストを分割する"""
    chunks = []
    while len(text) > max_len:
        chunks.append(text[:max_len])
        text = text[max_len:]
    if text:
        chunks.append(text)
    return chunks


def extract_page_id(value: str) -> str:
    """URL またはページ ID から 32 文字のページ ID を抽出する"""
    value = value.strip()
    match = re.search(r"([0-9a-f]{32})$", value.replace("-", ""))
    if match:
        return match.group(1)
    return value


# ---------------------------------------------------------------------------
# Notion DB への書き込み
# ---------------------------------------------------------------------------

def save_to_notion(payload: dict, analysis: dict, token: str, database_id: str) -> str:
    """
    News Cheker データベースに新規レコードを作成する。

    Args:
        payload:     Webhookで受け取った入力（url / content / image_b64）
        analysis:    {"title": str, "full_text": str}
        token:       NOTION_TOKEN
        database_id: NOTION_DATABASE_ID

    Returns:
        作成された Notion ページの URL
    """
    title = analysis.get("title", "分析結果")[:500]
    full_text = analysis.get("full_text", "")
    summary = analysis.get("summary", "")
    source_url = payload.get("url", "") or ""

    # AI解説を 1900 文字ごとに分割して rich_text 配列に格納
    rich_text_blocks = [
        {"type": "text", "text": {"content": chunk}}
        for chunk in split_text(full_text)
    ]

    # URL プロパティ（空文字の場合は None に変換 → Notion API が null を要求するため）
    url_value = source_url if source_url else None

    # ページ作成ペイロード
    # ★ "parent": {"database_id": ...} → DBに構造化レコードとして追加（page_id とは異なる）
    page_data = {
        "parent": {"database_id": database_id},
        "properties": {
            "名前": {
                "title": [{"type": "text", "text": {"content": title}}]
            },
            "概要": {
                "rich_text": [{"type": "text", "text": {"content": summary[:1900]}}]
            },
            "AI解説": {
                "rich_text": rich_text_blocks
            },
        },
    }

    # URL プロパティは値がある場合のみ追加
    if url_value:
        page_data["properties"]["URL"] = {"url": url_value}

    page = notion_request("POST", "/pages", token, page_data)
    page_id = page["id"].replace("-", "")
    return f"https://www.notion.so/{page_id}"


def get_credentials() -> tuple:
    """環境変数から認証情報を取得して検証する"""
    token = os.environ.get("NOTION_TOKEN", "").strip()
    raw_id = os.environ.get("NOTION_DATABASE_ID", "").strip()

    if not token:
        raise RuntimeError("NOTION_TOKEN が設定されていません")
    if not raw_id:
        raise RuntimeError("NOTION_DATABASE_ID が設定されていません")

    database_id = extract_page_id(raw_id)
    return token, database_id
