"""
backfill_body.py - 過去記事の本文を取得して要約を作り直すバックフィル

やること（1件ずつ）:
  1. fetch_meta で本文を取得（SmartNews は元記事URLを解決してから取る）
  2. 本文を材料に分類・タグ・3行要約を作り直す
  3. Notion を更新する:
     - 「元記事URL」プロパティ
     - カテゴリ・タグ（分類が成功した場合のみ上書き）
     - 3行要約コールアウト（あれば書き換え、無ければ追加）
     - 本文抜粋の段落（まだ無い場合のみ追加）

進捗は body_progress.json に保存され、中断しても再実行で続きから処理する。

使い方:
    python backfill_body.py --limit 3   # 試運転
    python backfill_body.py             # 全件
"""

import argparse
import json
import os
import time

import main  # .env 読み込みと fetch_meta のため
import gemini_client
import notion_writer

PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "body_progress.json")
SLEEP_BETWEEN = 2


def load_progress() -> set:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return set(json.load(f)["done"])
    return set()


def save_progress(done: set) -> None:
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"done": sorted(done)}, f, ensure_ascii=False)


def fetch_all_pages(token: str, database_id: str) -> list:
    """全ページの id / title / url / category を取得する"""
    pages = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = notion_writer.notion_request(
            "POST", f"/databases/{database_id}/query", token, body)
        for pg in res.get("results", []):
            item = {"id": pg["id"], "title": "", "url": "", "category": ""}
            for name, val in pg.get("properties", {}).items():
                kind = val.get("type")
                if kind == "title":
                    item["title"] = "".join(
                        a.get("plain_text", "") for a in val.get("title", []))
                elif kind == "url" and name == "URL":
                    # 「元記事URL」ではなく保存時の URL プロパティだけを使う
                    item["url"] = val.get("url") or ""
                elif kind == "select":
                    item["category"] = (val.get("select") or {}).get("name", "")
            pages.append(item)
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return pages


def update_page_blocks(token: str, page_id: str, summary: list, excerpt: str) -> None:
    """要約コールアウトを書き換え（無ければ追加）、抜粋段落を追加する"""
    blocks = notion_writer.notion_request(
        "GET", f"/blocks/{page_id}/children?page_size=10", token)

    callout_id = None
    has_paragraph = False
    for blk in blocks.get("results", []):
        if blk.get("type") == "callout" and callout_id is None:
            callout_id = blk["id"]
        elif blk.get("type") == "paragraph":
            text = "".join(r.get("plain_text", "")
                           for r in blk["paragraph"].get("rich_text", []))
            if text.strip():
                has_paragraph = True

    # 要約コールアウト
    if summary:
        text = "\n".join(summary)[:1900]
        if callout_id:
            notion_writer.notion_request(
                "PATCH", f"/blocks/{callout_id}", token,
                {"callout": {"rich_text": [
                    {"type": "text", "text": {"content": text}}]}})
        else:
            notion_writer.notion_request(
                "PATCH", f"/blocks/{page_id}/children", token,
                {"children": notion_writer.build_excerpt_blocks(summary, "")})

    # 本文抜粋（既に段落があるページには足さない）
    if excerpt and not has_paragraph:
        notion_writer.notion_request(
            "PATCH", f"/blocks/{page_id}/children", token,
            {"children": notion_writer.build_excerpt_blocks(None, excerpt)})


def run(limit: int = 0) -> None:
    token, database_id = notion_writer.get_credentials()
    notion_writer.ensure_properties(token, database_id)

    pages = fetch_all_pages(token, database_id)
    done = load_progress()
    targets = [
        p for p in pages
        if p["id"] not in done
        and p["url"]
        and p["category"] != notion_writer.REPORT_CATEGORY
    ]
    if limit:
        targets = targets[:limit]

    print(f"総ページ数: {len(pages)} / 処理対象: {len(targets)}件")
    stats = {"body": 0, "no_body": 0, "reclassified": 0, "failed": 0}

    for i, page in enumerate(targets, 1):
        title, url = page["title"], page["url"]
        try:
            meta = main.fetch_meta(url)
            body = meta["body"]

            if body:
                stats["body"] += 1
            else:
                stats["no_body"] += 1

            # 本文が取れたときだけ分類・要約を作り直す（無ければ現状維持）
            if body:
                result = gemini_client.classify(title, url, body)
                if result.get("quota_exceeded"):
                    print("\nGemini の割り当てを使い切ったため中断します。")
                    print("時間をおいて再実行すると、この続きから処理します。")
                    break

                props = {}
                if meta["original_url"]:
                    props[notion_writer.ORIGINAL_URL_PROPERTY] = {
                        "url": meta["original_url"]}
                if result["ok"]:
                    props[notion_writer.CATEGORY_PROPERTY] = {
                        "select": {"name": result["category"]}}
                    if result["tags"]:
                        props[notion_writer.TAG_PROPERTY] = {
                            "multi_select": [{"name": t} for t in result["tags"]]}
                if props:
                    notion_writer.notion_request(
                        "PATCH", f"/pages/{page['id']}", token,
                        {"properties": props})

                update_page_blocks(
                    token, page["id"],
                    result["summary"] if result["ok"] else None, body)
                if result["ok"]:
                    stats["reclassified"] += 1

            done.add(page["id"])
            save_progress(done)
            mark = "本文OK" if body else "本文なし"
            print(f"[{i}/{len(targets)}] {mark}: {title[:45]}")
        except Exception as e:
            stats["failed"] += 1
            print(f"[{i}/{len(targets)}] 失敗: {type(e).__name__}: {str(e)[:80]}")

        time.sleep(SLEEP_BETWEEN)

    print("\n=== 完了 ===")
    print(f"  本文取得      : {stats['body']}")
    print(f"  本文なし      : {stats['no_body']}（X・Facebook・削除済み等）")
    print(f"  要約作り直し  : {stats['reclassified']}")
    print(f"  失敗          : {stats['failed']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="処理件数の上限（試運転用）")
    args = parser.parse_args()
    run(args.limit)
