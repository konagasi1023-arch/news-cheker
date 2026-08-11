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
import notion_writer
import gemini_client

X_STATUS_RE = re.compile(
    r"https?://(?:x|twitter|mobile\.twitter)\.com/[^/]+/status(?:es)?/\d+"
)


def fetch_x_post(url: str) -> dict:
    """
    X（Twitter）のポストを公式 oEmbed API で取得する（認証不要・公開ポストのみ）。
    取得できたら {"title": "投稿者: 本文冒頭", "description": ポスト全文} を返す。
    """
    if not X_STATUS_RE.match(url):
        return None
    api = ("https://publish.twitter.com/oembed?omit_script=1&lang=ja&url="
           + urllib.parse.quote(url, safe=""))
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    author = data.get("author_name", "")
    text = ""
    m = re.search(r"<p[^>]*>(.*?)</p>", data.get("html", ""), re.DOTALL)
    if m:
        text = re.sub(r"<br\s*/?>", "\n", m.group(1))
        text = html_module.unescape(re.sub(r"<[^>]+>", "", text)).strip()

    if not (author or text):
        return None

    first_line = text.split("\n")[0] if text else ""
    title = f"{author}: {first_line[:60]}" if author else first_line[:80]
    return {"title": title.strip(" :"), "description": text[:1000]}


def _extract_meta(html: str, prop_patterns: list) -> str:
    """メタタグの content を取得する（属性順が逆のパターンにも対応）"""
    for attr, name in prop_patterns:
        m = re.search(
            rf'<meta[^>]+{attr}=["\']{name}["\'][^>]+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE)
        if not m:
            m = re.search(
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+{attr}=["\']{name}["\']',
                html, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def fetch_meta(url: str) -> dict:
    """
    URL からタイトルと本文抜粋（description）を取得する。
    X のポストは oEmbed で本文ごと取得する。

    Returns:
        {"title": str, "description": str}（失敗時はどちらも空文字）
    """
    x_post = fetch_x_post(url)
    if x_post:
        return x_post

    empty = {"title": "", "description": ""}
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            # charsetをレスポンスヘッダーから取得
            content_type = resp.headers.get("Content-Type", "")
            charset_match = re.search(r"charset=([\w-]+)", content_type)
            charset = charset_match.group(1) if charset_match else None
            raw = resp.read(65536)

        # charsetがヘッダーにない場合はmetaタグから取得
        if not charset:
            meta_match = re.search(
                rb'<meta[^>]+charset=["\']?([\w-]+)', raw, re.IGNORECASE
            )
            charset = meta_match.group(1).decode("ascii", errors="ignore") if meta_match else "utf-8"

        html = raw.decode(charset, errors="ignore")

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
        return {"title": title, "description": description[:1000]}
    except Exception:
        return empty


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

    # タイトルと本文抜粋を取得（X は oEmbed でポスト本文ごと取れる）
    meta = fetch_meta(url)
    title = title_hint or meta["title"] or _fallback_title(url)
    context = context_hint or meta["description"]

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
async def generate_report(period: str, token: str = ""):
    """
    保存記事のふりかえりレポートを生成して Notion に保存する。
    GitHub Actions 等の定期実行から叩く想定。REPORT_TOKEN で保護する。
    """
    expected = os.environ.get("REPORT_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="REPORT_TOKEN が設定されていません")
    if token != expected:
        raise HTTPException(status_code=403, detail="token が一致しません")
    if period not in REPORT_PERIODS:
        raise HTTPException(status_code=404, detail=f"不明な期間です: {period}")

    days, label, kind = REPORT_PERIODS[period]
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
