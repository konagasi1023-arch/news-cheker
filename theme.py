"""
vault のノートからテーマ横断で記事を集め、一覧ノートを作る。

2026-09-01 に「Googleの予測モデル」の記事を探したとき、ノート全文を検索すると
39件に膨らんで大半が無関係だった（本文に「予測」という語が出るだけの記事）。
frontmatter の title に対して「Google系の語」×「予測・モデル系の語」で絞ると
4件に収束した。その絞り方をそのまま道具にしている。

使い方:
    python theme.py "Google,Gemini,DeepMind" "予測,モデル,forecast" --name "Googleの予測モデル"
    python theme.py "Claude,Anthropic" "MCP,エージェント" --name "Claudeのエージェント" --body
    python theme.py ... --audio      # 音声原稿も作る
"""
import argparse
import os
import re
import sys
import unicodedata

VAULT = r"C:\Obsidian_Vault"
SUBDIR = os.path.join("News Checker")
THEME_DIR = os.path.join(SUBDIR, "テーマ")


def fold(text: str) -> str:
    """全角半角と大文字小文字の違いを無視して比べられる形にする"""
    return unicodedata.normalize("NFKC", text or "").lower()


def parse_note(path: str) -> dict:
    """frontmatter 付きノートを読む。読めないものは None を返す（黙って飛ばさない）"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    head, body = text[3:end], text[end + 4:]

    item = {"path": path, "title": "", "url": "", "category": "",
            "tags": [], "saved": "", "notion": "", "summary": [], "excerpt": ""}
    for line in head.split("\n"):
        line = line.strip()
        m = re.match(r'^(\w+):\s*(.*)$', line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val.startswith('"') and val.endswith('"') and len(val) > 1:
                val = val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            if key in ("title", "url", "source_url", "category", "saved", "notion"):
                item["source_url" if key == "source_url" else key] = val
        elif line.startswith("- "):
            tag = line[2:].strip().strip('"')
            if tag:
                item["tags"].append(tag)

    # 本文側から要約と抜粋を取り出す（音声原稿の材料になる）
    sec = re.search(r"## 要約\n\n(.*?)(?=\n## |\n🔗|\Z)", body, re.S)
    if sec:
        item["summary"] = [l[2:].strip() for l in sec.group(1).split("\n")
                           if l.startswith("- ")]
    sec = re.search(r"## 本文抜粋\n\n(.*?)(?=\n🔗|\Z)", body, re.S)
    item["excerpt"] = sec.group(1).strip() if sec else ""
    if not item["excerpt"]:
        # 要約が無いノートは見出しが付かず本文がそのまま入っている
        stripped = re.sub(r"^#.*$", "", body, flags=re.M).strip()
        item["excerpt"] = re.sub(r"🔗 \[元記事\].*$", "", stripped).strip()
    return item


def load_notes(vault: str) -> tuple:
    base = os.path.join(vault, SUBDIR)
    notes, unreadable = [], []
    for name in sorted(os.listdir(base)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(base, name)
        item = parse_note(path)
        if item is None or not item["title"]:
            unreadable.append(name)
            continue
        notes.append(item)
    return notes, unreadable


def matches(item: dict, groups: list, use_body: bool) -> bool:
    """語群すべてにひとつ以上当てはまるものを拾う（語群内はどれか1つでよい）"""
    haystack = fold(item["title"])
    if use_body:
        haystack += " " + fold(item["excerpt"]) + " " + fold(" ".join(item["tags"]))
    return all(any(fold(w) in haystack for w in group) for group in groups)


def build_note(name: str, hits: list, groups: list, use_body: bool) -> str:
    lines = ["---", f'title: "{name}"', "type: テーマ",
             f"count: {len(hits)}", "---", "", f"# {name}", "",
             f"語群: {' × '.join('（' + ' / '.join(g) + '）' for g in groups)}",
             f"探した範囲: {'題名＋本文＋タグ' if use_body else '題名のみ'}",
             f"該当: {len(hits)}件", ""]
    for i, h in enumerate(sorted(hits, key=lambda x: x["saved"], reverse=True), 1):
        note_name = os.path.splitext(os.path.basename(h["path"]))[0]
        lines.append(f"## {i}. {h['title']}")
        lines.append("")
        lines.append(f"- 保存日: {h['saved'] or '不明'} ／ カテゴリ: {h['category'] or '未分類'}")
        if h["tags"]:
            lines.append(f"- タグ: {' / '.join(h['tags'])}")
        lines.append(f"- ノート: [[{note_name}]]")
        link = h.get("source_url") or h.get("url")
        if link:
            lines.append(f"- 元記事: {link}")
        for s in h["summary"]:
            lines.append(f"- {s}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("groups", nargs="+",
                    help='語群。カンマ区切りで1群。例: "Google,Gemini" "予測,モデル"')
    ap.add_argument("--name", required=True, help="テーマ名（ファイル名になる）")
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--body", action="store_true",
                    help="題名だけでなく本文とタグも見る（件数は増えるが無関係も混ざる）")
    ap.add_argument("--audio", action="store_true", help="音声原稿も作る")
    args = ap.parse_args()

    groups = [[w.strip() for w in g.split(",") if w.strip()] for g in args.groups]
    notes, unreadable = load_notes(args.vault)
    print(f"ノート {len(notes)}件を読みました"
          + (f"（読めなかったもの {len(unreadable)}件）" if unreadable else ""))

    hits = [n for n in notes if matches(n, groups, args.body)]

    # 同じ記事が2回保存されていることがある。判定はレポート側と同じ規則を使う
    # （同じルールを2か所に書くと、片方だけ直して食い違う）。
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gemini_client
    hits, dropped = gemini_client.usable_articles(hits)
    print(f"該当 {len(hits)}件"
          f"（{'題名＋本文＋タグ' if args.body else '題名のみ'}で照合"
          + (f"／重複・本文なし {dropped}件を除外" if dropped else "") + "）")
    for h in sorted(hits, key=lambda x: x["saved"], reverse=True):
        print(f"  {h['saved'] or '        '}  {h['title'][:60]}")

    if not hits:
        print("\n該当なし。語群を緩めるか --body を付けて本文も見てください。")
        return 1

    theme_dir = os.path.join(args.vault, THEME_DIR)
    os.makedirs(theme_dir, exist_ok=True)
    out = os.path.join(theme_dir, f"{args.name}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_note(args.name, hits, groups, args.body))
    print(f"\n一覧ノート: {out}")

    if args.audio:
        import main as nc_main
        nc_main._load_dotenv()
        text = gemini_client.generate_report(hits, f"「{args.name}」について")
        audio = os.path.join(theme_dir, f"{args.name}_音声用.md")
        with open(audio, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"音声原稿: {audio}（{len(text)}字・読み上げ約{len(text)//350}分）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
