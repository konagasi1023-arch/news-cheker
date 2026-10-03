"""
replace_notion_body.py - Notion のレポートページの本文を、vault の手直し済みの原稿に差し替える

点検で原稿を直したら、vault の全文 → 分割 → Notion の3か所を揃える。その Notion の分。
ページは作り直さない（作成時刻が次のレポートの切り取り位置なので動かさない）。

    python replace_notion_body.py <ページID> <原稿のパス> <件数>

差し替える前に原稿の番号（件数と連番）を確かめ、差し替えた後に読み戻して同じことを確かめる。
「重複」の検出は本文の普通の言い回しにも当たるので条件にしない（目で確かめてから差し替える）。

（2026-10-04 まで scratchpad に置いていた使い捨てを、消えたので repo に置き直した）
"""

import re
import sys

import main  # .env
import gemini_client
import notion_writer as nw


def run(page_id: str, path: str, expected: int) -> int:
    token, _ = nw.get_credentials()
    text = open(path, encoding="utf-8").read().strip()
    check = gemini_client.verify_report(text, expected)
    print("原稿の検算:", check)
    if not (check["numbers"] == expected and check["sequential"]):
        print("[中止] 番号の件数か連番が合わないので差し替えない")
        return 1
    res = nw.replace_report_body(token, page_id, text)
    print(f"追加: {res['added']}ブロック / {res['requests']}回")
    print(f"削除: {res['deleted']}ブロック")
    back = nw.read_report_text(token, page_id)
    check = gemini_client.verify_report(back, expected)
    print("読み戻しの検算:", check)
    print("番目の残り:", len(re.findall(r"(?m)^\S{0,6}番目。", back)), "/ 文字数:", len(back))
    return 0 if check["numbers"] == expected and check["sequential"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(2)
    sys.exit(run(sys.argv[1], sys.argv[2], int(sys.argv[3])))
