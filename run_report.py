"""
run_report.py - 保存記事から音声用レポートを作るまでを一本で通すスクリプト。

これまでレポートはその場限りのスクリプトを組み立てて作っていた。
手順が残らないので、前処理を入れ忘れたり、除外の数え方が回ごとに
変わったりする。毎回まったく同じ順で走るようにここへ固定する。

    分類の拾い直し
      → 直近レポート以降の記事を取得（日付ではなく作成時刻で切る）
      → 材料の無い記事と重複を落とす
      → カテゴリ別に生成（検算つき）
      → 元記事の本文と照合（明らかな誤記は直し、疑いは vault の「…_照合.md」に書く）
      → Notion に保存
      → vault に音声用テキストを書き出す
      → 38分ごとに分割（1パート14件前後。上限は split_report.DEFAULT_MAX_MINUTES）

使い方:
    python -u run_report.py --dry-run     # 生成せず対象だけ数える
    python -u run_report.py               # 通しで実行する
    python -u run_report.py --minutes 30  # 分割を30分区切りにする

70件を超えると生成だけで10分以上かかる。`python -u` を付けて
途中経過を出しながら、バックグラウンドで走らせること。
"""

import argparse
import os
import sys
import time
from datetime import datetime

import main  # .env の読み込みのため
import backfill  # write_classification を共用する
import fact_check
import gemini_client
import notion_writer
import split_report

DEFAULT_VAULT = "C:/Obsidian_Vault"
REPORTS_SUBDIR = "News Checker/Reports"

# Notion から引く日数。ここは広めでよい。実際の切り取りは
# 直近レポートの作成時刻で行うので、広げても対象は増えない。
LOOKBACK_DAYS = 14

# 分類し直すときの待ち時間（バックフィルと同じ間隔にする）
SLEEP_BETWEEN = 1.0

# 記事の作成日がこの日数以上にまたがったら「日次」ではなく「まとめ」と呼ぶ
MULTI_DAY_THRESHOLD = 3


def latest_report_time(token: str, database_id: str) -> dict:
    """
    直近レポートの作成時刻を返す。

    見つからなかったのか、引きに行けなかったのかを呼び出し側が
    区別できるように、成否を添えて返す。失敗を「レポートが無い」と
    同じ扱いにすると、全期間を黙って作り直してしまう。
    """
    try:
        res = notion_writer.notion_request(
            "POST", f"/databases/{database_id}/query", token,
            {
                "filter": {"property": notion_writer.CATEGORY_PROPERTY,
                           "select": {"equals": notion_writer.REPORT_CATEGORY}},
                "sorts": [{"timestamp": "created_time", "direction": "descending"}],
                "page_size": 1,
            })
    except Exception as e:
        return {"ok": False, "cutoff": "",
                "reason": f"直近レポートを引けなかった: {type(e).__name__}: {str(e)[:120]}"}

    results = res.get("results", [])
    if not results:
        return {"ok": True, "cutoff": "", "reason": "レポートがまだ1件も無い"}
    return {"ok": True, "cutoff": results[0].get("created_time", ""), "reason": ""}


def reclassify(articles: list, token: str, dry_run: bool) -> dict:
    """
    対象記事のうち分類が空のものを、保存済み本文で分類し直す。

    保存時の分類は通信の失敗やモデルの壊れた JSON で一時的に落ちる。
    そのままだと「その他」扱いでレポートに入る。本文はもう手元にあるので、
    Webから取り直さずにそれを材料にする。

    見るのはレポート期間内だけ。DB全体の拾い直しは
    `python backfill.py --unclassified` を別途回すこと。
    """
    targets = [a for a in articles if not a.get("category")]
    stats = {"targets": len(targets), "classified": 0,
             "no_material": 0, "failed": 0, "quota": False}
    if not targets:
        print("[拾い直し] 未分類なし")
        return stats

    print(f"[拾い直し] 未分類 {len(targets)}件")
    for i, a in enumerate(targets, 1):
        context = (a.get("excerpt") or "").strip()
        if not context:
            # 本文が0字。何度試しても分類できないので、対象から外して数える
            stats["no_material"] += 1
            print(f"  [{i}/{len(targets)}] 材料なし: {a['title'][:45]}")
            continue
        if dry_run:
            print(f"  [{i}/{len(targets)}] 対象（本文{len(context)}字）: {a['title'][:45]}")
            continue

        result = gemini_client.classify(a["title"], a.get("url", ""), context)
        if result.get("quota_exceeded"):
            stats["quota"] = True
            print("  Gemini の割り当てを使い切ったため、拾い直しを中断します。")
            break
        if not result["ok"]:
            stats["failed"] += 1
            print(f"  [{i}/{len(targets)}] 分類できず: {a['title'][:45]}")
            time.sleep(SLEEP_BETWEEN)
            continue

        try:
            backfill.write_classification(
                token, a["id"], a.get("url", ""), a["title"], result)
        except Exception as e:
            stats["failed"] += 1
            print(f"  [{i}/{len(targets)}] 書き込み失敗: {type(e).__name__}: {str(e)[:80]}")
            time.sleep(SLEEP_BETWEEN)
            continue

        # 書き戻せたので、このあとの生成にも新しい分類を使う
        a["category"] = result["category"]
        a["tags"] = result["tags"]
        if result["summary"] and not a.get("summary"):
            a["summary"] = result["summary"]
        stats["classified"] += 1
        print(f"  [{i}/{len(targets)}] [{result['category']}] {a['title'][:45]}")
        time.sleep(SLEEP_BETWEEN)

    return stats


def describe(articles: list) -> None:
    """対象の内訳を出す。本文取得率は媒体側の形式変更に気づく手がかりになる"""
    with_body = sum(1 for a in articles if (a.get("excerpt") or "").strip())
    rate = with_body * 100 // max(len(articles), 1)
    print(f"本文あり {with_body}/{len(articles)}件（{rate}%）")
    counts = {}
    for a in articles:
        name = a.get("category") or "（未分類）"
        counts[name] = counts.get(name, 0) + 1
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {name}: {n}件")


def span_days(articles: list) -> int:
    """記事の作成日が何日ぶんにまたがっているか（日本時間で数える）"""
    days = set()
    for a in articles:
        saved_at = notion_writer.parse_time(a.get("created_time"))
        if saved_at:
            days.add(saved_at.astimezone(notion_writer.JST).strftime("%Y-%m-%d"))
    return len(days)


def unique_path(path: str) -> str:
    """同じ日に2本目を作るとき、前のレポートを上書きしない"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    tail = "_音声用"
    base = stem[:-len(tail)] if stem.endswith(tail) else stem
    for suffix in ["_夜"] + [f"_{n}" for n in range(2, 20)]:
        candidate = f"{base}{suffix}{tail}{ext}"
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError(f"書き出し先が決められません: {path}")


MISSING_NOTE = "News Checker/本文が取れなかった記事.md"
MISSING_DAYS = 14


def write_missing_list(vault: str) -> int:
    """
    直近 MISSING_DAYS 日で、本文が取れなかった記事（0字・題名だけ）の一覧を作り直す。

    LinkedIn・Facebook はログインが必要で、リンクだけの共有では本文が取れない。
    投稿の本文をコピーして「本文つき」で共有し直すと、既存ページに本文が書き足され
    （main._fill_existing）、次の同期でこの一覧から消える。2026-09-26 にユーザーが選んだ対策。
    vault のノートから作るので Notion は読まない（翌朝8:00の同期までの遅れはある）。
    """
    import theme
    from collections import defaultdict
    from datetime import timedelta
    from urllib.parse import urlparse

    since = (datetime.now(notion_writer.JST) - timedelta(days=MISSING_DAYS)).strftime("%Y-%m-%d")
    notes, _ = theme.load_notes(vault)
    groups = defaultdict(list)
    for n in notes:
        url = n.get("url") or ""
        if not url.startswith("http") or (n.get("saved") or "") < since:
            continue
        ex = (n.get("excerpt") or "").strip()
        if ex and not gemini_client.is_title_only(n):
            continue
        host = urlparse(url).netloc.replace("www.", "")
        label = ("LinkedIn" if ("lnkd.in" in host or "linkedin" in host) else
                 "Facebook" if "facebook" in host else
                 "X（Twitter）" if host in ("x.com", "twitter.com") else "その他のサイト")
        groups[label].append(n)
    total = sum(len(v) for v in groups.values())
    lines = ["---", 'title: "本文が取れなかった記事"', f"updated: {datetime.now():%Y-%m-%d %H:%M}",
             f"count: {total}", "---", "", "# 本文が取れなかった記事", "",
             f"直近{MISSING_DAYS}日に保存した記事のうち、本文が0字か題名だけのもの。レポートには入っていない。",
             "入れ直すには、リンクを開いて**投稿の本文をコピー**し、Android の共有から",
             "**「News Cheker（本文つき）」**で共有する。既存のページに本文が書き足され、次のレポートに載る。",
             "（入れ直した記事は、翌朝の同期のあとこの一覧から消える）", ""]
    for label in ("LinkedIn", "Facebook", "X（Twitter）", "その他のサイト"):
        items = sorted(groups.get(label, []), key=lambda n: n.get("saved", ""), reverse=True)
        if not items:
            continue
        lines += [f"## {label}（{len(items)}件）", ""]
        for n in items:
            note = os.path.splitext(os.path.basename(n["path"]))[0]
            lines.append(f"- {n.get('saved', '')} [{n['title'][:60]}]({n['url']}) — [[{note}|ノート]]")
        lines.append("")
    path = os.path.join(vault, MISSING_NOTE)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n本文が取れなかった記事（直近{MISSING_DAYS}日）: {total}件 → {path}")
    return total


def run(args) -> int:
    token, database_id = notion_writer.get_credentials()

    if args.since:
        cutoff, reason = args.since, "指定された時刻"
    else:
        found = latest_report_time(token, database_id)
        if not found["ok"]:
            print(f"[中止] {found['reason']}")
            print("  --since 2026-09-07T21:05:00+09:00 のように時刻を指定するか、")
            print("  Notion 側を確認してから再実行してください。")
            return 1
        cutoff = found["cutoff"]
        reason = found["reason"] or "直近レポートの作成時刻"

    cutoff_at = notion_writer.parse_time(cutoff)
    if cutoff and not cutoff_at:
        print(f"[中止] 切り取り時刻を読めません: {cutoff}")
        return 1
    shown = cutoff_at.astimezone(notion_writer.JST).strftime("%Y-%m-%d %H:%M") \
        if cutoff_at else "（無し・全期間）"
    print(f"切り取り: {shown} — {reason}")

    articles = notion_writer.fetch_recent_articles(
        token, database_id, args.days, created_after=cutoff)
    undated = [a for a in articles
               if not notion_writer.parse_time(a.get("created_time"))]
    if undated:
        # 落とさず残してある。黙って消すと1本失うため、代わりに知らせる
        print(f"[注意] 作成時刻を読めない記事が {len(undated)}件。対象に入れています")
    print(f"切り取り後 {len(articles)}件")
    if not articles:
        print("対象の記事がありません。")
        return 0

    if not args.skip_reclassify:
        stats = reclassify(articles, token, args.dry_run)
        if stats["targets"] and not args.dry_run:
            print(f"[拾い直し] 分類できた {stats['classified']} / "
                  f"材料なし {stats['no_material']} / 失敗 {stats['failed']}")
        if stats["quota"]:
            # ここで割り当てが尽きているなら、生成も必ず失敗する。
            # 半端なレポートを作る前に止めて、明日やり直せるようにする
            print("[中止] Gemini の割り当てが残っていません。時間をおいて再実行してください。")
            return 1

    usable, dropped = gemini_client.usable_articles(articles)
    print(f"対象 {len(usable)}件（落とした {dropped}件）")
    # 落としたものは必ず名前つきで出す。件数だけでは、本文のある記事が
    # 消えていても気づけない（2026-09-15 に2件が黙って消えた）。
    kept = {id(a) for a in usable}
    unreadable = 0
    for a in articles:
        if id(a) in kept:
            continue
        if a.get("read_failed"):
            why, unreadable = "本文を読めなかった", unreadable + 1
        elif not (a.get("excerpt") or "").strip():
            why = "本文なし"
        elif gemini_client.is_title_only(a):
            why = "題名だけ"
        else:
            why = "重複"
        print(f"  落とした（{why}）: {a['title'][:50]}")
    if unreadable:
        # 読めなかった記事は材料があるかもしれない。落としたまま作ると
        # その記事は次回も拾われず、永久に消える。作らずに止める。
        print(f"[中止] 本文を読めなかった記事が {unreadable}件あります。"
              "時間をおいて再実行してください。")
        return 1
    describe(usable)
    if not usable:
        print("解説できる材料のある記事がありません。")
        return 0

    kind = args.kind or ("まとめ" if span_days(usable) >= MULTI_DAY_THRESHOLD else "日次")
    label = "今日" if kind == "日次" else "この期間に"

    if args.dry_run:
        chars = sum(len(a.get("excerpt") or "") for a in usable)
        print("\n（--dry-run のため生成しません）")
        print(f"  種別: {kind}レポート / 材料 {chars:,}字")
        return 0

    print(f"\n{kind}レポートを生成します（{len(usable)}件・10〜20分かかります）")
    report_text = gemini_client.generate_report(usable, label)

    date_str = datetime.now(notion_writer.JST).strftime("%Y-%m-%d")

    # 元記事との照合（失敗してもレポートは止めない）。n番目の記事＝n件目の対応は
    # generate_report と同じ report_order で取る
    checked = None
    if not args.no_check:
        try:
            t0 = time.time()
            checked = fact_check.check(report_text, usable, gemini_client.report_order(usable))
            report_text, fixed, held = fact_check.apply_typos(report_text, checked)
            print(f"照合 {sum(r['checked'] for r in checked)}/{len(checked)}件（{time.time() - t0:.0f}秒）"
                  f"：誤記を直した {len(fixed)}件／直さず点検に回す {len(held)}件／"
                  f"本文に根拠なしの語がある記事 {sum(bool(r['unsupported']) for r in checked)}件")
            for f in fixed:
                print(f"  {f['no']}件目 {f['wrong']} → {f['right']}")
        except Exception as e:
            print(f"[注意] 照合できなかった: {type(e).__name__}: {str(e)[:200]}")
            checked = None
    title = f"📊 {kind}レポート {date_str}（{len(usable)}件）"

    # 原稿は生成に20分・Gemini 20回を使う。**Notion より先に vault へ書く。**
    # 以前は Notion の保存が先で、そこで落ちると原稿ごと消えた（2026-09-19、413）。
    outdir = os.path.join(args.vault, REPORTS_SUBDIR)
    os.makedirs(outdir, exist_ok=True)
    path = unique_path(os.path.join(outdir, f"{kind}レポート_{date_str}_音声用.md"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_text + "\n")
    minutes = len(report_text) // split_report.CHARS_PER_MINUTE
    print(f"\n{path}\n{len(report_text):,}字（読み上げ約{minutes}分）\n")

    split_report.run(path, args.minutes)

    if checked is not None:
        note = path.replace("_音声用.md", "_照合.md")  # レポートと対にする（2本目は _夜 も揃う）
        fact_check.write_note(note, date_str, checked, fixed, held)
        print(f"照合の結果: {note}")

    # 取りこぼしの一覧（失敗してもレポートは止めない）
    try:
        write_missing_list(args.vault)
    except Exception as e:
        print(f"[注意] 取りこぼしの一覧を作れなかった: {type(e).__name__}: {e}")

    # Notion のページは次回の切り取り位置にもなる。保存に失敗したら、次回は
    # 同じ記事がもう一度対象に入る（取りこぼすより安全）。原稿は vault に残っている。
    try:
        notion_url = notion_writer.save_report(title, report_text, token, database_id)
    except Exception as e:
        print(f"\n[注意] Notion への保存に失敗: {type(e).__name__}: {str(e)[:200]}")
        print(f"  原稿と分割は vault に保存済み: {path}")
        return 1
    print(f"\n{title}\n{notion_url}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="音声用レポートを通しで作る")
    parser.add_argument("--no-check", action="store_true",
                        help="元記事との照合を飛ばす")
    parser.add_argument("--dry-run", action="store_true",
                        help="生成せず、対象件数と内訳だけ出す")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS,
                        help=f"Notion から引く日数。既定 {LOOKBACK_DAYS}")
    parser.add_argument("--since", default="",
                        help="切り取り時刻を直接指定する（例 2026-09-07T21:05:00+09:00）")
    parser.add_argument("--minutes", type=int, default=split_report.DEFAULT_MAX_MINUTES,
                        help=f"分割の上限（分）。既定 {split_report.DEFAULT_MAX_MINUTES}")
    parser.add_argument("--kind", default="", choices=["", "日次", "まとめ"],
                        help="題名の種別。既定は日数から判定する")
    parser.add_argument("--vault", default=DEFAULT_VAULT, help="Obsidian vault のパス")
    parser.add_argument("--skip-reclassify", action="store_true",
                        help="分類の拾い直しをしない")
    sys.exit(run(parser.parse_args()))
