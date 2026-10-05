"""
filter_hunks.py - unified diff のうち、キーワードを含むハンクだけを残す（一部だけをコミットするため）

gemini_client.py には音声設定の未コミットの変更が長く残っているので、同じファイルの別の変更を
コミットするときは、必要なハンクだけをステージする。

    git diff -U3 gemini_client.py > all.patch
    python tools/filter_hunks.py all.patch keep.patch キーワード1 キーワード2 ...
    git apply --cached keep.patch

（2026-10-05 まで scratchpad に置いていて消えたので、repo に置き直した）
"""
import re
import sys

src, dst, *kws = sys.argv[1:]
text = open(src, encoding="utf-8").read()
header, *hunks = re.split(r"(?m)^(?=@@ )", text)
kept = [h for h in hunks if any(k in h for k in kws)]
print(f"hunks total={len(hunks)} kept={len(kept)}")
for h in kept:
    print("  ", h.splitlines()[0])
open(dst, "w", encoding="utf-8", newline="\n").write(header + "".join(kept))
