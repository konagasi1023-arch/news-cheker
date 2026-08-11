"""
notebooklm_export.py - NotebookLM に投入する1ファイルを書き出す

NotebookLM には音声解説を生成する公開APIが無いため、完全自動化はできない。
そこで「NotebookLM にアップロードするだけ」の状態のファイルを作る。

出力した .md を NotebookLM の「ソースを追加」に入れ、
「音声解説を生成」を押せば、その期間の記事をまとめた対談音声が作られる。

使い方:
    python notebooklm_export.py                    # 直近7日分
    python notebooklm_export.py --days 30          # 直近30日分
    python notebooklm_export.py --out "C:/path/to/output.md"
"""

import argparse
import os
from collections import defaultdict

import main  # .env の読み込みのため
import notion_writer

HEADER = """# ニュースまとめ（直近{days}日 / {count}件）

このドキュメントは、保存したニュース記事のタイトル・分類・要約をまとめたものです。
カテゴリごとに整理されています。各記事には元記事のURLが付いています。

"""


def export(days: int, out_path: str) -> None:
    token, database_id = notion_writer.get_credentials()

    articles = notion_writer.fetch_recent_articles(token, database_id, days)
    if not articles:
        print(f"直近{days}日に保存された記事がありません。")
        return

    by_category = defaultdict(list)
    for a in articles:
        by_category[a["category"] or "未分類"].append(a)

    lines = [HEADER.format(days=days, count=len(articles))]
    for category, items in sorted(by_category.items(), key=lambda x: -len(x[1])):
        lines.append(f"## {category}（{len(items)}件）\n")
        for a in items:
            lines.append(f"### {a['title']}\n")
            if a["tags"]:
                lines.append(f"キーワード: {' / '.join(a['tags'])}\n")
            for s in a["summary"]:
                lines.append(f"- {s}")
            if a["summary"]:
                lines.append("")
            if a["url"]:
                lines.append(f"出典: {a['url']}\n")
        lines.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"書き出しました: {out_path}")
    print(f"  記事数: {len(articles)}件 / カテゴリ: {len(by_category)}種")
    print()
    print("次の手順:")
    print("  1. https://notebooklm.google.com を開く")
    print("  2. ノートブックを作成し、このファイルを「ソースを追加」でアップロード")
    print("  3. 「音声解説」を生成 → スマホの NotebookLM アプリで聴ける")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="対象期間（日数）")
    parser.add_argument("--out", default="", help="出力先パス")
    args = parser.parse_args()

    out = args.out or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"notebooklm-{args.days}days.md")
    export(args.days, out)
