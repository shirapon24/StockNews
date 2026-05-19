# 株主優待通知システム

## ファイル構成

```
main.py          # メインスクリプト（Flask + スケジューラ）
requirements.txt # 依存パッケージ
Procfile         # Railway/Render用
README.md        # この文書
```

---

## Railwayへのデプロイ手順（無料・5分で完了）

### 1. GitHubにリポジトリを作成してプッシュ

```bash
git init
git add .
git commit -m "first commit"
git remote add origin https://github.com/ユーザー名/yutai-notifier.git
git push -u origin main
```

### 2. Railway にデプロイ

1. https://railway.app にアクセス → GitHubでログイン
2. 「New Project」→「Deploy from GitHub repo」
3. リポジトリを選択 → 自動でデプロイ開始

### 3. 環境変数を設定（Variablesタブ）

| 変数名               | 値                                   |
|---------------------|--------------------------------------|
| ANTHROPIC_API_KEY   | sk-ant-...                           |
| SLACK_WEBHOOK_URL   | https://hooks.slack.com/services/... |
| DASHBOARD_USER      | 任意のユーザー名                      |
| DASHBOARD_PASSWORD  | 強いパスワード（必ず変更！）           |

### 4. URLにアクセス

「Settings」→「Domains」に表示されるURLがダッシュボード。

---

## ローカルで動かす場合

```bash
pip install -r requirements.txt

export ANTHROPIC_API_KEY="sk-ant-..."
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export DASHBOARD_USER="admin"
export DASHBOARD_PASSWORD="yourpassword"

python main.py
# → http://localhost:5000 でアクセス
```

---

## 動作の仕組み

```
起動
 ├─ Flask サーバー起動（ダッシュボード配信）
 └─ バックグラウンドで即時チェック実行
     ├─ TDnet RSSを取得
     ├─ 株主優待キーワードでフィルタ
     ├─ 新着なし → 何もしない
     └─ 新着あり → Claude API呼び出し（1件ごと）
         ├─ Slack通知
         └─ DBに保存（ダッシュボードに即反映）

以降 15分ごとに繰り返し
ダッシュボードも15分ごとに自動リロード
```

## Railwayの料金

- 無料枠: 月500時間（1インスタンスなら事実上無制限）
- このアプリの月額: ほぼ¥0
