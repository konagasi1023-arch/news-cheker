"""
obsidian_sync.py - Notion の News Cheker DB を Obsidian vault に Markdown として同期する

PC 側で実行するスクリプト。サーバー（Render）には依存しない。
frontmatter 付き Markdown を生成するので、Obsidian のタグ・グラフビュー・
検索・Dataview がそのまま使える。

使い方:
    python obsidian_sync.py --vault "C:/path/to/vault"          # 増分同期
    python obsidian_sync.py --vault "C:/path/to/vault" --full   # 全件同期し直す

- 記事は vault/News Checker/ 配下に「YYYY-MM-DD タイトル - id.md」で保存される
- レポートは vault/News Checker/Reports/ 配下に保存される
- 前回同期時刻は vault/News Checker/.sync-state.json に記録され、以後は増分のみ
"""

import argparse
import json
import os
import re
import sys

import main  # .env の読み込みのため
import notion_writer

SUBDIR = "News Checker"
STATE_FILE = ".sync-state.json"

# Windows/Obsidian で使えない文字
INVALID_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')


def sanitize_filename(title: str, max_len: int = 60) -> str:
    name = INVALID_CHARS.sub(" ", title)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len].strip() or "untitled"


def load_state(base_dir: str) -> dict:
    path = os.path.join(base_dir, STATE_FILE)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(base_dir: str, state: dict) -> None:
    with open(os.path.join(base_dir, STATE_FILE), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def fetch_pages(token: str, database_id: str, since: str = "") -> list:
    """全ページ（since 指定時は last_edited_time がそれ以降のもの）を取得する"""
    pages = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if since:
            body["filter"] = {
                "timestamp": "last_edited_time",
                "last_edited_time": {"on_or_after": since},
            }
        if cursor:
            body["start_cursor"] = cursor
        res = notion_writer.notion_request(
            "POST", f"/databases/{database_id}/query", token, body)
        pages.extend(res.get("results", []))
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return pages


def parse_page(pg: dict) -> dict:
    """Notion ページオブジェクトから必要な情報を取り出す"""
    item = {"id": pg["id"], "title": "", "url": "", "original_url": "",
            "category": "", "tags": [], "date": "",
            "edited": pg.get("last_edited_time", "")}
    for name, val in pg.get("properties", {}).items():
        kind = val.get("type")
        if kind == "title":
            title = "".join(a.get("plain_text", "") for a in val.get("title", []))
            # 共有元アプリが付けた前後の引用符を取り除く
            if len(title) > 1 and title[0] == title[-1] == '"':
                title = title[1:-1].strip()
            item["title"] = title
        elif kind == "url":
            if name == notion_writer.ORIGINAL_URL_PROPERTY:
                item["original_url"] = val.get("url") or ""
            else:
                item["url"] = val.get("url") or ""
        elif kind == "select":
            item["category"] = (val.get("select") or {}).get("name", "")
        elif kind == "multi_select":
            item["tags"] = [o["name"] for o in val.get("multi_select", [])]
        elif kind == "date":
            item["date"] = ((val.get("date") or {}).get("start") or "")[:10]
    return item


def fetch_body_text(token: str, page_id: str) -> dict:
    """
    ページ本文を取得する。
    コールアウトは3行要約、段落は本文抜粋（レポートの場合はレポート本文）。
    """
    summary, paragraphs = [], []
    try:
        res = notion_writer.notion_request(
            "GET", f"/blocks/{page_id}/children?page_size=100", token)
        for blk in res.get("results", []):
            kind = blk.get("type")
            if kind not in ("paragraph", "callout", "quote", "bulleted_list_item"):
                continue
            text = "".join(
                r.get("plain_text", "") for r in blk[kind].get("rich_text", []))
            if not text.strip():
                continue
            if kind == "callout" and not summary:
                summary = [l for l in text.split("\n") if l.strip()]
            else:
                paragraphs.append(text)
    except Exception:
        pass
    return {"summary": summary, "excerpt": "\n\n".join(paragraphs)}


def build_markdown(item: dict, body: dict) -> str:
    """frontmatter 付き Markdown を組み立てる"""
    def yaml_escape(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["---"]
    lines.append(f"title: {yaml_escape(item['title'])}")
    if item["url"]:
        lines.append(f"url: {item['url']}")
    if item.get("original_url"):
        lines.append(f"source_url: {item['original_url']}")
    if item["category"]:
        lines.append(f"category: {yaml_escape(item['category'])}")
    if item["tags"]:
        lines.append("tags:")
        for t in item["tags"]:
            lines.append(f"  - {yaml_escape(t)}")
    if item["date"]:
        lines.append(f"saved: {item['date']}")
    lines.append(f"notion: https://www.notion.so/{item['id'].replace('-', '')}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {item['title']}")
    lines.append("")

    if body["summary"]:
        lines.append("## 要約")
        lines.append("")
        for s in body["summary"]:
            lines.append(f"- {s}")
        lines.append("")
    if body["excerpt"]:
        # レポートは要約を持たないので、見出しを付けずそのまま本文にする
        if body["summary"]:
            lines.append("## 本文抜粋")
            lines.append("")
        lines.append(body["excerpt"])
        lines.append("")

    link = item.get("original_url") or item["url"]
    if link:
        lines.append(f"🔗 [元記事]({link})")
    return "\n".join(lines) + "\n"


def sync(vault: str, full: bool) -> None:
    token, database_id = notion_writer.get_credentials()

    base_dir = os.path.join(vault, SUBDIR)
    reports_dir = os.path.join(base_dir, "Reports")
    os.makedirs(reports_dir, exist_ok=True)

    state = {} if full else load_state(base_dir)
    since = state.get("last_sync", "")

    pages = fetch_pages(token, database_id, since)
    print(f"同期対象: {len(pages)}件" + (f"（{since} 以降の更新分）" if since else "（全件）"))

    latest_edit = since
    written = 0
    for i, pg in enumerate(pages, 1):
        item = parse_page(pg)
        if not item["title"]:
            continue

        body = fetch_body_text(token, item["id"])
        md = build_markdown(item, body)

        is_report = item["category"] == notion_writer.REPORT_CATEGORY
        target_dir = reports_dir if is_report else base_dir

        # 同じページの古いファイル（タイトル変更前など）を掃除してから書く
        id8 = item["id"].replace("-", "")[-8:]
        for old in os.listdir(target_dir):
            if old.endswith(f"{id8}.md"):
                os.remove(os.path.join(target_dir, old))

        prefix = f"{item['date']} " if item["date"] else ""
        filename = f"{prefix}{sanitize_filename(item['title'])} - {id8}.md"
        with open(os.path.join(target_dir, filename), "w", encoding="utf-8") as f:
            f.write(md)
        written += 1

        if item["edited"] > latest_edit:
            latest_edit = item["edited"]
        if i % 50 == 0:
            print(f"  ... {i}/{len(pages)}")

    if latest_edit:
        save_state(base_dir, {"last_sync": latest_edit})

    print(f"完了: {written}件を書き出しました → {base_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True, help="Obsidian vault のパス")
    parser.add_argument("--full", action="store_true", help="全件を同期し直す")
    args = parser.parse_args()

    if not os.path.isdir(args.vault):
        print(f"vault が見つかりません: {args.vault}")
        sys.exit(1)

    sync(args.vault, args.full)
