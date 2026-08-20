"""
split_report.py - レポートを音声解説で聴き切れる大きさに分割する

NotebookLM の音声解説は1本あたり30分前後が上限で、それを超える量を渡すと
記事が読み飛ばされる。かといって細かく割りすぎるとノートブックを何個も
作ることになる。そこで「1本が上限に収まる範囲で、最も少ないファイル数」に分ける。

分割はカテゴリの切れ目で行い、1カテゴリが単独で上限を超える場合だけ
記事の切れ目（「N件目。」）で割る。番号はレポート全体を通した連番のままなので、
どのファイルが何件目から何件目までを扱うかが分かる。

使い方:
    python split_report.py "<レポートのパス>"
    python split_report.py "<レポートのパス>" --minutes 20   # 1本の上限を変える
"""

import argparse
import math
import os
import re
import shutil

# 読み上げ速度の実測値（日本語）。1分あたりの文字数
CHARS_PER_MINUTE = 350

# 音声解説1本の上限。ファイル数を増やしたくないので上限いっぱいの30分に置く
DEFAULT_MAX_MINUTES = 30

ARTICLE_RE = re.compile(r"\d+件目")
SECTION_RE = re.compile(r"ここからは.{2,20}?の話題です。")


def split_into_blocks(text: str) -> list:
    """レポートをカテゴリ単位のブロックに分ける（導入は先頭ブロックに含める）"""
    starts = [m.start() for m in SECTION_RE.finditer(text)]
    if not starts:
        return [text]
    bounds = starts + [len(text)]
    blocks = [text[:bounds[0]].rstrip()] if bounds[0] else []
    for i in range(len(starts)):
        blocks.append(text[bounds[i]:bounds[i + 1]].strip())
    # 導入だけのブロックは単独では意味がないので次と結合する
    if len(blocks) > 1 and not SECTION_RE.match(blocks[0]):
        blocks[1] = blocks[0] + "\n\n" + blocks[1]
        blocks.pop(0)
    return blocks


def split_block(block: str, limit: int) -> list:
    """上限を超えるブロックを記事の切れ目で割る"""
    if len(block) <= limit:
        return [block]
    starts = [m.start() for m in ARTICLE_RE.finditer(block)]
    if len(starts) < 2:
        return [block]  # 割れないならそのまま（1記事が長いだけ）

    pieces, current = [], block[:starts[0]]
    for i, pos in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(block)
        article = block[pos:end]
        if current and len(current) + len(article) > limit:
            pieces.append(current.strip())
            current = article
        else:
            current += article
    if current.strip():
        pieces.append(current.strip())
    return pieces


def pack(blocks: list, limit: int) -> list:
    """順序を保ったまま、1つの束が limit を超えないように詰める"""
    bins, current = [], ""
    for b in blocks:
        if current and len(current) + len(b) > limit:
            bins.append(current)
            current = b
        else:
            current = f"{current}\n\n{b}" if current else b
    if current:
        bins.append(current)
    return bins


def balanced_split(text: str, max_chars: int) -> list:
    """最小のファイル数で、かつ各ファイルができるだけ均等になるように分ける"""
    blocks = []
    for b in split_into_blocks(text):
        blocks.extend(split_block(b, max_chars))

    fewest = len(pack(blocks, max_chars))
    if fewest <= 1:
        return [text]

    # 同じファイル数に収まる範囲で上限を下げ、分量を均す
    best = pack(blocks, max_chars)
    for limit in range(max_chars, 0, -200):
        candidate = pack(blocks, limit)
        if len(candidate) > fewest:
            break
        best = candidate
    return best


def run(path: str, max_minutes: int) -> None:
    text = open(path, encoding="utf-8").read().strip()
    total_articles = len(ARTICLE_RE.findall(text))
    max_chars = max_minutes * CHARS_PER_MINUTE

    parts = balanced_split(text, max_chars)
    date = re.search(r"\d{4}-\d{2}-\d{2}", os.path.basename(path))
    date = date.group(0) if date else "report"

    outdir = os.path.join(os.path.dirname(path), f"{date}_分割")
    if len(parts) <= 1:
        print(f"全{total_articles}件 / 約{len(text)//CHARS_PER_MINUTE}分 — "
              f"1本で聴き切れるので分割しません。")
        if os.path.isdir(outdir):
            shutil.rmtree(outdir)  # 前回の分割が残っていると紛らわしい
        return

    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)

    print(f"全{total_articles}件 / 約{len(text)//CHARS_PER_MINUTE}分 — "
          f"{len(parts)}ファイルに分割します（1本 {max_minutes}分以内）")

    seen = 0
    for i, part in enumerate(parts, 1):
        count = len(ARTICLE_RE.findall(part))
        first, last = seen + 1, seen + count
        seen = last
        header = (f"これは全{total_articles}件のうち"
                  f"{first}件目から{last}件目までのパートです。"
                  f"番号順に、すべての記事を解説します。\n\n")
        name = f"{date}_パート{i}_{first}〜{last}件目.md"
        with open(os.path.join(outdir, name), "w", encoding="utf-8") as f:
            f.write(header + part + "\n")
        body = header + part
        print(f"  パート{i}: {count:3}件  {len(body):6}字  "
              f"約{len(body)//CHARS_PER_MINUTE:2}分  {name}")

    print(f"\n出力先: {outdir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="レポートのパス")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MAX_MINUTES,
                        help=f"1ファイルの上限（分）。既定 {DEFAULT_MAX_MINUTES}")
    args = parser.parse_args()
    run(args.path, args.minutes)
