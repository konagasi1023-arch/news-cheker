"""
main.py - News Cheker Webhook サーバー（FastAPI）

エンドポイント:
  POST /webhook  - Android から URL / テキスト / 画像 を受信して Notion に保存
  GET  /health   - Render.com ヘルスチェック + UptimeRobot ping 用

受信形式（3パターン）:
  ① URL:    {"url": "https://..."}
  ② テキスト: {"content": "記事本文..."}
  ③ 画像:   {"image_b64": "base64...", "mime_type": "image/jpeg"}

環境変数（.env または Render.com のダッシュボードで設定）:
  GEMINI_API_KEY     - Google AI Studio の API キー
  NOTION_TOKEN       - Notion インテグレーションのシークレットキー
  NOTION_DATABASE_ID - News Cheker DB の ID
"""

import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

import gemini_client
import notion_writer

# .env ファイルがある場合は自動読み込み（python-dotenv なしの簡易実装）
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

app = FastAPI(title="News Cheker Webhook", version="1.0.0")


@app.get("/health")
async def health():
    """Render.com ヘルスチェック + UptimeRobot ping 用"""
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    """
    Android（HTTP Shortcuts）から URL / テキスト / 画像 を受け取り、
    Gemini で分析して Notion News Cheker DB に保存する。
    """
    # --- リクエスト解析 ---
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="リクエストボディが JSON ではありません")

    url       = (body.get("url") or "").strip()
    content   = (body.get("content") or "").strip()
    image_b64 = (body.get("image_b64") or "").strip()
    mime_type = (body.get("mime_type") or "image/jpeg").strip()

    # --- バリデーション ---
    if not url and not content and not image_b64:
        raise HTTPException(status_code=400, detail="url / content / image_b64 のいずれかを指定してください")

    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail=f"無効な URL です: {url}")

    # 優先順位: url > image_b64 > content
    if url:
        payload = {"url": url}
    elif image_b64:
        payload = {"image_b64": image_b64, "mime_type": mime_type}
    else:
        payload = {"content": content}

    # --- Gemini 分析 ---
    try:
        analysis = gemini_client.analyze(payload)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=f"Gemini API エラー: {e}")

    # --- Notion 保存 ---
    try:
        token, database_id = notion_writer.get_credentials()
        notion_url = notion_writer.save_to_notion(payload, analysis, token, database_id)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"Notion API エラー: {e}")

    print(f"[OK] 保存完了: {notion_url}")
    return JSONResponse(content={
        "status": "ok",
        "title": analysis["title"],
        "notion_url": notion_url,
    })


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
