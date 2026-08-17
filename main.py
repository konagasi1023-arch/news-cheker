"""
main.py - News Cheker サーバー（FastAPI）

エンドポイント:
  POST /webhook  - ブックマークレットから URL を受信して Notion に保存
  GET  /save     - PWA シェアターゲット（Chrome 共有から URL を受信）
  GET  /health   - Render.com ヘルスチェック + UptimeRobot ping 用
  GET  /         - PWA ホームページ
  GET  /manifest.json - PWA マニフェスト
  GET  /sw.js    - サービスワーカー
  GET  /icon.svg - アプリアイコン

環境変数（.env または Render.com のダッシュボードで設定）:
  NOTION_TOKEN       - Notion インテグレーションのシークレットキー
  NOTION_DATABASE_ID - News Cheker DB の ID
"""

import os
import json
import re
import struct
import zlib
from urllib.parse import urlparse
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware


def _make_png(width: int, height: int, r: int, g: int, b: int) -> bytes:
    """標準ライブラリのみで単色 PNG を生成する"""
    def chunk(name: bytes, data: bytes) -> bytes:
        c = name + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    raw = b''.join(b'\x00' + bytes([r, g, b] * width) for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw))
            + chunk(b'IEND', b''))

import html as html_module
import urllib.request
import urllib.parse
from html.parser import HTMLParser
import notion_writer
import gemini_client

X_STATUS_RE = re.compile(
    r"https?://(?:x|twitter|mobile\.twitter)\.com/[^/]+/status(?:es)?/\d+"
)


def _get_json(url: str, timeout: int = 12) -> dict:
    """JSON を取得する。失敗したら None"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _x_from_oembed(url: str) -> dict:
    """X 公式 oEmbed から投稿を取得する（認証不要・公開ポストのみ）"""
    api = ("https://publish.twitter.com/oembed?omit_script=1&lang=ja&url="
           + urllib.parse.quote(url, safe=""))
    data = _get_json(api, timeout=10)
    if not data:
        return None

    text = ""
    m = re.search(r"<p[^>]*>(.*?)</p>", data.get("html", ""), re.DOTALL)
    if m:
        text = re.sub(r"<br\s*/?>", "\n", m.group(1))
        text = html_module.unescape(re.sub(r"<[^>]+>", "", text)).strip()

    author = data.get("author_name", "")
    if not (author or text):
        return None
    return {"author": author, "text": text, "links": []}


def _x_from_fxtwitter(status_id: str) -> dict:
    """
    fxtwitter から投稿を取得する。
    公式 oEmbed は長文投稿を 174 字ほどで打ち切るため、全文が要るときに使う。
    有志運営の無料サービスなので、落ちていても公式の結果で動くようにしておく。
    """
    data = _get_json(f"https://api.fxtwitter.com/status/{status_id}", timeout=15)
    if not data:
        return None
    tweet = data.get("tweet") or {}
    text = (tweet.get("text") or "").strip()
    if not text:
        return None

    links = []
    for key in ("url", "expanded_url"):
        for item in (tweet.get("media", {}) or {}).get("external", []) or []:
            if item.get(key):
                links.append(item[key])
    return {
        "author": (tweet.get("author") or {}).get("name", ""),
        "text": text,
        "links": links,
    }


def _expand_x_links(text: str) -> list:
    """投稿本文に含まれる t.co 以外の URL を拾う（記事リンクをたどるため）"""
    urls = []
    for u in re.findall(r"https?://[^\s　]+", text):
        u = u.rstrip("）)、。,")
        if "//t.co/" in u or "twitter.com" in u or "x.com" in u:
            continue
        if u not in urls:
            urls.append(u)
    return urls


def fetch_x_post(url: str) -> dict:
    """
    X（Twitter）のポストを取得する。

    公式 oEmbed を先に試し、長文投稿で打ち切られている場合だけ fxtwitter で全文を取る。
    投稿が記事へのリンクを含む場合は、その記事の本文も続けて読み込む。

    Returns:
        {"title": "投稿者: 本文冒頭", "description": 投稿本文（＋リンク先本文）}
    """
    m = X_STATUS_RE.match(url)
    if not m:
        return None
    status_id = url.rstrip("/").split("/")[-1].split("?")[0]

    post = _x_from_oembed(url)
    # 長文投稿は末尾が省略記号か「続きを読む」の t.co リンクになる。
    # どちらかに当てはまれば全文を取り直す。
    tail = (post or {}).get("text", "").rstrip()[-60:]
    truncated = not post or "…" in tail or "..." in tail or "//t.co/" in tail
    if truncated:
        full = _x_from_fxtwitter(status_id)
        if full and len(full["text"]) > len((post or {}).get("text", "")):
            post = full
    if not post:
        return None

    text = post["text"]
    first_line = next((l for l in text.split("\n") if l.strip()), "")
    author = post["author"]
    title = f"{author}: {first_line[:60]}" if author else first_line[:80]

    # 投稿が紹介している記事があれば、その本文も材料にする
    body = text
    for link in (_expand_x_links(text) + post["links"])[:2]:
        try:
            article = extract_article_body(_download_html(link))
        except Exception:
            article = ""
        if article:
            body += f"\n\n【リンク先記事: {link}】\n{article[:3000]}"

    return {"title": title.strip(" :"), "description": body}


def _extract_meta(html: str, prop_patterns: list) -> str:
    """
    メタタグの content を取得する。
    属性の順序が逆のパターンと、引用符の無い属性値（Forbes など）にも対応する。
    """
    # 引用符あり / 無し（次の属性の直前まで）の両方を拾う
    value = r'(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))'
    for attr, name in prop_patterns:
        for pattern in (
            rf'<meta[^>]+{attr}=["\']?{re.escape(name)}["\']?[^>]*?content={value}',
            rf'<meta[^>]+content={value}[^>]*?{attr}=["\']?{re.escape(name)}["\']?',
        ):
            m = re.search(pattern, html, re.IGNORECASE)
            if m:
                found = next((g for g in m.groups() if g), "")
                if found:
                    return found
    return ""


def _download_html(url: str, max_bytes: int = 400000, timeout: int = 12) -> str:
    """URL の HTML を charset を考慮して取得する。失敗時は空文字"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            charset_match = re.search(r"charset=([\w-]+)", content_type)
            charset = charset_match.group(1) if charset_match else None
            raw = resp.read(max_bytes)

        if not charset:
            meta_match = re.search(
                rb'<meta[^>]+charset=["\']?([\w-]+)', raw, re.IGNORECASE)
            charset = meta_match.group(1).decode("ascii", errors="ignore") if meta_match else "utf-8"

        return raw.decode(charset, errors="ignore")
    except Exception:
        return ""


SMARTNEWS_HOST_RE = re.compile(r"https?://(?:www\.|l\.)?smartnews\.com/")


def resolve_smartnews(url: str) -> dict:
    """
    SmartNews の記事ページから元記事の情報を取り出す。

    SmartNews のページは JS 描画で本文を持たないが、SSR された HTML に
    linkData（元記事URL・媒体名・著者）が JS オブジェクトとして埋まっている。

    Returns:
        {"url": 元記事URL, "site": 媒体名, "author": 著者名} / 見つからなければ None
    """
    if not SMARTNEWS_HOST_RE.match(url):
        return None
    html = _download_html(url, max_bytes=200000)
    if not html:
        return None

    m = re.search(r'linkData:\{[^{}]*?url:"(https?://[^"]+)"', html)
    if not m:
        return None
    site = re.search(r'site:\{name:"([^"]+)"', html)
    author = re.search(r'author:\{name:"([^"]+)"', html)
    return {
        "url": m.group(1),
        "site": site.group(1) if site else "",
        "author": author.group(1) if author else "",
    }


class _ArticleParser(HTMLParser):
    """
    段落テキストを、それが属するブロック要素ごとに集めるパーサー。

    ページ全体の <p> をまとめて拾うと、関連記事リンクやサイドバーが
    本文に混ざってしまう。本文は特定のコンテナに固まっているので、
    「配下の段落テキストが最も多いコンテナ」を本文とみなす。
    """

    # 本文が入りうるコンテナ
    CONTAINERS = {"div", "section", "article", "main", "td"}
    # 中身を捨てる要素
    SKIP = {"script", "style", "nav", "header", "footer", "aside", "form",
            "figure", "figcaption", "noscript", "select", "button"}
    # 段落として扱う要素
    BLOCKS = {"p", "h2", "h3", "h4", "li", "blockquote", "dd"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # 開いているコンテナの id
        self.blocks = {}         # コンテナ id -> [直下の段落テキスト]
        self.scores = {}         # コンテナ id -> 本文らしさのスコア
        self.children = {}       # コンテナ id -> [子コンテナ id]
        self.counter = 0
        self.skip_depth = 0
        self.current = None      # 収集中の段落テキスト
        self.link_chars = {}     # コンテナ id -> リンク内の文字数
        self.in_link = False

    def _flush(self):
        """収集中の段落を確定させる（</p> が省略された HTML にも対応するため）"""
        if self.current is None:
            return
        text = re.sub(r"\s+", " ", "".join(self.current)).strip()
        self.current = None
        # 短い行は見出しナビやクレジットのことが多い
        if len(text) < 25 or not self.stack:
            return
        # 段落は直近のコンテナに入れる。ただし本文が段落ごとに div で
        # 包まれている場合に備え、祖先にもスコアを配分する
        # （近いほど高く。readability と同じ考え方）
        self.blocks[self.stack[-1]].append(text)
        for depth, cid in enumerate(reversed(self.stack)):
            self.scores[cid] = self.scores.get(cid, 0) + len(text) / (depth + 1)
            if depth >= 4:
                break

    def handle_starttag(self, tag, attrs):
        # スキップ対象の入れ子だけを数える（他のタグでは増やさない）
        if tag in self.SKIP:
            self._flush()
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self.CONTAINERS:
            self._flush()
            self.counter += 1
            if self.stack:
                self.children.setdefault(self.stack[-1], []).append(self.counter)
            self.stack.append(self.counter)
            self.blocks[self.counter] = []
            self.link_chars[self.counter] = 0
        elif tag in self.BLOCKS:
            self._flush()
            self.current = []
        elif tag == "a":
            self.in_link = True

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in self.CONTAINERS:
            self._flush()
            if self.stack:
                self.stack.pop()
        elif tag in self.BLOCKS:
            self._flush()
        elif tag == "a":
            self.in_link = False

    def handle_data(self, data):
        if self.skip_depth or self.current is None:
            return
        self.current.append(data)
        if self.in_link and self.stack:
            self.link_chars[self.stack[-1]] += len(data.strip())

    def _collect(self, cid: int) -> list:
        """コンテナ配下の段落を出現順に集める"""
        texts = list(self.blocks.get(cid, []))
        for child in self.children.get(cid, []):
            texts.extend(self._collect(child))
        return texts

    def _link_chars_deep(self, cid: int) -> int:
        """コンテナ配下のリンク文字数を合計する"""
        total = self.link_chars.get(cid, 0)
        for child in self.children.get(cid, []):
            total += self._link_chars_deep(child)
        return total

    def best_text(self) -> str:
        """本文らしさが最も高いコンテナのテキストを返す"""
        best, best_score = "", 0.0
        for cid, score in self.scores.items():
            texts = self._collect(cid)
            total = sum(len(t) for t in texts)
            if total < 200:
                continue
            # リンクだらけのコンテナは関連記事一覧なので減点する
            link_ratio = self._link_chars_deep(cid) / max(total, 1)
            adjusted = score * (1 - min(link_ratio, 1.0))
            if adjusted > best_score:
                best_score, best = adjusted, "\n".join(texts)
        return best


def extract_article_body(html: str) -> str:
    """
    記事ページの HTML から本文テキストを抽出する。
    JSON-LD の articleBody があればそれを使い、無ければ本文密度で判定する。
    """
    # 1. JSON-LD の articleBody（最も正確）
    for m in re.finditer(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict):
                body = item.get("articleBody")
                if isinstance(body, str) and len(body) > 200:
                    return re.sub(r"\s+", " ", html_module.unescape(body)).strip()

    # 2. 本文密度で判定する
    try:
        parser = _ArticleParser()
        parser.feed(html)
        return parser.best_text()
    except Exception:
        return ""


def fetch_meta(url: str) -> dict:
    """
    URL からタイトル・本文抜粋・本文を取得する。
    X のポストは oEmbed、SmartNews は元記事を解決してから本文を取る。

    Returns:
        {"title", "description", "body", "original_url", "site"}
        （取れなかった項目は空文字）
    """
    empty = {"title": "", "description": "", "body": "", "original_url": "", "site": ""}

    x_post = fetch_x_post(url)
    if x_post:
        return {**empty, **x_post, "body": x_post["description"]}

    # SmartNews は元記事に差し替えて取得する
    original_url, site = "", ""
    target = url
    sn = resolve_smartnews(url)
    if sn:
        target = original_url = sn["url"]
        site = sn["site"]

    html = _download_html(target)
    if not html:
        return {**empty, "original_url": original_url, "site": site}

    # OGタグ → twitter:title → titleタグの順で取得
    raw_title = _extract_meta(html, [
        ("property", "og:title"), ("name", "twitter:title")])
    if not raw_title:
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        raw_title = title_match.group(1) if title_match else ""

    description = _extract_meta(html, [
        ("property", "og:description"), ("name", "description")])

    title = re.sub(r"\s+", " ", html_module.unescape(raw_title.strip())).strip()
    description = html_module.unescape(description.strip())
    body = extract_article_body(html)

    return {"title": title, "description": description[:1000],
            "body": body, "original_url": original_url, "site": site}


def fetch_title(url: str) -> str:
    """URLにアクセスしてタイトルを取得する（backfill.py 等との互換用）"""
    return fetch_meta(url)["title"]


# URL からトラッキング用パラメータを除去する（重複検知の精度向上のため）
TRACKING_KEYS = {"fbclid", "gclid", "yclid", "igshid", "igsh", "mc_cid", "mc_eid"}


def clean_url(url: str) -> str:
    """utm_* などのトラッキングパラメータを取り除いた URL を返す"""
    try:
        parts = urllib.parse.urlsplit(url)
        if not parts.query:
            return url
        is_x = parts.netloc.endswith(("x.com", "twitter.com"))
        kept = [
            (k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if not k.startswith("utm_")
            and k not in TRACKING_KEYS
            and not (is_x and k in ("s", "t"))
        ]
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path,
             urllib.parse.urlencode(kept), parts.fragment))
    except Exception:
        return url

# .env ファイルがある場合は自動読み込み
def _load_dotenv():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

_load_dotenv()

app = FastAPI(title="News Cheker", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# PWA ファイル
# ---------------------------------------------------------------------------

@app.get("/manifest.json")
async def manifest():
    data = {
        "name": "News Cheker",
        "short_name": "News Cheker",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#1a1a2e",
        "theme_color": "#4285f4",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}
        ],
        "share_target": {
            "action": "/save",
            "method": "GET",
            "params": {
                "title": "title",
                "text": "text",
                "url": "url"
            }
        }
    }
    return Response(content=json.dumps(data), media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    js = "self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));"
    return Response(content=js, media_type="application/javascript")


@app.get("/icon-192.png")
async def icon_192():
    return Response(content=_make_png(192, 192, 66, 133, 244), media_type="image/png")


@app.get("/icon-512.png")
async def icon_512():
    return Response(content=_make_png(512, 512, 66, 133, 244), media_type="image/png")


@app.get("/icon.svg")
async def icon():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="20" fill="#4285f4"/><text x="50" y="72" font-size="64" font-family="sans-serif" font-weight="bold" text-anchor="middle" fill="white">N</text></svg>'
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/")
async def index():
    html = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>News Cheker</title>
  <link rel="manifest" href="/manifest.json">
  <meta name="theme-color" content="#4285f4">
  <style>
    body { font-family: sans-serif; text-align: center; padding: 60px 20px; background: #1a1a2e; color: #fff; }
    h1 { font-size: 2rem; margin-bottom: 8px; }
    p { color: #aaa; }
  </style>
</head>
<body>
  <h1>📰 News Cheker</h1>
  <p>Chromeで記事を共有すると Notion に保存されます。</p>
  <script>
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js');
    }
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# 保存エンドポイント
# ---------------------------------------------------------------------------

def _fallback_title(url: str) -> str:
    """タイトルが空のときURLのドメインで代替する"""
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


def save_article(url: str, title_hint: str = "", context_hint: str = "") -> dict:
    """
    記事を取得・分類して Notion に保存する（/webhook と /save の共通処理）。

    重複 URL は保存せず既存ページを返す。分類・要約は失敗しても保存を止めない。

    Args:
        url:          保存するURL（呼び出し側で clean_url 済みであること）
        title_hint:   共有側から渡されたタイトル（あれば優先）
        context_hint: 共有側から渡された本文テキスト（SNSポスト本文など）

    Returns:
        {"duplicate": bool, "notion_url": str, "title": str,
         "category": str, "tags": list, "summary": list}
    """
    token, database_id = notion_writer.get_credentials()

    # 重複チェック：同じURLが既にあれば保存しない
    try:
        existing = notion_writer.find_by_url(url, token, database_id)
    except Exception:
        existing = ""  # 照会に失敗しても保存は続行する
    if existing:
        return {"duplicate": True, "notion_url": existing, "title": title_hint,
                "category": "", "tags": [], "summary": []}

    # タイトルと本文を取得（X は oEmbed、SmartNews は元記事を解決して本文まで取る）
    meta = fetch_meta(url)
    title = title_hint or meta["title"] or _fallback_title(url)
    # 要約の材料は 本文 > 共有テキスト > description の順に良い
    context = meta["body"] or context_hint or meta["description"]

    # 分類できなかった場合はカテゴリを空のままにしておく。
    # 「その他」と書いてしまうと、後から未分類のページを見分けられなくなるため。
    category, tags, summary = "", [], []
    try:
        notion_writer.ensure_properties(token, database_id)
        result = gemini_client.classify(title, url, context)
        if result["ok"]:
            category, tags, summary = result["category"], result["tags"], result["summary"]
        else:
            print(f"[WARN] 分類できませんでした（未分類で保存します）: {title[:40]}")
    except Exception as e:
        print(f"[WARN] 分類をスキップしました: {type(e).__name__}: {e}")

    notion_url = notion_writer.save_to_notion(
        url, title, token, database_id,
        category=category, tags=tags, summary=summary,
        excerpt=meta["body"], original_url=meta["original_url"],
    )
    return {"duplicate": False, "notion_url": notion_url, "title": title,
            "category": category, "tags": tags, "summary": summary}


@app.get("/save")
async def save_from_share(url: str = "", title: str = "", text: str = ""):
    """PWA シェアターゲット：Chrome の共有から URL を受信して Notion に保存"""
    # urlが空の場合、textからURLを抽出（SmartNews等はURLをtextに埋め込む）
    if not url and text:
        match = re.search(r'https?://\S+', text)
        if match:
            url = match.group()
    # 共有テキストのURL以外の部分は本文（SNSポスト等）として要約の材料に使う
    shared_text = re.sub(r'https?://\S+', '', text).strip(" -\n") if text else ""
    if not title and shared_text:
        title = shared_text[:100]
    if not url:
        return HTMLResponse(content="<html><body><p>URLが指定されていません</p></body></html>")

    try:
        result = save_article(clean_url(url), title, shared_text)
        category, tags = result["category"], result["tags"]
        title = result["title"]
        if result["duplicate"]:
            return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"><title>News Cheker</title></head>
<body style="font-family:sans-serif;text-align:center;padding:40px 20px;background:#1a1a2e;color:#fff;">
<h2>📌 既に保存済みです</h2><p style="word-break:break-all;opacity:0.8;">{url}</p>
<script>setTimeout(()=>window.close(),3000);</script>
</body></html>""")
    except RuntimeError as e:
        return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"><title>エラー</title></head>
<body style="font-family:sans-serif;text-align:center;padding:60px;background:#1a1a2e;color:#fff;">
<h2>❌ 保存に失敗しました</h2><p>{e}</p>
</body></html>""")

    meta = f"{category}｜{' · '.join(tags)}" if category else ""
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"><title>News Cheker</title>
<meta name="theme-color" content="#4285f4">
<style>body{{font-family:sans-serif;text-align:center;padding:40px 20px;background:#1a1a2e;color:#fff;}}
p{{word-break:break-all;font-size:0.9em;opacity:0.8;}}
.meta{{color:#4285f4;font-size:0.85em;margin-top:12px;}}</style>
</head><body>
<h2>✅ 保存しました</h2>
<p>{title}</p>
<p class="meta">{meta}</p>
<script>setTimeout(()=>window.close(),3000);</script>
</body></html>""")


@app.post("/webhook")
async def webhook(request: Request):
    """ブックマークレットから URL を受信して Notion に保存"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="リクエストボディが JSON ではありません")

    raw_url = (body.get("url") or "").strip()
    raw_text = (body.get("text") or "").strip()
    title = (body.get("title") or "").strip()

    # 共有アプリによっては url 欄に「ポスト本文＋URL」が丸ごと入るため、
    # URLを正規表現で抽出し、残りは本文（要約の材料）として扱う
    combined = f"{raw_url}\n{raw_text}".strip()
    match = re.search(r'https?://\S+', combined)
    if not match:
        raise HTTPException(status_code=400, detail=f"URL が見つかりません: {combined[:100]}")
    url = clean_url(match.group())
    shared_text = re.sub(r'https?://\S+', '', combined).strip(" -\n")

    try:
        result = save_article(url, title, shared_text)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"Notion API エラー: {e}")

    if result["duplicate"]:
        print(f"[SKIP] 重複のため保存せず: {url}")
        return JSONResponse(content={
            "status": "duplicate", "notion_url": result["notion_url"], "url": url})

    print(f"[OK] 保存完了: [{result['category']}] {result['title']} - {url}")
    return JSONResponse(content={
        "status": "ok",
        "title": result["title"],
        "category": result["category"],
        "tags": result["tags"],
        "summary": result["summary"],
        "notion_url": result["notion_url"],
    })


# ---------------------------------------------------------------------------
# 日次・週次レポート
# ---------------------------------------------------------------------------

REPORT_PERIODS = {
    "daily": (1, "今日", "日次"),
    "weekly": (7, "今週", "週次"),
}


@app.get("/report/{period}")
async def generate_report(period: str, token: str = "", days: int = 0):
    """
    保存記事のふりかえりレポートを生成して Notion に保存する。
    GitHub Actions 等の定期実行から叩く想定。REPORT_TOKEN で保護する。

    days を指定すると期間を上書きできる（レポートし損ねた日をまとめて拾うとき用）。
    """
    expected = os.environ.get("REPORT_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="REPORT_TOKEN が設定されていません")
    if token != expected:
        raise HTTPException(status_code=403, detail="token が一致しません")
    if period not in REPORT_PERIODS:
        raise HTTPException(status_code=404, detail=f"不明な期間です: {period}")

    default_days, label, kind = REPORT_PERIODS[period]
    if days:
        if not 1 <= days <= 31:
            raise HTTPException(status_code=400, detail="days は 1〜31 で指定してください")
        label = f"この{days}日間に"
    days = days or default_days
    notion_token, database_id = notion_writer.get_credentials()

    articles = notion_writer.fetch_recent_articles(notion_token, database_id, days)
    if not articles:
        return JSONResponse(content={"status": "empty", "message": f"{label}の保存記事はありません"})

    try:
        report_text = gemini_client.generate_report(articles, label)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"レポート生成エラー: {e}")

    date_str = notion_writer.datetime.now(notion_writer.JST).strftime("%Y-%m-%d")
    title = f"📊 {kind}レポート {date_str}（{len(articles)}件）"
    notion_url = notion_writer.save_report(title, report_text, notion_token, database_id)

    print(f"[OK] レポート作成: {title}")
    return JSONResponse(content={
        "status": "ok", "title": title,
        "articles": len(articles), "notion_url": notion_url,
    })


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
