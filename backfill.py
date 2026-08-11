"""
backfill.py - 既存の Notion ページにタイトル再取得とカテゴリ/タグ付与を行う一回限りのスクリプト

`import re` のバグでタイトルが取得できずドメイン名のまま保存されたページが多数あるため、
先にタイトルを直してから分類する（順序が逆だと誤ったタイトルで分類してしまう）。

進捗は progress.json に保存し、途中で止めても再実行すれば続きから処理する。

使い方:
    python backfill.py --dry-run       # 変更せず対象だけ表示
    python backfill.py --limit 10      # 10件だけ実処理（試運転）
    python backfill.py                 # 全件処理
    python backfill.py --report-dupes  # 重複URLの一覧だけ出力
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from urllib.parse import urlparse

import main  # .env 読み込みと fetch_title のため
import gemini_client
import notion_writer

PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "progress.json")

# Gemini 無料枠のレート制限（毎分あたりの回数）に触れないための待機秒数
SLEEP_BETWEEN = 5


def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done": []}


def save_progress(progress: dict) -> None:
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=1)


def fetch_all_pages(token: str, database_id: str) -> list:
    """DB の全ページを取得する"""
    pages = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = notion_writer.notion_request(
            "POST", f"/databases/{database_id}/query", token, body
        )
        for pg in res.get("results", []):
            props = pg.get("properties", {})
            title, url, category = "", "", ""
            for key, val in props.items():
                kind = val.get("type")
                if kind == "title":
                    title = "".join(a.get("plain_text", "") for a in val.get("title", []))
                elif kind == "url":
                    url = val.get("url") or ""
                elif kind == "select":
                    category = (val.get("select") or {}).get("name", "")
            pages.append({"id": pg["id"], "title": title, "url": url, "category": category})
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return pages


def is_missing_title(page: dict) -> bool:
    """タイトルがドメイン名のまま＝取得に失敗していたページか判定する"""
    if not page["url"] or not page["title"]:
        return False
    return page["title"].strip() == urlparse(page["url"]).netloc


def report_dupes(pages: list) -> None:
    """重複URLを一覧表示する（削除はしない）"""
    by_url = defaultdict(list)
    for pg in pages:
        if pg["url"]:
            by_url[pg["url"]].append(pg)

    dupes = {u: v for u, v in by_url.items() if len(v) > 1}
    if not dupes:
        print("重複URLはありません。")
        return

    total_extra = sum(len(v) - 1 for v in dupes.values())
    print(f"=== 重複URL {len(dupes)}種 / 余分なページ {total_extra}件 ===")
    print("※ 削除はしません。必要なら Notion 上で手動削除してください。\n")
    for url, group in sorted(dupes.items(), key=lambda x: -len(x[1])):
        print(f"[{len(group)}件] {group[0]['title'][:60]}")
        print(f"        {url[:100]}")
        for pg in group:
            print(f"          https://www.notion.so/{pg['id'].replace('-', '')}")
        print()


def main_backfill(limit: int, dry_run: bool, skip_titles: bool) -> None:
    token, database_id = notion_writer.get_credentials()
    if not dry_run:
        notion_writer.ensure_properties(token, database_id)

    pages = fetch_all_pages(token, database_id)
    print(f"総ページ数: {len(pages)}")

    progress = load_progress()
    done = set(progress["done"])

    targets = [p for p in pages if p["id"] not in done]
    if limit:
        targets = targets[:limit]

    missing = [p for p in pages if is_missing_title(p)]
    print(f"タイトル未取得: {len(missing)}件 / 処理対象: {len(targets)}件")
    if dry_run:
        print("\n--- DRY RUN（変更しません）---")
        for p in targets[:20]:
            flag = "[要タイトル再取得]" if is_missing_title(p) else ""
            print(f"  {flag} {p['title'][:60]}")
        return

    stats = {"title_fixed": 0, "classified": 0, "failed": 0}

    for i, page in enumerate(targets, 1):
        title, url = page["title"], page["url"]

        # 1. タイトルがドメイン名のままなら取り直す
        if not skip_titles and is_missing_title(page):
            fetched = main.fetch_title(url)
            if fetched and fetched != title:
                title = fetched
                stats["title_fixed"] += 1

        # 2. 分類する
        result = gemini_client.classify(title, url)
        if not result["ok"]:
            # 無料枠を使い切った状態で続けても「その他」を量産するだけなので中断する。
            # 進捗は保存済みなので、翌日そのまま再実行すれば続きから処理される。
            print("\n分類に失敗したため中断します（Gemini の割り当てを使い切った可能性）。")
            print("時間をおいて再実行すると、この続きから処理します。")
            break
        category, tags = result["category"], result["tags"]

        # 3. Notion を更新する
        try:
            notion_writer.notion_request(
                "PATCH",
                f"/pages/{page['id']}",
                token,
                {"properties": notion_writer.build_properties(url, title, category, tags)},
            )
            stats["classified"] += 1
            done.add(page["id"])
            progress["done"] = list(done)
            save_progress(progress)
            print(f"[{i}/{len(targets)}] [{category}] {title[:45]}")
        except Exception as e:
            stats["failed"] += 1
            print(f"[{i}/{len(targets)}] 失敗: {type(e).__name__}: {str(e)[:80]}")

        time.sleep(SLEEP_BETWEEN)

    print(f"\n=== 完了 ===")
    print(f"  分類済み      : {stats['classified']}")
    print(f"  タイトル修復  : {stats['title_fixed']}")
    print(f"  失敗          : {stats['failed']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="処理件数の上限（0で全件）")
    parser.add_argument("--dry-run", action="store_true", help="変更せず対象を表示")
    parser.add_argument("--skip-titles", action="store_true", help="タイトル再取得をしない")
    parser.add_argument("--report-dupes", action="store_true", help="重複URLの一覧のみ出力")
    args = parser.parse_args()

    if args.report_dupes:
        token, database_id = notion_writer.get_credentials()
        report_dupes(fetch_all_pages(token, database_id))
        sys.exit(0)

    main_backfill(args.limit, args.dry_run, args.skip_titles)
