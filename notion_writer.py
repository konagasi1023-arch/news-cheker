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
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

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


CATEGORY_PROPERTY = "カテゴリ"
TAG_PROPERTY = "タグ"

# プロパティ確認は起動後1回で足りるのでプロセス内でキャッシュする
_properties_checked = False


def ensure_properties(token: str, database_id: str) -> None:
    """
    データベースに「カテゴリ」「タグ」プロパティが無ければ追加する。

    select / multi_select のオプションは、ページ作成時に未知の値を渡せば
    Notion 側で自動生成されるため、ここでは入れ物だけ用意する。
    """
    global _properties_checked
    if _properties_checked:
        return

    db = notion_request("GET", f"/databases/{database_id}", token)
    existing = db.get("properties", {})

    missing = {}
    if CATEGORY_PROPERTY not in existing:
        missing[CATEGORY_PROPERTY] = {"select": {}}
    if TAG_PROPERTY not in existing:
        missing[TAG_PROPERTY] = {"multi_select": {}}

    if missing:
        notion_request(
            "PATCH", f"/databases/{database_id}", token, {"properties": missing}
        )

    _properties_checked = True


def find_by_url(url: str, token: str, database_id: str) -> str:
    """
    同じ URL のページが既にあれば、そのページの URL を返す（重複保存の防止用）。
    無ければ空文字を返す。
    """
    res = notion_request(
        "POST", f"/databases/{database_id}/query", token,
        {"filter": {"property": "URL", "url": {"equals": url}}, "page_size": 1},
    )
    results = res.get("results", [])
    if not results:
        return ""
    return f"https://www.notion.so/{results[0]['id'].replace('-', '')}"


def build_properties(url: str, title: str, category: str = "", tags: list = None) -> dict:
    """ページのプロパティ辞書を組み立てる（新規作成・更新で共用）"""
    props = {
        "名前": {"title": [{"type": "text", "text": {"content": title[:500]}}]},
        "URL": {"url": url},
    }
    if category:
        props[CATEGORY_PROPERTY] = {"select": {"name": category}}
    if tags:
        props[TAG_PROPERTY] = {"multi_select": [{"name": t} for t in tags]}
    return props


def save_to_notion(
    url: str,
    title: str,
    token: str,
    database_id: str,
    category: str = "",
    tags: list = None,
    summary: list = None,
) -> str:
    """
    News Cheker データベースに URL + タイトル + 分類 + 要約を保存する。

    Args:
        url:         保存するURL
        title:       ページタイトル
        token:       NOTION_TOKEN
        database_id: NOTION_DATABASE_ID
        category:    カテゴリ（省略可）
        tags:        タグのリスト（省略可）
        summary:     3行要約（省略可。ページ本文のコールアウトとして書き込む）

    Returns:
        作成された Notion ページの URL
    """
    properties = build_properties(url, title, category, tags)
    properties["日付"] = {
        "date": {"start": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")}
    }

    page_data = {"parent": {"database_id": database_id}, "properties": properties}
    if summary:
        text = "\n".join(summary)[:1900]
        page_data["children"] = [{
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": text}}],
                "icon": {"type": "emoji", "emoji": "📝"},
            },
        }]

    page = notion_request("POST", "/pages", token, page_data)
    page_id = page["id"].replace("-", "")
    return f"https://www.notion.so/{page_id}"


REPORT_CATEGORY = "レポート"


def fetch_recent_articles(token: str, database_id: str, days: int) -> list:
    """
    直近 days 日に保存された記事（レポートを除く）を取得する。
    各記事のページ本文から3行要約も読み取って返す。
    """
    from datetime import timedelta
    since = (datetime.now(JST) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00+09:00")

    articles = []
    cursor = None
    while True:
        # カテゴリ除外は Python 側で行う。存在しない select オプションを
        # フィルタに指定すると Notion が 400 を返すため（初回は「レポート」が未作成）。
        body = {
            "filter": {"property": "日付", "date": {"on_or_after": since}},
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        res = notion_request("POST", f"/databases/{database_id}/query", token, body)

        for pg in res.get("results", []):
            props = pg.get("properties", {})
            item = {"id": pg["id"], "title": "", "url": "", "category": "",
                    "tags": [], "summary": []}
            for val in props.values():
                kind = val.get("type")
                if kind == "title":
                    item["title"] = "".join(
                        a.get("plain_text", "") for a in val.get("title", []))
                elif kind == "url":
                    item["url"] = val.get("url") or ""
                elif kind == "select":
                    item["category"] = (val.get("select") or {}).get("name", "")
                elif kind == "multi_select":
                    item["tags"] = [o["name"] for o in val.get("multi_select", [])]
            if item["category"] != REPORT_CATEGORY:
                articles.append(item)

        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")

    # ページ本文のコールアウト（3行要約）を読み取る
    for item in articles:
        try:
            blocks = notion_request(
                "GET", f"/blocks/{item['id']}/children?page_size=5", token)
            for blk in blocks.get("results", []):
                if blk.get("type") == "callout":
                    text = "".join(
                        r.get("plain_text", "")
                        for r in blk["callout"].get("rich_text", []))
                    item["summary"] = [l for l in text.split("\n") if l.strip()]
                    break
        except Exception:
            pass  # 要約が読めなくてもレポートは作れる

    return articles


def save_report(title: str, body_text: str, token: str, database_id: str) -> str:
    """レポートを1ページとして DB に保存する（カテゴリ=レポート）"""
    properties = {
        "名前": {"title": [{"type": "text", "text": {"content": title[:500]}}]},
        CATEGORY_PROPERTY: {"select": {"name": REPORT_CATEGORY}},
        "日付": {
            "date": {"start": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")}
        },
    }

    # Notion のブロックは1つ2000字までなので段落単位で分割する
    blocks = []
    chunk = ""
    for para in body_text.split("\n\n"):
        if len(chunk) + len(para) > 1800 and chunk:
            blocks.append(chunk)
            chunk = para
        else:
            chunk = f"{chunk}\n\n{para}" if chunk else para
    if chunk:
        blocks.append(chunk)

    children = [{
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": b[:1990]}}]},
    } for b in blocks[:90]]

    page = notion_request("POST", "/pages", token, {
        "parent": {"database_id": database_id},
        "properties": properties,
        "children": children,
    })
    return f"https://www.notion.so/{page['id'].replace('-', '')}"


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
