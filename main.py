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

import urllib.request
import notion_writer


def fetch_title(url: str) -> str:
    """URLにアクセスしてHTMLのtitleタグを取得する（短縮URLも自動追跡）"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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

        import html as html_module

        # OGタグ → twitter:title → titleタグの順で取得
        og_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not og_match:
            og_match = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html, re.IGNORECASE)

        tw_match = re.search(r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not tw_match:
            tw_match = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:title["\']', html, re.IGNORECASE)

        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)

        raw_title = (
            og_match.group(1) if og_match else
            tw_match.group(1) if tw_match else
            title_match.group(1) if title_match else
            ""
        )
        if not raw_title:
            return ""

        title = html_module.unescape(raw_title.strip())
        return re.sub(r"\s+", " ", title).strip()
    except Exception:
        pass
    return ""

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


@app.get("/save")
async def save_from_share(url: str = "", title: str = "", text: str = ""):
    """PWA シェアターゲット：Chrome の共有から URL を受信して Notion に保存"""
    import re
    # urlが空の場合、textからURLを抽出（SmartNews等はURLをtextに埋め込む）
    if not url and text:
        match = re.search(r'https?://\S+', text)
        if match:
            url = match.group()
    # titleが空でtextにURLが含まれる場合、URL以外の部分をtitleに使う
    if not title and text:
        title = re.sub(r'https?://\S+', '', text).strip(" -\n")
    if not url:
        return HTMLResponse(content="<html><body><p>URLが指定されていません</p></body></html>")

    if not title:
        title = fetch_title(url) or _fallback_title(url)

    try:
        token, database_id = notion_writer.get_credentials()
        notion_writer.save_to_notion(url, title, token, database_id)
    except RuntimeError as e:
        return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"><title>エラー</title></head>
<body style="font-family:sans-serif;text-align:center;padding:60px;background:#1a1a2e;color:#fff;">
<h2>❌ 保存に失敗しました</h2><p>{e}</p>
</body></html>""")

    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"><title>News Cheker</title>
<meta name="theme-color" content="#4285f4">
<style>body{font-family:sans-serif;text-align:center;padding:60px 20px;background:#1a1a2e;color:#fff;}</style>
</head><body>
<h2>✅ 保存しました</h2>
<p>Notion に追加されました。</p>
<script>setTimeout(()=>window.close(),2000);</script>
</body></html>""")


@app.post("/webhook")
async def webhook(request: Request):
    """ブックマークレットから URL を受信して Notion に保存"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="リクエストボディが JSON ではありません")

    url = (body.get("url") or "").strip()
    title = (body.get("title") or "").strip()

    if not url:
        raise HTTPException(status_code=400, detail="url が必要です")

    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail=f"無効な URL です: {url}")

    if not title:
        title = fetch_title(url) or _fallback_title(url)

    try:
        token, database_id = notion_writer.get_credentials()
        notion_url = notion_writer.save_to_notion(url, title, token, database_id)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"Notion API エラー: {e}")

    print(f"[OK] 保存完了: {title} - {url}")
    return JSONResponse(content={"status": "ok", "title": title, "notion_url": notion_url})


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
