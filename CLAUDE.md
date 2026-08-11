# news-checker

AndroidからURLや記事テキスト・画像を受信し、Geminiで分析してNotionに自動保存するWebhookサーバー。

## 概要
- **デプロイ先**: Render.com（`render.yaml` 参照）
- **エンドポイント**: `POST /webhook`, `GET /health`
- **受信形式**: URL / テキスト / Base64画像

## ファイル構成
- `main.py` — FastAPIサーバー本体・エントリポイント
- `gemini_client.py` — Google Gemini API連携（分析処理）
- `notion_writer.py` — Notion API連携（DB保存）
- `render.yaml` — Render.comデプロイ設定
- `requirements.txt` — 依存ライブラリ
- `.env.example` — 環境変数テンプレート（`.env` を作って実際の値を設定）

## 環境変数（.env）
```
GEMINI_API_KEY=      # Google AI Studio
NOTION_TOKEN=        # Notionインテグレーションシークレット
NOTION_DATABASE_ID=  # News Checker DBのID
```

## ローカル起動
```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

## 注意
- `main.py` は Render.com の `startCommand` でルート直接参照されるため、ファイルをサブフォルダに移動しないこと
- `.env` はGitに含めないこと（`.env.example` のみコミット対象）
