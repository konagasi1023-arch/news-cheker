"""
refetch_empty.py - 本文が取れなかった記事を、この PC から取り直して Notion に書き足す

BRIDGE（thebridge.jp）は Render から取ると0字になり、この PC からは取れる
（2026-09-25・09-27 の2件で確認）。サーバーのあるデータセンターからの取得を
拒否しているとみられる。同じ型のサイトは他にもありうるので、PC 側で拾い直す。
2026-09-28 にユーザーが選んだ対策。15分おきの同期（sync_task.cmd）から呼ぶ。

- 対象は vault のノートから探す（Notion の本文を1件ずつ読まずに済む）：
  直近 DAYS 日に保存され、本文が0字か題名だけのもの
- 取れたら分類し直して、既存の Notion ページに書き足す（日付が今日に進み、次のレポートに載る）
- 1回の実行は最大 MAX_PER_RUN 件・TIME_BUDGET 秒まで。同じ記事は MAX_ATTEMPTS 回で諦める
  （ログイン必須の Facebook などは何度やっても取れない）

    python refetch_empty.py            # 取り直して書き足す
    python refetch_empty.py --dry-run  # 取れるかだけ見る（書き込まない）
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import main  # .env の読み込みと fetch_meta
import gemini_client
import notion_writer
import theme

DAYS = 3
MAX_PER_RUN = 5
TIME_BUDGET = 240
MAX_ATTEMPTS = 3
MIN_CHARS = 100
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refetch_state.json")


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def candidates(vault: str, state: dict) -> list:
    since = (datetime.now(notion_writer.JST) - timedelta(days=DAYS)).strftime("%Y-%m-%d")
    notes, _ = theme.load_notes(vault)
    out = []
    for n in notes:
        url = n.get("url") or ""
        if not url.startswith("http") or (n.get("saved") or "") < since or not n.get("notion"):
            continue
        ex = (n.get("excerpt") or "").strip()
        if ex and not gemini_client.is_title_only(n):
            continue
        if state.get(url, {}).get("attempts", 0) >= MAX_ATTEMPTS or state.get(url, {}).get("done"):
            continue
        out.append(n)
    return sorted(out, key=lambda n: n.get("saved", ""), reverse=True)


def refetch(n: dict, token: str, dry_run: bool) -> str:
    """1件を取り直す。結果を短い文で返す"""
    url = n["url"]
    meta = main.fetch_meta(main.clean_url(url))
    body = (meta.get("body") or "").strip()
    note = meta.get("note", "")
    title = n.get("title") or ""
    if main._BAD_TITLE.match(title) or not title:
        title = meta.get("title") or ""
    if len(body) + len(note) < MIN_CHARS or gemini_client.is_title_only(
            {"title": title, "excerpt": body}):
        return f"取れず（{len(body)}字）"
    if dry_run:
        return f"取れる（{len(body)}字）"
    res = gemini_client.classify(title, url, "\n\n".join(p for p in (note, body) if p))
    if not title and res.get("ok"):
        title = res.get("article_title") or ""
    title = title or main._title_from_text(body) or n.get("title") or url
    page_id = notion_writer.page_id_of(n["notion"])
    cur = notion_writer.read_page(token, page_id)
    ok = res.get("ok")
    notion_writer.fill_page_body(
        token, page_id, title, res["category"] if ok else "", res["tags"] if ok else [],
        res["summary"] if ok else [], body, cur["blocks"])
    return f"書き足した（{len(body)}字・{res.get('category') if ok else '未分類'}）：{title[:40]}"


def run(vault: str, dry_run: bool) -> int:
    state = load_state()
    todo = candidates(vault, state)
    print(f"取り直しの対象: {len(todo)}件（直近{DAYS}日・本文なし・{MAX_ATTEMPTS}回未満）")
    token, _ = notion_writer.get_credentials()
    t0, done = time.time(), 0
    for n in todo[:MAX_PER_RUN]:
        if time.time() - t0 > TIME_BUDGET:
            print("  時間の上限に達したので次回へ")
            break
        url = n["url"]
        try:
            msg = refetch(n, token, dry_run)
        except Exception as e:
            msg = f"失敗 {type(e).__name__}: {str(e)[:120]}"
        print(f"  {msg} - {url[:80]}")
        if dry_run:
            continue
        st = state.setdefault(url, {"attempts": 0})
        st["attempts"] += 1
        st["last"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        if msg.startswith("書き足した"):
            st["done"] = True
            done += 1
        save_state(state)
    print(f"書き足した {done}件")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="本文が取れなかった記事を PC から取り直す")
    p.add_argument("--vault", default="C:/Obsidian_Vault")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    sys.exit(run(a.vault, a.dry_run))
