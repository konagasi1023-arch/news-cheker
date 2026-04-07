"""
notion_writer.py - Notion API 統合（News Cheker データベースへの書き込み）

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


def extract_page_id(value: str) -> str:
    """URL またはページ ID から 32 文字のページ ID を抽出する"""
    value = value.strip()
    match = re.search(r"([0-9a-f]{32})$", value.replace("-", ""))
    if match:
        return match.group(1)
    return value


def save_to_notion(url: str, title: str, token: str, database_id: str) -> str:
    """
    News Cheker データベースに URL + タイトルを保存する。

    Args:
        url:         保存するURL
        title:       ページタイトル
        token:       NOTION_TOKEN
        database_id: NOTION_DATABASE_ID

    Returns:
        作成された Notion ページの URL
    """
    page_data = {
        "parent": {"database_id": database_id},
        "properties": {
            "名前": {
                "title": [{"type": "text", "text": {"content": title[:500]}}]
            },
            "URL": {"url": url},
        },
    }

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
