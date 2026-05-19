"""
株主優待通知システム
- Flask でダッシュボードをWeb配信
- APScheduler が15分ごとにTDnet RSSをチェック（バックグラウンドスレッド）
- 新着があればClaude APIで要約・スコア・X投稿文を生成
- Slack通知 + DBに保存
"""

import feedparser
import sqlite3
import json
import requests
import anthropic
import os
import functools
from datetime import datetime
from threading import Thread
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, render_template_string, request, Response

# ============================================================
# 設定（環境変数で管理）
# ============================================================
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
SLACK_WEBHOOK_URL  = os.environ.get("SLACK_WEBHOOK_URL", "")
DASHBOARD_USER     = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")
DB_PATH = "yutai.db"
PORT    = int(os.environ.get("PORT", 5000))

TDNET_RSS_URL  = "https://www.release.tdnet.info/inbs/I_list_001_ja.rdf"
YUTAI_KEYWORDS = ["株主優待", "優待", "株主特典"]

app = Flask(__name__)

# ============================================================
# Basic認証デコレータ
# ============================================================
def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USER or auth.password != DASHBOARD_PASSWORD:
            return Response(
                "認証が必要です", 401,
                {"WWW-Authenticate": 'Basic realm="Yutai Dashboard"'}
            )
        return f(*args, **kwargs)
    return decorated

# ============================================================
# DB初期化
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news (
            id          TEXT PRIMARY KEY,
            title       TEXT,
            company     TEXT,
            code        TEXT,
            url         TEXT,
            published   TEXT,
            summary     TEXT,
            score       INTEGER,
            tweet       TEXT,
            sentiment   TEXT,
            created_at  TEXT
        )
    """)
    conn.commit()
    conn.close()

# ============================================================
# TDnet RSSから株主優待ニュースを取得
# ============================================================
def fetch_yutai_news():
    log("RSSを取得中...")
    try:
        feed = feedparser.parse(TDNET_RSS_URL)
    except Exception as e:
        log(f"RSS取得エラー: {e}")
        return []

    new_items = []
    conn = sqlite3.connect(DB_PATH)
    for entry in feed.entries:
        title = entry.get("title", "")
        if not any(kw in title for kw in YUTAI_KEYWORDS):
            continue
        item_id = entry.get("id") or entry.get("link")
        exists = conn.execute("SELECT 1 FROM news WHERE id=?", (item_id,)).fetchone()
        if exists:
            continue
        new_items.append({
            "id":        item_id,
            "title":     title,
            "url":       entry.get("link", ""),
            "published": entry.get("published", ""),
        })
    conn.close()

    if new_items:
        log(f"新着 {len(new_items)} 件を検出")
    else:
        log("新着なし → 終了")
    return new_items

# ============================================================
# Claude APIで要約・スコア・X投稿文を一括生成
# ============================================================
def analyze_with_claude(item: dict) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""以下の株主優待ニュースを分析し、JSONのみを返してください。余分な説明は不要です。

タイトル: {item['title']}
URL: {item['url']}
公開日時: {item['published']}

返すJSONのキー:
- company: 企業名（文字列）
- code: 証券コード（文字列、不明なら空文字）
- summary: Slack通知用の要約（3〜4文、日本語）
- score: 重要度スコア（1〜10の整数。新設・廃止は高め、軽微な変更は低め）
- tweet: X投稿用テキスト（改行含む・ハッシュタグ付き・280文字以内）
- sentiment: SNS反響の予測傾向（"ポジティブ" / "ネガティブ" / "中立"）

tweetのフォーマット例:
【株主優待】1234 企業名

変更内容を簡潔に。

#株主優待 #証券コード"""

    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

# ============================================================
# Slackに通知
# ============================================================
def notify_slack(item: dict, analysis: dict):
    if not SLACK_WEBHOOK_URL:
        return
    score = analysis.get("score", 5)
    stars = "★" * min(score, 10) + "☆" * max(0, 10 - score)
    text = (
        f"*【株主優待速報】{analysis.get('company', '')}*\n"
        f"重要度: {stars[:5]} ({score}/10)\n"
        f"\n{analysis.get('summary', '')}\n"
        f"\n<{item['url']}|元記事を見る>"
    )
    try:
        resp = requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": text, "username": "株主優待Bot", "icon_emoji": ":bell:"},
            timeout=10
        )
        log(f"Slack通知: {'OK' if resp.status_code == 200 else f'失敗({resp.status_code})'}")
    except Exception as e:
        log(f"Slack通知エラー: {e}")

# ============================================================
# メイン処理（15分ごとに実行）
# ============================================================
def job():
    new_items = fetch_yutai_news()
    if not new_items:
        return  # 新着なし → Claude API呼ばない

    conn = sqlite3.connect(DB_PATH)
    for item in new_items:
        try:
            log(f"Claude API呼び出し: {item['title']}")
            analysis = analyze_with_claude(item)
            conn.execute("""
                INSERT OR IGNORE INTO news
                (id, title, company, code, url, published,
                 summary, score, tweet, sentiment, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                item["id"], item["title"],
                analysis.get("company", ""), analysis.get("code", ""),
                item["url"], item["published"],
                analysis.get("summary", ""), analysis.get("score", 5),
                analysis.get("tweet", ""), analysis.get("sentiment", "中立"),
                datetime.now().isoformat(),
            ))
            conn.commit()
            notify_slack(item, analysis)
        except Exception as e:
            log(f"エラー ({item['title']}): {e}")
    conn.close()

# ============================================================
# ダッシュボード HTML
# ============================================================
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>株主優待 速報</title>
<style>
  :root { --bg:#f9f8f5; --card:#fff; --border:#e8e5de; --text:#1a1a18; --sub:#6b6b68; --blue:#1d9bf0; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans',sans-serif;
         background:var(--bg); color:var(--text); padding:20px 16px;
         max-width:680px; margin:0 auto; }
  h1 { font-size:17px; font-weight:600; margin-bottom:4px; }
  .updated { font-size:12px; color:var(--sub); margin-bottom:18px; }
  .reload { color:var(--blue); cursor:pointer; text-decoration:underline; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:12px;
          padding:16px; margin-bottom:12px; transition:opacity .3s; }
  .card.done { opacity:.35; pointer-events:none; }
  .card-title { font-size:14px; font-weight:600; margin-bottom:8px; line-height:1.4; }
  .chips { display:flex; flex-wrap:wrap; gap:5px; margin-bottom:10px; }
  .chip { font-size:11px; font-weight:500; padding:2px 7px; border-radius:4px;
          background:#eaf3de; color:#27500a; }
  .chip.score { background:#faeeda; color:#633806; }
  .chip.neg   { background:#fcebeb; color:#791f1f; }
  .chip.neu   { background:#f1efe8; color:#444; }
  hr { border:none; border-top:1px solid var(--border); margin:10px 0; }
  .label { font-size:11px; font-weight:600; color:var(--sub); margin-bottom:4px; }
  .summary { font-size:13px; line-height:1.7; margin-bottom:10px; }
  textarea { width:100%; font-size:13px; font-family:inherit; border:1px solid var(--border);
             border-radius:8px; padding:9px 11px; resize:vertical; min-height:108px;
             line-height:1.65; background:var(--bg); color:var(--text); }
  textarea:focus { outline:none; border-color:#999; }
  .actions { display:flex; align-items:center; justify-content:space-between;
             margin-top:7px; flex-wrap:wrap; gap:6px; }
  .char { font-size:12px; color:var(--sub); }
  .char.warn { color:#d97706; } .char.over { color:#dc2626; }
  .btns { display:flex; gap:6px; }
  .btn { font-size:13px; padding:7px 14px; border-radius:8px; border:1px solid var(--border);
         cursor:pointer; background:var(--card); color:var(--text); white-space:nowrap; }
  .btn:active { opacity:.7; }
  .btn.copied { background:#eaf3de; color:#27500a; border-color:#639922; }
  .source { font-size:12px; margin-top:9px; }
  .source a { color:var(--blue); text-decoration:none; }
  .empty { text-align:center; padding:60px 0; color:var(--sub); font-size:14px; line-height:2; }
</style>
</head>
<body>
<h1>📢 株主優待 速報ダッシュボード</h1>
<p class="updated">
  最終更新: {{ updated }}
  &nbsp;<span class="reload" onclick="location.reload()">↺ 更新</span>
</p>

{% if items %}
{% for item in items %}
<div class="card" id="card-{{ loop.index0 }}">
  <div class="card-title">
    {% if item.code %}{{ item.code }} {% endif %}{{ item.company }} — {{ item.title }}
  </div>
  <div class="chips">
    <span class="chip score">重要度 {{ item.score }}/10</span>
    <span class="chip {% if item.sentiment == 'ネガティブ' %}neg{% elif item.sentiment == '中立' %}neu{% endif %}">
      {{ item.sentiment }}
    </span>
    <span class="chip neu">{{ item.published[:10] if item.published else '' }}</span>
  </div>
  <hr>
  <div class="label">要約</div>
  <div class="summary">{{ item.summary }}</div>
  <hr>
  <div class="label">X 投稿テキスト（編集してコピー）</div>
  <textarea id="ta-{{ loop.index0 }}" oninput="count({{ loop.index0 }})">{{ item.tweet }}</textarea>
  <div class="actions">
    <span class="char" id="cc-{{ loop.index0 }}"></span>
    <div class="btns">
      <button class="btn" onclick="markDone({{ loop.index0 }})">✓ 投稿済み</button>
      <button class="btn" id="btn-{{ loop.index0 }}" onclick="copyTweet({{ loop.index0 }})">📋 コピー</button>
    </div>
  </div>
  <div class="source"><a href="{{ item.url }}" target="_blank">元記事を見る →</a></div>
</div>
{% endfor %}
{% else %}
<div class="empty">
  現在、新着の株主優待ニュースはありません。<br>
  <span class="reload" onclick="location.reload()">↺ 再読み込み</span>
</div>
{% endif %}

<script>
document.querySelectorAll('textarea').forEach((_,i) => count(i));

function count(i) {
  const ta = document.getElementById('ta-'+i);
  const cc = document.getElementById('cc-'+i);
  if (!ta || !cc) return;
  const n = ta.value.length;
  cc.textContent = n + ' / 280文字';
  cc.className = 'char' + (n > 280 ? ' over' : n > 240 ? ' warn' : '');
}
function copyTweet(i) {
  const ta  = document.getElementById('ta-'+i);
  const btn = document.getElementById('btn-'+i);
  navigator.clipboard.writeText(ta.value).catch(() => { ta.select(); document.execCommand('copy'); });
  btn.classList.add('copied');
  btn.textContent = '✓ コピーしました';
  setTimeout(() => { btn.classList.remove('copied'); btn.textContent = '📋 コピー'; }, 2000);
}
function markDone(i) {
  document.getElementById('card-'+i).classList.add('done');
}
// 15分ごとに自動リロード
setTimeout(() => location.reload(), 15 * 60 * 1000);
</script>
</body>
</html>"""

# ============================================================
# Flask ルーティング
# ============================================================
@app.route("/")
@require_auth
def dashboard():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT code, company, title, url, published,
               summary, score, tweet, sentiment
        FROM news
        ORDER BY created_at DESC
        LIMIT 50
    """).fetchall()
    conn.close()
    items = [
        {"code": r[0], "company": r[1], "title": r[2], "url": r[3],
         "published": r[4], "summary": r[5], "score": r[6],
         "tweet": r[7], "sentiment": r[8]}
        for r in rows
    ]
    updated = datetime.now().strftime("%Y/%m/%d %H:%M")
    return render_template_string(DASHBOARD_HTML, items=items, updated=updated)

@app.route("/health")
def health():
    return "ok", 200

# ============================================================
# ユーティリティ
# ============================================================
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ============================================================
# エントリポイント
# ============================================================
if __name__ == "__main__":
    init_db()

    # スケジューラをバックグラウンドで起動
    scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
    scheduler.add_job(job, "interval", minutes=15)
    scheduler.start()
    log("スケジューラ起動（15分ごとにチェック）")

    # 起動時に即実行（バックグラウンドスレッドで）
    Thread(target=job, daemon=True).start()

    # Flaskサーバー起動
    log(f"ダッシュボード起動 → http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)