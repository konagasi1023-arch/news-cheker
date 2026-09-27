"""
tag_vocab.py - 分類でタグを選ばせる「よく使うタグ」の一覧（tag_vocab.json）を作り直す

2026-09-28、タグの選択肢が 5,656 個に膨らみ Notion のデータベース定義が上限に達した
（新しいタグは書き込めない。notion_writer.notion_request が既存のタグだけに絞って保存する）。
API では選択肢を減らせない（1回に送れる選択肢が100個まで）ので、ユーザーの選択で
「分類のとき、よく使うタグの中から選ばせる」ことにした。一覧は gemini_client.classify が読む。

    python tag_vocab.py           # MIN_USES 回以上使われたタグで作り直す
    python tag_vocab.py --min 5   # 下限を変える

一覧はリポジトリに入れる（Render の保存処理も同じ一覧を使うため）。作り直したらコミットする。
"""

import argparse
import collections
import json
import os
import sys

import main  # .env
import notion_writer

MIN_USES = 3
PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tag_vocab.json")


def count_tags() -> collections.Counter:
    token, db = notion_writer.get_credentials()
    use, cursor = collections.Counter(), None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = notion_writer.notion_request("POST", f"/databases/{db}/query", token, body)
        for page in res["results"]:
            for t in page["properties"][notion_writer.TAG_PROPERTY]["multi_select"]:
                use[t["name"]] += 1
        if not res.get("has_more"):
            return use
        cursor = res["next_cursor"]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--min", type=int, default=MIN_USES)
    a = p.parse_args()
    use = count_tags()
    vocab = [t for t, n in use.most_common() if n >= a.min]
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(vocab, f, ensure_ascii=False, indent=0)
    print(f"{a.min}回以上のタグ {len(vocab)}個 → {PATH}（全 {len(use)}個）")
