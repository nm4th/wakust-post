"""
ワクスト 記事タイトル自動更新 ＋ 出勤記事再投稿スクリプト
====================================================================
毎日0:00 JSTに実行し、以下を行います。

  1. 記事一覧から全記事のURLとタイトルを取得
  2. 各記事の編集画面(edit_text_2)からスケジュールURLを取得
  3. スケジュールページから本日以降で最も近い出勤日を最大3件取得
  4. タイトルの【日付出勤】部分を更新
     - 同月: 【3/13,14,15出勤】  月またぎ: 【3/13,14|4/4出勤】
  5. 無料部分に「〇月〇日更新」を挿入
  6. 無料部分の回遊リスト: 明日出勤(グループ1)・明後日出勤(グループ2)
  7. 明日出勤の記事を優先的に再投稿（カテゴリ上限4/4・無料部分URLの記事は除外）
  8. PVデータをCSVに記録

使い方:
  pip install requests beautifulsoup4
  python wakust_auto_update.py
"""

import requests
from bs4 import BeautifulSoup
import time
import re
import json
import os
import sys
import csv
import glob
import html as html_module
import logging
import math
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from urllib.parse import urlparse, parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


# ============================================================
# ログ設定
# ============================================================
def setup_logging():
    os.makedirs("logs", exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(message)s")

    # stdout → logs/wakust.log
    file_handler = logging.FileHandler("logs/wakust.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    # stderr → logs/wakust_error.log
    error_handler = logging.FileHandler("logs/wakust_error.log", encoding="utf-8")
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)

    # コンソールにも出力
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(error_handler)
    root_logger.addHandler(console_handler)


setup_logging()
log = logging.getLogger(__name__)

# ============================================================
# ★ 設定（必要に応じて変更してください）
# ============================================================
WAKUST_EMAIL    = os.environ.get("WAKUST_EMAIL", "")
WAKUST_PASSWORD = os.environ.get("WAKUST_PASSWORD", "")
# WAKUST_COOKIE: ブラウザのCookieを丸ごとコピーして設定すると、
# ログインをスキップして認証済みセッションを直接構築する
# フォーマット: "PHPSESSID=xxx; is_login_mid=xxx; user_last_login=xxx; ..."
WAKUST_COOKIE   = os.environ.get("WAKUST_COOKIE", "")

# メール通知設定（GitHub Secretsで管理）
REPORT_EMAIL    = os.environ.get("REPORT_EMAIL", "")       # 送信先
SMTP_USER       = os.environ.get("SMTP_USER", "")          # Gmail アドレス
SMTP_PASSWORD   = os.environ.get("SMTP_PASSWORD", "")      # Gmail アプリパスワード

# タイムゾーン（GitHub ActionsはUTCで動くため、JST明示が必須）
JST = timezone(timedelta(hours=9))

def jst_strftime(fmt):
    """time.strftimeのJST版"""
    return datetime.now(JST).strftime(fmt)

# CALENDAR_ONLY: まとめ記事（出勤カレンダー）のみ更新
CALENDAR_ONLY = os.environ.get("CALENDAR_ONLY", "0") == "1"
# TITLE_ONLY: タイトル＋回遊リストのみ更新（16:30モード、再投稿・PVなし）
TITLE_ONLY = os.environ.get("TITLE_ONLY", "0") == "1"
# CODOC_MODE: "post_new" = codocに未投稿記事から1件投稿（朝昼夜の3回/日実行想定）
#             未設定/空 = 通常モード
CODOC_MODE = os.environ.get("CODOC_MODE", "").strip()
# CODOC_COOKIE: ブラウザで2FA突破後のCookie文字列
# 形式: "XSRF-TOKEN=xxx; codoc_session=yyy; remember_web_...=zzz"
CODOC_COOKIE = os.environ.get("CODOC_COOKIE", "").strip()
# CODOC_LIMITED: "1" = codoc上は限定公開にして、販売導線は自社サイトのみにする
#                "0" = codoc.jp の記事一覧にも載せる
CODOC_LIMITED = os.environ.get("CODOC_LIMITED", "1").strip() or "1"
# SITE_PUBLISH_LIMIT: 1回の実行で自社サイトに新規掲載する記事数
try:
    SITE_PUBLISH_LIMIT = max(1, int(os.environ.get("SITE_PUBLISH_LIMIT", "1")))
except ValueError:
    SITE_PUBLISH_LIMIT = 1


# ============================================================
# 定数
# ============================================================
STATE_FILE          = "wakust_state.json"
PV_LOG_DIR          = "logs"
PV_LOG_FILE         = "logs/wakust_pv_log.csv"
BASE_URL            = "https://wakust.com"
LOGIN_AJAX_URL      = "https://wakust.com/wp-content/themes/wakust/user_edit/login_mypage.php"
POST_LIST_URL       = f"{BASE_URL}/mypage/?post_list"
EDIT_FORM_ACTION    = f"{BASE_URL}/useredit/"
SETPRICE_LIST_URL   = f"{BASE_URL}/mypage/?setprice"
EDIT_SET_URL        = f"{BASE_URL}/wp-content/themes/wakust/user_edit/edit_set.php"
SETLIST_URL_FMT     = f"{BASE_URL}/setlist/?set_id={{}}"
USERPROFILE_URL     = f"{BASE_URL}/mypage/?userprofile"
EDIT_PROFILE_URL    = f"{BASE_URL}/wp-content/themes/wakust/user_edit/edit_profile.php"
# プロフィールのフリーリンク1〜5に対応するフィールド番号
PROFILE_LINK_SLOTS  = [6, 7, 8, 11, 12]
# フリーリンクに載せる本日出勤セットの地域（順序=表示順）
PROFILE_LINK_AREAS  = ["東京都内", "新宿", "池袋", "神奈川", "埼玉"]
REPOST_FIELD        = "repost"
RELATED_BLOCK_START       = "<!-- related_posts_start -->"
RELATED_BLOCK_END         = "<!-- related_posts_end -->"
RELATED_NEXT_BLOCK_START  = "<!-- related_next_posts_start -->"
RELATED_NEXT_BLOCK_END    = "<!-- related_next_posts_end -->"
UPDATED_DATE_START        = "<!-- updated_date_start -->"
UPDATED_DATE_END          = "<!-- updated_date_end -->"
CALENDAR_BLOCK_START      = '<div id="calendar_block_start" style="display:none"></div>'
CALENDAR_BLOCK_END        = '<div id="calendar_block_end" style="display:none"></div>'
# 旧マーカー（HTMLコメント版）: サイト側で消える場合があるため互換用
_OLD_CALENDAR_BLOCK_START = "<!-- calendar_block_start -->"
_OLD_CALENDAR_BLOCK_END   = "<!-- calendar_block_end -->"
PAID_PREVIEW_START        = "<!-- paid_preview_start -->"
PAID_PREVIEW_END          = "<!-- paid_preview_end -->"
PAID_DISCLAIMER_START     = "<!-- paid_disclaimer_start -->"
PAID_DISCLAIMER_END       = "<!-- paid_disclaimer_end -->"

# まとめ記事（出勤カレンダー）: タイトル更新・再投稿をスキップ
# {post_id: {"categories": set, "area_label": str}}
SUMMARY_POSTS = {
    "1657099": {"categories": {"東京都", "池袋", "新宿"}, "area_label": "東京エリア"},
    "1657101": {"categories": {"多摩"},                   "area_label": "多摩エリア"},
    "1657104": {"categories": {"神奈川県"},               "area_label": "神奈川エリア"},
    "1657105": {"categories": {"埼玉県"},                 "area_label": "埼玉エリア"},
}
SUMMARY_POST_IDS = set(SUMMARY_POSTS.keys())
# 全まとめ記事の対象カテゴリ（情報収集用）
SUMMARY_ALL_CATEGORIES = set()
# カテゴリ→カレンダー記事URL のマッピング
CATEGORY_CALENDAR_URL = {}
for _sp_id, _sp in SUMMARY_POSTS.items():
    SUMMARY_ALL_CATEGORIES |= _sp["categories"]
    _cal_url = f"https://wakust.com/Risingnoboru/{_sp_id}/"
    for _cat in _sp["categories"]:
        CATEGORY_CALENDAR_URL[_cat] = {"url": _cal_url, "label": _sp["area_label"]}

# 販売ポイント（値段）の自動調整設定
# 1000スタートで販売回数が2回増えるごとに100ポイント上げ、上限は1500
POINT_BASE = 1000  # 基準ポイント（販売0回時）
POINT_STEP = 100   # 増加ポイント
POINT_SALES_PER_STEP = 2  # 何回販売ごとに値上げするか
POINT_MAX  = 1500  # 上限ポイント

# セット販売の構成ルール
CATEGORY_TO_SET_AREA = {
    "東京都":   "東京都内",
    "新宿":     "新宿",
    "池袋":     "池袋",
    "神奈川県": "神奈川",
    "千葉県":   "千葉",
    "埼玉県":   "埼玉",
    "多摩":     "多摩",
}
SET_TAG_PRIORITY   = ["NN", "NS", "HR", "PZ"]  # 先勝ちで1記事1タグ
SET_MIN_POSTS      = 2      # 最低記事数
SET_MAX_OFF_PCT    = 45     # 割引上限(%)
SET_POST_INTERVAL  = 0.5    # セット作成間隔(秒)


def _calc_set_price(total_pt, post_count):
    """件数に応じた割引を適用して100pt単位で切り上げ

    2件=25%, 3-4件=30%, 5-6件=35%, 7-8件=40%, 9件以上=45%引き（上限）
    """
    if total_pt <= 0:
        return 0
    if post_count <= 2:
        off_pct = 25
    else:
        off_pct = min(30 + 5 * ((post_count - 3) // 2), SET_MAX_OFF_PCT)
    ratio = (100 - off_pct) / 100
    return int(math.ceil(total_pt * ratio / 100)) * 100


def calculate_sales_point(sales_count):
    """販売回数から販売ポイントを計算する。

    販売0-1回: 1000、2-3回: 1100、4-5回: 1200、... 10回以上: 1500（上限）
    """
    try:
        sc = int(sales_count or 0)
    except (TypeError, ValueError):
        sc = 0
    if sc < 0:
        sc = 0
    return min(POINT_BASE + POINT_STEP * (sc // POINT_SALES_PER_STEP), POINT_MAX)



# ============================================================
# PVログ記録
# ============================================================
PV_LOG_COLUMNS = [
    "記録日時", "曜日", "記事ID", "タイトル", "URL", "カテゴリー",
    "投稿日時", "最終編集日時", "最終再投稿日時", "直近出勤日",
    "前日PV", "前週PV", "前月PV", "全期間PV", "販売回数", "売上pt",
]
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


def log_pv(posts, post_infos=None, state=None):
    """記事ごとのPV・売上データをCSVに記録（0時モードのみ呼ばれる）

    出力: wakust_pv_log.csv（追記形式、17列）
    """
    os.makedirs(PV_LOG_DIR, exist_ok=True)
    now = datetime.now(JST)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    weekday = WEEKDAY_JP[now.weekday()]

    # post_infosから直近出勤日を引くためのマップ
    info_map = {}
    if post_infos:
        for info in post_infos:
            info_map[info["post"]["id"]] = info

    # stateから最終再投稿日時を引くためのマップ
    state = state or {}

    write_header = not os.path.exists(PV_LOG_FILE)
    with open(PV_LOG_FILE, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(PV_LOG_COLUMNS)
        for post in posts:
            info = info_map.get(post["id"], {})
            post_state = state.get(post["id"], {})
            reposted_at = post_state.get("reposted_at", "")
            next_date = info.get("next_date", "")
            writer.writerow([
                now_str,
                weekday,
                post["id"],
                post["title"],
                post["url"],
                post.get("category", "未分類"),
                post.get("posted_at", ""),
                post.get("edited_at", ""),
                reposted_at,
                next_date or "",
                post.get("pv_daily") or "",
                post.get("pv_weekly") or "",
                post.get("pv_monthly") or "",
                post.get("pv_total") or "",
                post.get("sales_count") or "",
                post.get("sales_pt") or "",
            ])

    pv_posts = [p for p in posts if p.get("pv_daily") is not None]
    total_pv = sum(p["pv_daily"] for p in pv_posts)
    log.info(f"📊 PVログ記録: {len(posts)}件 合計{total_pv}PV → {PV_LOG_FILE}")
    for p in sorted(pv_posts, key=lambda x: x["pv_daily"], reverse=True)[:10]:
        log.info(f"    [{p['id']}] {p['pv_daily']:>4}PV  {p['title']}")


PV_REPORT_FILE = "logs/wakust_pv_report.csv"


def generate_pv_report(posts):
    """前日比＋週次サマリーのPV比較レポートを生成する（0時モードで呼ばれる）。

    - 前日比: 前日のCSVデータと比較し各記事のPV増減・ランキング変動を出力
    - 週次サマリー: 過去7日分のCSVを集計しPV推移・トップ記事・成長率を出力
    - レポートCSV: logs/wakust_pv_report.csv に当日分を追記
    """
    os.makedirs(PV_LOG_DIR, exist_ok=True)

    if not os.path.exists(PV_LOG_FILE):
        log.info("📈 PV比較レポート: ログファイルが未作成のためスキップ")
        return

    # CSVを日付ごとにグループ化して読み込む
    daily_data = _load_pv_log_by_date()

    if not daily_data:
        log.info("📈 PV比較レポート: 過去データなし。スキップ")
        return

    today = datetime.now(JST).strftime("%Y-%m-%d")
    dates_sorted = sorted(daily_data.keys())

    # 現在のPVデータをマップ化
    current_map = {}
    for p in posts:
        if p.get("pv_daily") is not None:
            current_map[p["id"]] = {
                "title": p["title"],
                "pv_daily": p.get("pv_daily") or 0,
                "pv_weekly": p.get("pv_weekly") or 0,
                "pv_monthly": p.get("pv_monthly") or 0,
                "pv_total": p.get("pv_total") or 0,
                "sales_count": p.get("sales_count") or 0,
            }

    # レポート本文を収集（ログ＋メール用）
    report_lines = []

    def _report(msg):
        log.info(msg)
        report_lines.append(msg)

    # ── 前日比レポート ──
    yesterday_date = dates_sorted[-1]
    yesterday_map = daily_data[yesterday_date]

    _report(f"\n{'═'*55}")
    _report(f"📈 PV比較レポート（前日比: {yesterday_date} → {today}）")
    _report(f"{'═'*55}")

    # 前日と今日の合計PV
    prev_total = sum(d.get("pv_daily", 0) for d in yesterday_map.values())
    curr_total = sum(d.get("pv_daily", 0) for d in current_map.values())
    diff_total = curr_total - prev_total
    sign = "+" if diff_total >= 0 else ""
    _report(f"  合計PV: {prev_total} → {curr_total} ({sign}{diff_total})")

    # 記事ごとの増減を計算
    report_rows = []
    for pid, curr in current_map.items():
        prev = yesterday_map.get(pid, {})
        prev_pv = prev.get("pv_daily", 0)
        curr_pv = curr["pv_daily"]
        diff = curr_pv - prev_pv
        growth = ((curr_pv / prev_pv - 1) * 100) if prev_pv > 0 else 0
        report_rows.append({
            "id": pid,
            "title": curr["title"],
            "pv_prev": prev_pv,
            "pv_curr": curr_pv,
            "pv_diff": diff,
            "growth_pct": growth,
            "pv_total": curr["pv_total"],
        })

    # PV増加トップ10
    rising = sorted(report_rows, key=lambda x: x["pv_diff"], reverse=True)
    _report(f"\n  📈 PV上昇トップ10:")
    for r in rising[:10]:
        sign = "+" if r["pv_diff"] >= 0 else ""
        _report(f"    [{r['id']}] {r['pv_prev']:>4} → {r['pv_curr']:>4} ({sign}{r['pv_diff']:>+4}) {r['title'][:30]}")

    # PV減少ワースト5
    falling = sorted(report_rows, key=lambda x: x["pv_diff"])
    worst = [r for r in falling[:5] if r["pv_diff"] < 0]
    if worst:
        _report(f"\n  📉 PV下降ワースト5:")
        for r in worst:
            _report(f"    [{r['id']}] {r['pv_prev']:>4} → {r['pv_curr']:>4} ({r['pv_diff']:>+4}) {r['title'][:30]}")

    # ── 週次サマリー ──
    week_dates = dates_sorted[-7:]
    if len(week_dates) >= 2:
        _report(f"\n{'═'*55}")
        _report(f"📊 週次サマリー（{week_dates[0]} 〜 {today}）")
        _report(f"{'═'*55}")

        # 日別合計PVの推移
        _report(f"  日別PV推移:")
        daily_totals = []
        for d in week_dates:
            dt = sum(v.get("pv_daily", 0) for v in daily_data[d].values())
            daily_totals.append(dt)
            weekday = WEEKDAY_JP[datetime.strptime(d, "%Y-%m-%d").weekday()]
            _report(f"    {d}（{weekday}）: {dt:>5}PV")
        _report(f"    {today}（{WEEKDAY_JP[datetime.now(JST).weekday()]}）: {curr_total:>5}PV ← 本日")

        # 週間平均
        all_totals = daily_totals + [curr_total]
        avg_pv = sum(all_totals) / len(all_totals)
        _report(f"  週間平均: {avg_pv:.0f}PV/日")

        # 週間成長率（最初の日 vs 今日）
        first_day_total = daily_totals[0] if daily_totals else 0
        if first_day_total > 0:
            weekly_growth = (curr_total / first_day_total - 1) * 100
            sign = "+" if weekly_growth >= 0 else ""
            _report(f"  週間成長率: {sign}{weekly_growth:.1f}%")

        # 週間累計PVトップ10
        weekly_cumulative = defaultdict(lambda: {"pv_sum": 0, "title": "", "days": 0})
        for d in week_dates:
            for pid, data in daily_data[d].items():
                weekly_cumulative[pid]["pv_sum"] += data.get("pv_daily", 0)
                weekly_cumulative[pid]["title"] = data.get("title", "")
                weekly_cumulative[pid]["days"] += 1
        # 今日分も加算
        for pid, curr in current_map.items():
            weekly_cumulative[pid]["pv_sum"] += curr["pv_daily"]
            weekly_cumulative[pid]["title"] = curr["title"]
            weekly_cumulative[pid]["days"] += 1

        top_weekly = sorted(weekly_cumulative.items(), key=lambda x: x[1]["pv_sum"], reverse=True)
        _report(f"\n  🏆 週間累計PVトップ10:")
        for pid, data in top_weekly[:10]:
            _report(f"    [{pid}] {data['pv_sum']:>5}PV ({data['days']}日間)  {data['title'][:30]}")

    # ── レポートCSVに追記 ──
    write_header = not os.path.exists(PV_REPORT_FILE)
    with open(PV_REPORT_FILE, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "日付", "記事ID", "タイトル", "前日PV", "当日PV",
                "PV増減", "成長率%", "全期間PV",
            ])
        for r in report_rows:
            writer.writerow([
                today, r["id"], r["title"], r["pv_prev"], r["pv_curr"],
                r["pv_diff"], f"{r['growth_pct']:.1f}", r["pv_total"],
            ])

    log.info(f"\n📄 PV比較レポートCSV出力 → {PV_REPORT_FILE}")

    # ── メール送信 ──
    _send_report_email(today, report_lines)


def _send_report_email(today, report_lines):
    """PV比較レポートをGmail経由でメール送信する。

    必要な環境変数（GitHub Secrets）:
      REPORT_EMAIL  - 送信先メールアドレス
      SMTP_USER     - 送信元Gmailアドレス
      SMTP_PASSWORD  - Gmailアプリパスワード
    """
    if not all([REPORT_EMAIL, SMTP_USER, SMTP_PASSWORD]):
        log.info("📧 メール送信: SMTP設定が未構成のためスキップ")
        return

    subject = f"ワクスト PVレポート {today}"
    body = "\n".join(report_lines)

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = REPORT_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        log.info(f"📧 レポートメール送信完了 → {REPORT_EMAIL}")
    except Exception as e:
        log.warning(f"⚠️ メール送信失敗: {e}")


def _load_pv_log_by_date():
    """PVログCSVを日付ごとに {date: {post_id: {pv_daily, title, ...}}} で読み込む"""
    daily_data = {}
    try:
        with open(PV_LOG_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                date_str = row.get("記録日時", "")[:10]
                if not date_str:
                    continue
                pid = row.get("記事ID", "")
                if not pid:
                    continue
                if date_str not in daily_data:
                    daily_data[date_str] = {}
                daily_data[date_str][pid] = {
                    "title": row.get("タイトル", ""),
                    "pv_daily": int(row.get("前日PV") or 0),
                    "pv_weekly": int(row.get("前週PV") or 0),
                    "pv_monthly": int(row.get("前月PV") or 0),
                    "pv_total": int(row.get("全期間PV") or 0),
                    "sales_count": int(row.get("販売回数") or 0),
                }
    except Exception as e:
        log.warning(f"⚠️ PVログ読み込みエラー: {e}")
    return daily_data


# ============================================================
# 状態管理
# ============================================================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _to_multipart(payload):
    """dict を multipart/form-data 用の files 形式に変換する"""
    return {k: (None, v) for k, v in payload.items()}


# ============================================================
# ログイン
# ============================================================
def _warm_cookies_via_playwright():
    """Playwrightで実ブラウザとしてアクセスし、年齢認証やチャレンジを通過して
    Cookieを取得する。戻り値: [{'name':..,'value':..,'domain':..}, ...] or None
    """
    try:
        from playwright.sync_api import sync_playwright
        import time as _time
        log.info("    🔧 Playwrightで年齢認証/チャレンジ通過を試行中...")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0.0.0 Safari/537.36"),
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
            )
            page = context.new_page()
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = {runtime: {}};
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['ja', 'en-US', 'en']});
            """)
            # トップページにアクセス
            page.goto(f"{BASE_URL}/", wait_until="domcontentloaded", timeout=60000)
            _time.sleep(2)

            # ページ状態を判定
            content = page.content()
            AGE_CLICK_CANDIDATES = [
                'text="はい"',
                'text="18歳以上"',
                'text="同意する"',
                'text="同意"',
                'text="Enter"',
                'text="入場"',
                'a:has-text("はい")',
                'button:has-text("はい")',
                'a:has-text("18歳以上")',
                'button:has-text("18歳以上")',
                'a[href*="age"]',
                '.age-yes, #age_ok, .age_ok, #age-yes',
            ]

            def _find_age_button():
                for sel in AGE_CLICK_CANDIDATES:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0 and el.is_visible(timeout=500):
                            return sel, el
                    except Exception:
                        continue
                return None, None

            age_sel, age_el = _find_age_button()
            is_challenge = ("少々お待ち" in content
                            or "window.location.reload" in content)

            if age_sel:
                # 【ケースA】年齢認証画面 → ボタンクリック
                log.info(f"    🔧 年齢認証画面を検出 → クリック: {age_sel}")
                try:
                    age_el.click(timeout=5000)
                    _time.sleep(3)
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception as e:
                    log.warning(f"    ⚠️ 年齢認証クリック後の待機失敗: {e}")
            elif is_challenge:
                # 【ケースB】少々お待ちください等のチャレンジ → reload待機
                log.info("    🔧 チャレンジページを検出 → 自動reload待機")
                _time.sleep(8)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                # まだチャレンジならもう一度
                if "少々お待ち" in page.content():
                    log.info("    🔧 チャレンジ継続中 → 追加reload+待機")
                    _time.sleep(10)
                    try:
                        page.reload(wait_until="networkidle", timeout=30000)
                    except Exception:
                        pass
                # reload後に年齢認証が出てくることもあるので再チェック
                age_sel2, age_el2 = _find_age_button()
                if age_sel2:
                    log.info(f"    🔧 reload後に年齢認証を検出 → クリック: {age_sel2}")
                    try:
                        age_el2.click(timeout=5000)
                        _time.sleep(3)
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
            else:
                # 【ケースC】通常ページ（既に認証済み等） → 即Cookie取得
                log.info("    🔧 通常ページ検出（年齢認証/チャレンジなし）")

            cookies = context.cookies()
            log.info(f"    🔧 Playwright取得Cookie: {[c['name'] for c in cookies]}")
            browser.close()
        return cookies
    except Exception as e:
        log.warning(f"    ⚠️ Playwrightチャレンジ通過失敗: {e}")
        return None


def login_wakust():
    max_retries = 5
    # 30秒 → 60秒 → 120秒 → 300秒 → 600秒 (最大約17分粘る)
    wait_intervals = [30, 60, 120, 300, 600]
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    browser_headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
        # Accept-Encodingはrequestsが自動設定するので上書きしない
        # (brotliを明示するとrequestsが復号できず文字化けする)
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    def _is_challenge_page(text):
        """「少々お待ちください」等のチャレンジページか判定"""
        return ("少々お待ち" in text or "spinner" in text.lower()
                or "window.location.reload" in text)

    # ------------------------------------------------------------------
    # WAKUST_COOKIE が設定されていれば、Cookie注入して認証済みsessionを構築
    # 通常のログインフローをスキップ（チャレンジ画面回避のため）
    # ------------------------------------------------------------------
    if WAKUST_COOKIE:
        log.info("🍪 WAKUST_COOKIE検出 → Cookie注入によるログインを試行")
        session = requests.Session()
        session.headers.update(browser_headers)
        # "name=value; name2=value2; ..." 形式をパース
        for pair in WAKUST_COOKIE.split(";"):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            name, value = name.strip(), value.strip()
            if name:
                try:
                    session.cookies.set(name, value, domain="wakust.com")
                except Exception as e:
                    log.warning(f"    ⚠️ Cookie設定失敗 [{name}]: {e}")
        log.info(f"    🔧 注入Cookie: {list(session.cookies.keys())}")
        # 認証確認: /mypage/ にGETしてloginページに戻されないか
        try:
            check = session.get(f"{BASE_URL}/mypage/", timeout=30,
                                allow_redirects=True)
            body = check.text[:2000]
            if (check.status_code == 200
                    and "login" not in check.url.lower()
                    and "ログイン" not in body
                    and not _is_challenge_page(body)):
                log.info(f"✅ Cookie注入でログイン成功 (URL={check.url})")
                return session
            log.warning(f"⚠️ Cookieは無効の可能性 (HTTP {check.status_code}, "
                        f"URL={check.url}) → 通常ログインにフォールバック")
        except requests.RequestException as e:
            log.warning(f"⚠️ Cookie検証時の通信エラー: {e} → 通常ログインにフォールバック")
        session.close()

    warmed_cookies = None  # Playwrightで取得したCookieをキャッシュ

    for attempt in range(1, max_retries + 1):
        session = requests.Session()
        session.headers.update(browser_headers)
        # 年齢認証済み扱いにする（成人サイト向け）
        session.cookies.set("age_verified_true", "true", domain="wakust.com")
        # Playwrightで取得済みのCookieがあれば注入
        if warmed_cookies:
            for c in warmed_cookies:
                try:
                    session.cookies.set(
                        c["name"], c["value"],
                        domain=c.get("domain", "wakust.com"),
                        path=c.get("path", "/"),
                    )
                except Exception:
                    pass
            log.info(f"    🔧 Playwright済Cookieを注入: "
                     f"{list(session.cookies.keys())}")

        try:
            # トップページを先にGETしてCookie(PHPSESSID等)を確立
            # Bot判定回避のため
            try:
                warm = session.get(f"{BASE_URL}/", timeout=15)
                log.info(f"    🔧 ウォームアップGET: HTTP {warm.status_code} "
                         f"cookies={list(session.cookies.keys())}")
                # チャレンジページが返ってきたらPlaywrightにフォールバック
                if _is_challenge_page(warm.text) and not warmed_cookies:
                    log.info("    🔧 チャレンジページ検出、Playwrightで通過を試みる")
                    warmed_cookies = _warm_cookies_via_playwright()
                    if warmed_cookies:
                        # 次のループでリトライする
                        session.close()
                        continue
            except requests.RequestException as e:
                log.info(f"    🔧 ウォームアップGET失敗（続行）: {e}")

            res = session.post(LOGIN_AJAX_URL, files={
                "login_email":    (None, WAKUST_EMAIL),
                "login_password": (None, WAKUST_PASSWORD),
            }, headers={"Referer": f"{BASE_URL}/mypage/", "Origin": BASE_URL},
               timeout=30)

            if res.status_code == 200 and "loginok" in res.text:
                log.info(f"✅ ログイン成功 cookies={list(session.cookies.keys())}")
                # ログイン後に/mypage/をGETして認証フローを完結
                # (is_login_mid等の追加Cookieを取得するため)
                try:
                    follow = session.get(f"{BASE_URL}/mypage/", timeout=15)
                    log.info(f"    🔧 マイページGET: HTTP {follow.status_code} "
                             f"cookies={list(session.cookies.keys())}")
                except requests.RequestException as e:
                    log.warning(f"    ⚠️ マイページGET失敗: {e}")
                return session

            # ログインレスポンスがチャレンジページならPlaywrightで通過
            if _is_challenge_page(res.text) and not warmed_cookies:
                log.info("    🔧 ログインレスポンスがチャレンジページ、Playwrightで通過を試みる")
                warmed_cookies = _warm_cookies_via_playwright()

            snippet = res.text[:500].replace("\n", " ")
            log.warning(f"⚠️ ログイン失敗 (試行 {attempt}/{max_retries}): "
                        f"HTTP {res.status_code} body先頭500字: {snippet}")
        except requests.RequestException as e:
            log.warning(f"⚠️ ログインリクエスト例外 (試行 {attempt}/{max_retries}): {e}")

        session.close()
        if attempt < max_retries:
            wait = wait_intervals[attempt - 1]
            log.info(f"🔄 {wait}秒後にリトライします...")
            time.sleep(wait)

    log.error("❌ ログイン失敗: 全リトライ失敗")
    return None


# ============================================================
# 記事一覧の取得
# ============================================================
def _parse_post_list_page(soup):
    """1ページ分の記事一覧をパースする"""
    posts = []
    for td in soup.find_all(class_="td_2"):
        a = td.find("a", href=True)
        if not a:
            continue
        url   = a["href"]
        title = a.get_text(strip=True)
        m = re.search(r"/Risingnoboru/(\d+)/", url)
        if not m:
            continue
        post_id = m.group(1)

        # PV数・売上を同じ行(tr)から取得
        pv_daily = None
        pv_weekly = None
        pv_monthly = None
        pv_total = None
        sales_count = None
        sales_pt = None
        posted_at = None
        edited_at = None
        is_reserved = False
        tr = td.find_parent("tr")
        if tr:
            for sib_td in tr.find_all("td"):
                if sib_td == td:
                    continue
                text = sib_td.get_text(" ", strip=True)
                if "前" in text and "日" in text:
                    m_d = re.search(r"前\s*日\s*[：:]\s*(\d+)", text)
                    m_w = re.search(r"前\s*週\s*[：:]\s*(\d+)", text)
                    m_m = re.search(r"前\s*月\s*[：:]\s*(\d+)", text)
                    m_t = re.search(r"全\s*期\s*間\s*[：:]\s*(\d+)", text)
                    if m_d:
                        pv_daily = int(m_d.group(1))
                    if m_w:
                        pv_weekly = int(m_w.group(1))
                    if m_m:
                        pv_monthly = int(m_m.group(1))
                    if m_t:
                        pv_total = int(m_t.group(1))
                # 売上・販売回数
                if "販売" in text or "売上" in text:
                    m_sc = re.search(r"販売(?:回数)?\s*[：:]\s*(\d+)", text)
                    m_sp = re.search(r"売上\s*[：:]\s*(\d+)", text)
                    if m_sc:
                        sales_count = int(m_sc.group(1))
                    if m_sp:
                        sales_pt = int(m_sp.group(1))
                # 投稿日時・最終編集日時
                if "予約" in text:
                    is_reserved = True
                dt_m = re.search(r"(\d{4}[/-]\d{2}[/-]\d{2}\s+\d{2}:\d{2})", text)
                if dt_m:
                    if posted_at is None:
                        posted_at = dt_m.group(1)
                    else:
                        edited_at = dt_m.group(1)

        # 公開ステータス
        is_published = True
        if tr:
            status_sel = tr.find("select", class_=re.compile(r"select_post_st"))
            if status_sel:
                sel_opt = status_sel.find("option", selected=True)
                if not sel_opt:
                    sel_opt = status_sel.find("option", attrs={"selected": ""})
                if sel_opt and sel_opt.get("value") != "0":
                    is_published = False

        posts.append({
            "id":          post_id,
            "title":       title,
            "url":         url,
            "edit_url":    f"{BASE_URL}/mypage/?post_edit={post_id}",
            "category":    "未分類",
            "pv_daily":    pv_daily,
            "pv_weekly":   pv_weekly,
            "pv_monthly":  pv_monthly,
            "pv_total":    pv_total,
            "sales_count": sales_count,
            "sales_pt":    sales_pt,
            "posted_at":   posted_at,
            "edited_at":   edited_at,
            "is_reserved": is_reserved,
            "is_published": is_published,
        })
    return posts


def fetch_post_list(session):
    """全ページの記事一覧を取得"""
    all_posts = []
    page = 1
    while True:
        url = f"{POST_LIST_URL}&cp={page}" if page > 1 else POST_LIST_URL
        res = session.get(url)
        # レスポンスの文字コードを補正 (ISO-8859-1をデフォルトにされる対策)
        if res.encoding is None or res.encoding.lower() == "iso-8859-1":
            res.encoding = res.apparent_encoding or "utf-8"
        soup = BeautifulSoup(res.text, "html.parser")
        posts = _parse_post_list_page(soup)
        if not posts:
            if page == 1:
                # 1ページ目で0件は異常。診断ログを出す
                log.warning(f"    🔧 1ページ目で0件: HTTP {res.status_code} URL={res.url}")
                log.warning(f"    🔧 encoding={res.encoding} "
                            f"content-type={res.headers.get('content-type')} "
                            f"content-encoding={res.headers.get('content-encoding')} "
                            f"content-length={res.headers.get('content-length')} "
                            f"actual-len={len(res.content)}")
                log.warning(f"    🔧 cookies={list(session.cookies.keys())}")
                log.warning(f"    🔧 body先頭800字: {res.text[:800]!r}")
                # td_2要素の数もログ出力
                td2s = soup.find_all(class_="td_2")
                log.warning(f"    🔧 td_2要素数={len(td2s)}")
                # loginページに戻されていないか
                if "login" in res.url.lower() or "ログイン" in res.text[:2000]:
                    log.error(f"    ❌ ログインページに戻されている（セッション切れの可能性）")
            break
        all_posts.extend(posts)
        # 次ページがあるか確認
        # 方法1: cp=次ページ番号 のリンクを探す
        next_page = page + 1
        next_link = soup.find("a", href=re.compile(rf"cp={next_page}\b"))
        # 方法2: テキストで「次」「›」「>」を含むリンク
        if not next_link:
            next_link = soup.find("a", href=re.compile(r"cp=\d+"), string=re.compile(r"次|›|>|»|›|»"))
        # 方法3: 現在ページより大きいcp=のリンクがあれば次ページあり
        if not next_link:
            for a in soup.find_all("a", href=re.compile(r"cp=(\d+)")):
                m_cp = re.search(r"cp=(\d+)", a["href"])
                if m_cp and int(m_cp.group(1)) > page:
                    next_link = a
                    break
        if next_link:
            log.info(f"    📄 次ページあり: {next_link.get('href', '')} text={next_link.get_text(strip=True)!r}")
        else:
            # デバッグ: ページネーション関連リンクを出力
            cp_links = soup.find_all("a", href=re.compile(r"cp=\d+"))
            if cp_links:
                log.info(f"    🔧 cp=リンク一覧: {[(a['href'], a.get_text(strip=True)[:10]) for a in cp_links]}")
            break
        page += 1
        time.sleep(0.5)

    log.info(f"📋 取得記事数: {len(all_posts)}（{page}ページ）")
    return all_posts


def _find_point_field_name(form):
    """編集フォーム内の「販売ポイント」入力欄のname属性を返す。

    「販売ポイント」というラベルテキストを含む要素の次に現れる
    input 要素のname属性を取得する。見つからない場合はNoneを返す。
    """
    if form is None:
        return None
    for el in form.find_all(string=lambda s: s and "販売ポイント" in s):
        parent = el.parent
        if parent is None:
            continue
        next_input = parent.find_next("input")
        if next_input is not None and next_input.get("name"):
            # type=hidden や checkbox/radio/file は除外
            t = (next_input.get("type") or "").lower()
            if t in ("", "text", "number", "tel"):
                return next_input.get("name")
    return None


def _unwrap_redirect_url(url):
    """リダイレクトラッパーURL（link.php?url=... 等）から実際のURLを展開する"""
    parsed = urlparse(url)
    # link.php?url=... / redirect?url=... / go?url=... パターン
    if parsed.path.rstrip("/").split("/")[-1] in ("link.php", "redirect", "go", "jump", "out"):
        qs = parse_qs(parsed.query)
        for key in ("url", "to", "redirect", "dest", "link"):
            if key in qs:
                inner = unquote(qs[key][0])
                if re.match(r"https?://", inner):
                    log.info(f"    🔧 リダイレクトURL展開: {url} → {inner}")
                    return inner
    return url


# ============================================================
# 編集画面の詳細取得
# ============================================================
def fetch_post_details(session, post):
    res  = session.get(post["edit_url"])
    soup = BeautifulSoup(res.text, "html.parser")
    form    = soup.find("form", action=lambda a: a and "useredit" in a)
    cat_sel = soup.find("select", {"name": "categorys"})

    # デバッグ: 編集ページの取得状況
    log.info(f"    🔧 status={res.status_code} url={res.url} form={'あり' if form else 'なし'} cat_sel={'あり' if cat_sel else 'なし'}")
    if not form:
        # formが見つからない場合、HTMLの先頭を出力して原因特定
        html_snippet = res.text[:500].replace("\n", "\\n")
        log.warning(f"    🔧 HTML先頭: {html_snippet}")
        # formタグを全探索
        all_forms = soup.find_all("form")
        log.warning(f"    🔧 全form数={len(all_forms)} actions={[f.get('action','') for f in all_forms]}")

    # カテゴリーIDをHTMLから直接取得
    # selected属性は値なし属性（selected のみ）なのでhas_attr()で判定する
    # X/4 のカウントを読み取り、4/4なら再投稿不可フラグを立てる
    category_id       = None
    category          = "未分類"
    category_at_limit = False  # True=上限に達している
    category_current  = 0      # 現在の投稿数
    category_max      = 4      # カテゴリ上限数（デフォルト4）
    if cat_sel:
        for opt in cat_sel.find_all("option"):
            if opt.has_attr("selected"):
                category_id = opt.get("value")
                category    = opt.get_text(strip=True)
                m = re.search(r"\((\d+)/(\d+)\)", category)
                if m:
                    category = category[:m.start()].strip()
                    category_current = int(m.group(1))
                    category_max     = int(m.group(2))
                    if category_current >= category_max:
                        category_at_limit = True
                break

    # フォームのペイロードを構築
    payload = {}
    if form:
        for inp in form.find_all(["input", "textarea", "select"]):
            name = inp.get("name")
            if not name:
                continue

            if inp.name == "textarea":
                # decode_contents() で生HTMLをそのまま取得
                # → フォント・太字・改行などのHTMLタグを保持
                payload[name] = inp.decode_contents()

            elif inp.name == "select":
                if name == "categorys":
                    if category_id:
                        payload[name] = category_id
                else:
                    # has_attr("selected") でHTMLのselected属性を正しく取得
                    for opt in inp.find_all("option"):
                        if opt.has_attr("selected"):
                            payload[name] = opt.get("value", "")
                            break
                    # selectedが取れなかった場合は最初のoptionをデフォルトとする
                    if name not in payload:
                        first = inp.find("option")
                        if first:
                            payload[name] = first.get("value", "")

            elif inp.get("type") == "checkbox":
                if name == REPOST_FIELD:
                    continue  # 後で制御
                if inp.has_attr("checked"):
                    payload[name] = inp.get("value", "on")

            elif inp.get("type") == "radio":
                if inp.has_attr("checked"):
                    payload[name] = inp.get("value", "")

            else:
                payload[name] = inp.get("value", "")

    # post_stはHTMLのselected属性から取得済み（上記selectループで処理）

    # 販売ポイント入力欄のname属性を検出
    point_field = _find_point_field_name(form)
    if point_field:
        log.info(f"    🔧 販売ポイント field: name={point_field} current={payload.get(point_field)!r}")
    else:
        log.warning(f"    ⚠️  販売ポイント フィールドが見つかりません")

    # デバッグ: payloadのキーとテキストフィールドの内容量
    text_fields = {k: len(v) for k, v in payload.items() if k.startswith("edit_text")}
    log.info(f"    🔧 payload keys={list(payload.keys())}")
    log.info(f"    🔧 text fields: {text_fields}")
    for fn in ("edit_text_1", "edit_text_2"):
        if payload.get(fn):
            snippet = payload[fn][:300].replace("\n", "\\n")
            log.info(f"    🔧 {fn} ({len(payload[fn])}字) 先頭: {snippet}")

    # スケジュールURLを抽出
    # edit_text_2（有料部分）を優先し、なければ edit_text_1（無料部分）からも探す
    # URLは <a href="..."> タグ内またはプレーンテキストで記載されている
    schedule_url = None
    for field_name in ("edit_text_2", "edit_text_1"):
        raw_text = payload.get(field_name, "")
        if not raw_text:
            continue

        # フォームデータはHTMLエンティティエンコードされている場合がある
        # （&lt;p&gt; → <p>）ので、デコードしてからパースする
        text = html_module.unescape(raw_text)

        # デバッグ: unescape前後の比較
        changed = text != raw_text
        log.info(f"    🔧 URL抽出[{field_name}] unescape変化={changed} raw先頭={repr(raw_text[:80])} unescaped先頭={repr(text[:80])}")

        soup_field = BeautifulSoup(text, "html.parser")
        a_tags = soup_field.find_all("a", href=True)
        log.info(f"    🔧 URL抽出[{field_name}] aタグ数={len(a_tags)} hrefs={[a['href'][:60] for a in a_tags[-3:]]}")

        for a in reversed(a_tags):
            href = a["href"].strip()
            if re.match(r"https?://", href) and "wakust.com" not in href:
                schedule_url = href
                break

        if not schedule_url:
            # フォールバック: プレーンテキストURLを探す
            last_lines = list(reversed(text.splitlines()))
            for line in last_lines:
                clean = re.sub(r"<[^>]+>", "", line).strip()
                if re.match(r"https?://", clean) and "wakust.com" not in clean:
                    schedule_url = clean
                    break
            if not schedule_url:
                log.info(f"    🔧 URL抽出[{field_name}] フォールバックも失敗 最終行={[re.sub(r'<[^>]+>', '', l).strip()[:60] for l in last_lines[:3]]}")

        if schedule_url:
            # リダイレクトラッパーURL（link.php?url=... 等）から実際のURLを展開
            schedule_url = _unwrap_redirect_url(schedule_url)
            log.info(f"    🔧 URL抽出成功: {schedule_url}")
            break

    # スケジュールURLが無料部分(edit_text_1)由来かどうか
    schedule_from_free = (schedule_url is not None and field_name == "edit_text_1")

    return {
        "category":           category,
        "schedule_url":       schedule_url,
        "schedule_from_free": schedule_from_free,
        "payload":            payload,
        "at_limit":           category_at_limit,
        "category_current":   category_current,
        "category_max":       category_max,
        "point_field":        point_field,
    }


# ============================================================
# 記事公開ページからタグを取得
# ============================================================
def fetch_post_tags(session, post_url, alpha_only=True):
    """記事の公開ページからタグとタイトル画像URLを抽出する。

    タグは「KEYWORD(NUMBER)」形式で表示されている。
    例: CKB(127), F(1473), HR(23397), 中野(989), 巨乳(19987)
    → alpha_only=True (既定): ["CKB", "F", "HR"]   ※セット組成のプレイタグ用
    → alpha_only=False:       ["CKB", "F", "HR", "中野", "巨乳"]
                              ※自社サイトの絞り込み用。日本語タグも拾う

    戻り値: (tags: list[str], image_url: str|None)
    """
    try:
        res = session.get(post_url)
        if res.status_code != 200:
            log.warning(f"    ⚠️  タグ取得失敗 (HTTP {res.status_code})")
            return [], None
        soup = BeautifulSoup(res.text, "html.parser")

        tags = []
        # ページ内のリンク・スパンからタグ形式テキストを探す
        tag_re = (re.compile(r'^([A-Za-z]+)\(\d+\)$') if alpha_only
                  else re.compile(r'^(.{1,20}?)\((\d+)\)$'))
        for el in soup.find_all(["a", "span"]):
            text = el.get_text(strip=True)
            m = tag_re.match(text)
            if m and m.group(1) not in tags:
                tags.append(m.group(1))

        if tags:
            log.info(f"    🏷️  タグ: {tags}")

        # タイトル画像URLを抽出（og:image → 記事本文内の最初のimg）
        image_url = None
        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            image_url = og_image["content"].strip()
        if not image_url:
            # 記事本文内の最初のimgタグ
            article = soup.find("article") or soup.find(class_=re.compile(r"post|entry|content"))
            if article:
                img = article.find("img", src=True)
                if img:
                    image_url = img["src"].strip()
        if image_url:
            log.info(f"    🖼️  画像: {image_url}")

        return tags, image_url
    except Exception as e:
        log.warning(f"    ⚠️  タグ取得エラー: {e}")
        return [], None


# ============================================================
# Playwrightでページ取得（403対策・JSレンダリング対策）
# ============================================================
def _fetch_with_playwright(url):
    """Playwrightでヘッドレスブラウザ経由でページを取得する。成功時はBeautifulSoupオブジェクトを返す。"""
    try:
        from playwright.sync_api import sync_playwright
        import time as _time
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                java_script_enabled=True,
            )
            page = context.new_page()
            # ヘッドレスブラウザ検出回避
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = {runtime: {}};
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['ja', 'en-US', 'en']});
            """)
            # まずdomcontentloadedで高速ロード
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response and response.status == 403:
                # Cloudflareチャレンジの可能性: 数秒待って再チェック
                log.info(f"    🔧 403応答 → Cloudflareチャレンジ待機中...")
                _time.sleep(5)
                # ページが遷移（チャレンジ通過）したか確認
                page.wait_for_load_state("networkidle", timeout=15000)
            else:
                # 通常ページ: JSレンダリング完了を待つ
                page.wait_for_load_state("networkidle", timeout=15000)
            # スケジュール要素が表示されるまで追加で待機
            try:
                page.wait_for_selector(".sch-date, .sch-work, .sch-tbl, .weekSchedule, .prof_table, table, dl", timeout=5000)
            except Exception:
                pass  # タイムアウトでも続行
            js_html = page.content()
            browser.close()
        soup = BeautifulSoup(js_html, "html.parser")
        # 403ページかどうか本文でも確認
        title = soup.find("title")
        if title and "403" in title.get_text():
            log.warning(f"    ⚠️ Playwrightでも403(本文): {url}")
            return None
        # Cloudflareチャレンジページの検出
        if soup.find(id="challenge-running") or soup.find(id="cf-challenge-running"):
            log.warning(f"    ⚠️ Cloudflareチャレンジを通過できず: {url}")
            return None
        log.info(f"    🔧 Playwrightで取得成功")
        return soup
    except Exception as e:
        log.warning(f"    ⚠️ Playwrightフォールバック失敗: {e}")
        return None


# ============================================================
# スケジュールページから直近の出勤日を取得
# ============================================================
PLAYWRIGHT_PREFER_DOMAINS = {
    "men-este",        # *.men-este.com (tokyo-fairy-land等)
    "mens-este",       # omiya-mens-este.net 等
    "bed-of-roses",    # Alpine.js (x-for/x-text) でJSレンダリング必須
    "liora2024",       # requests.getで接続タイムアウト
    "muchispa",        # muchispa-room.com (men-este系SaaS・Bot対策あり)
    "offsuit",         # offsuit.site (Bot対策で403)
    "mirrorsspa",      # mirrorsspa.com (JSレンダリング必須)
    "resortlanikai",   # resortlanikai.com (403/JSレンダリング)
}

def _has_work_info(info):
    """出勤情報テキストが有効な出勤エントリかどうかを判定する。
    HH:MM時刻、「満枠」「満了」「出勤」「◯」「○」のいずれかがあればTrue。"""
    if re.search(r"\d{1,2}:\d{2}", info):
        return True
    if any(kw in info for kw in ("満枠", "満了", "出勤", "◯", "○")):
        return True
    return False


def fetch_next_date_from_schedule(schedule_url, start_from_tomorrow=False):
    # スケジュール構造を正常にパースできたが休みを検出した場合True
    # これがTrueならタイトル復元のfallbackを使わず「本当に出勤なし」として扱う
    _saw_off = [False]  # listでクロージャから書き換え可能に
    try:
        _used_playwright = False
        _parsed_host = urlparse(schedule_url).hostname or ""
        _force_playwright = any(d in _parsed_host for d in PLAYWRIGHT_PREFER_DOMAINS)

        if _force_playwright:
            log.info(f"    🔧 Playwright優先ドメイン → Playwrightで取得")
            soup = _fetch_with_playwright(schedule_url)
            _used_playwright = True
            if soup is None:
                return [], False, False, _saw_off[0]
        else:
            res = requests.get(schedule_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
                "Referer": schedule_url,
            }, timeout=10)
            if res.status_code == 403:
                log.info(f"    🔧 HTTP 403 → Playwrightで再取得を試行")
                soup = _fetch_with_playwright(schedule_url)
                _used_playwright = True
                if soup is None:
                    return [], False, False, _saw_off[0]
            elif res.status_code != 200:
                log.error(f"    ❌ スケジュール取得失敗 (HTTP {res.status_code}): {schedule_url}")
                return [], False, False, _saw_off[0]
            else:
                # content-typeのcharsetを優先（apparent_encodingは誤判定があるため）
                if res.encoding is None or res.encoding == "ISO-8859-1":
                    ctype = res.headers.get("content-type", "")
                    m_charset = re.search(r"charset=([^\s;]+)", ctype, re.I)
                    if m_charset:
                        res.encoding = m_charset.group(1)
                    else:
                        res.encoding = "utf-8"
                soup = BeautifulSoup(res.text, "html.parser")
    except Exception as e:
        log.warning(f"    ⚠️ requests取得失敗: {e}")
        log.info(f"    🔧 接続エラー → Playwrightで再取得を試行")
        soup = _fetch_with_playwright(schedule_url)
        _used_playwright = True
        if soup is None:
            log.error(f"    ❌ Playwrightでも取得失敗")
            return [], False, False, _saw_off[0]

    # JSレンダリング判定: スケジュール構造があるが中身が空の場合
    # → Playwrightでヘッドレスブラウザ経由で再取得
    _needs_playwright = False
    if not _used_playwright:
        # weekScheduleクラスがあるがtableが空
        if (soup.find(class_=re.compile(r"weekSchedule", re.I)) and
                not soup.find("table")):
            _needs_playwright = True
        # sch-date/sch-workのdivがあるがdt/ddが空
        _sch_date_div = soup.find("div", class_=re.compile(r"sch-date"))
        _sch_work_div = soup.find("div", class_=re.compile(r"sch-work"))
        if _sch_date_div and _sch_work_div:
            if not _sch_date_div.find("dt") or not _sch_work_div.find("dd"):
                _needs_playwright = True
        # sch-tblクラスがあるがスケジュールデータが空
        _sch_tbl = soup.find(class_=re.compile(r"sch-tbl"))
        if _sch_tbl and not _sch_tbl.find("dt") and not _sch_tbl.find("td"):
            _needs_playwright = True
    if _needs_playwright:
        log.info(f"    🔧 JSレンダリング検出 → Playwrightで再取得を試行")
        pw_soup = _fetch_with_playwright(schedule_url)
        if pw_soup:
            soup = pw_soup
            _used_playwright = True

    today        = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    # 本日以降の出勤日を取得（本日出勤の判定を含む）
    start_date   = today
    current_year = today.year
    candidates   = []

    # 形式W: weekSchedule形式（friend-menes等）
    # クラス名が table 自体 or 親 div にある場合の両方に対応
    week_tables = []
    for el in soup.find_all(class_=re.compile(r"weekSchedule|week_schedule|week-schedule", re.I)):
        if el.name == "table":
            week_tables.append(el)
        else:
            week_tables.extend(el.find_all("table"))
    for wt in week_tables:
        for row in wt.find_all("tr"):
            ths = row.find_all("th")
            tds = row.find_all("td")
            if not ths or not tds:
                continue
            # th と td が交互に並ぶ形式（1行に7日分等）に対応
            for th, td in zip(ths, tds):
                m = re.search(r"(\d{1,2})/(\d{1,2})", th.get_text())
                if not m:
                    continue
                info = td.get_text(" ", strip=True)
                if "お休み" in info or "未定" in info:
                    continue
                if not _has_work_info(info):
                    continue
                month, day = int(m.group(1)), int(m.group(2))
                d = datetime(current_year, month, day)
                if d >= start_date:
                    candidates.append((d, f"{month}/{day}"))
    if candidates:
        log.info(f"    📅 形式W(weekSchedule)でマッチ")

    if not candidates:
        for table in soup.find_all("table"):
            # 形式A: thに月日、tdに出勤情報（zexterior・rex-luxury等）
            headers = table.find_all("th")
            cells   = table.find_all("td")
            if headers and cells:
                for header, cell in zip(headers, cells):
                    info = cell.get_text(strip=True)
                    if not info or "お休み" in info or "未定" in info:
                        continue
                    if not _has_work_info(info):
                        continue
                    # 「3月5日」または「3/5(木)」形式どちらも対応
                    m = re.search(r"(\d+)月\s*(\d+)日", header.get_text())
                    if not m:
                        m = re.search(r"(\d{1,2})/(\d{1,2})", header.get_text())
                    if m:
                        month, day = int(m.group(1)), int(m.group(2))
                        d = datetime(current_year, month, day)
                        if d >= start_date:
                            candidates.append((d, f"{month}/{day}"))

            # 形式A2: th=日付(1行目), td=店舗名(2行目)+時刻(3行目)の複数行構造
            # （kichijoji-igokochi等: th7個, td14個のように行をまたいで情報が分かれる）
            if not candidates and headers and cells:
                rows = table.find_all("tr")
                if len(rows) >= 3:
                    th_row = [r for r in rows if r.find("th")]
                    td_rows = [r for r in rows if r.find("td") and not r.find("th")]
                    if th_row and len(td_rows) >= 2:
                        date_ths = th_row[0].find_all("th")
                        num_cols = len(date_ths)
                        # 各列の全tdテキストを結合
                        col_infos = [""] * num_cols
                        for td_row in td_rows:
                            tds_in_row = td_row.find_all("td")
                            for ci, td in enumerate(tds_in_row):
                                if ci < num_cols:
                                    col_infos[ci] += " " + td.get_text(" ", strip=True)
                        for ci, th in enumerate(date_ths):
                            m = re.search(r"(\d{1,2})/(\d{1,2})", th.get_text())
                            if not m:
                                continue
                            info = col_infos[ci]
                            if "お休み" in info or "未定" in info:
                                continue
                            if not _has_work_info(info):
                                continue
                            month, day = int(m.group(1)), int(m.group(2))
                            d = datetime(current_year, month, day)
                            if d >= start_date:
                                candidates.append((d, f"{month}/{day}"))
                        if candidates:
                            log.info(f"    📅 形式A2(th日付+td複数行)でマッチ")

            # 形式B: 1行目tdが日付、2行目tdが出勤情報（tennesu等）
            # ※各行に複数列ある場合のみ（namexspaのような縦1列テーブルと区別）
            if not candidates:
                rows = table.find_all("tr")
                if len(rows) >= 2:
                    date_cells = rows[0].find_all("td")
                    info_cells = rows[1].find_all("td")
                    # 日付セルが複数あり、かつ日付パターンを含む場合のみ適用
                    date_matches = [re.search(r"(\d{1,2})/(\d{1,2})", dc.get_text()) for dc in date_cells]
                    valid_dates = [m for m in date_matches if m]
                    if len(valid_dates) >= 2:  # 複数日付=週間スケジュール形式
                        for i, dcell in enumerate(date_cells):
                            m = date_matches[i]
                            if not m:
                                continue
                            month, day = int(m.group(1)), int(m.group(2))
                            d = datetime(current_year, month, day)
                            if d < start_date:
                                continue
                            info = info_cells[i].get_text(" ", strip=True) if i < len(info_cells) else ""
                            if "未定" in info or "お休み" in info:
                                continue
                            if not _has_work_info(info):
                                continue
                            candidates.append((d, f"{month}/{day}"))

            if candidates:
                break

    # パターン定義リスト: 「3/5(木)\n:   15:00」形式（aromaresort等）
    # 日付の直後の行に時刻がある場合のみマッチ（離れた行の時刻は拾わない）
    if not candidates:
        for m in re.finditer(
            r"(\d{1,2})/(\d{1,2})\([月火水木金土日]\)\s*\n\s*:?\s*(\d{2}:\d{2})",
            soup.get_text()
        ):
            month, day = int(m.group(1)), int(m.group(2))
            d = datetime(current_year, month, day)
            if d >= start_date:
                candidates.append((d, f"{month}/{day}"))

    # パターン2: 「3/7 土 10:00〜」形式のテーブル（namexspa・bellee・eldorado等）
    # ※各行が「日付 | 時刻 | 予約リンク」の縦型テーブル
    if not candidates:
        for table in soup.find_all("table"):
            _matched_this_table = False
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                date_text = cells[0].get_text(strip=True)
                info_text = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                m = re.search(r"(\d{1,2})/(\d{1,2})", date_text)
                if not m:
                    continue
                _matched_this_table = True
                month, day = int(m.group(1)), int(m.group(2))
                d = datetime(current_year, month, day)
                if d < start_date:
                    continue
                if "お休み" in info_text or "未定" in info_text:
                    _saw_off[0] = True
                    continue
                if not info_text:
                    # 空セル = 休み (このテーブル形式では空欄で「休み」を表現)
                    _saw_off[0] = True
                    continue
                if _has_work_info(info_text):
                    candidates.append((d, f"{month}/{day}"))
            if _matched_this_table and (candidates or _saw_off[0]):
                log.info(f"    📅 パターン2(縦型 日付|時刻|予約)で処理: "
                         f"candidates={len(candidates)}件, saw_off={_saw_off[0]}")
                break

    # パターンK: krc_cast_calendar形式（アダマス等）
    # div.krc_cast_calendar > ul > li 内に p.day（日付）と p（出勤情報）
    if not candidates:
        cal_div = soup.find("div", class_=re.compile(r"krc_cast_calendar|cast.?calendar", re.I))
        if cal_div:
            for li in cal_div.find_all("li"):
                day_p = li.find("p", class_="day")
                if not day_p:
                    continue
                m = re.search(r"(\d{1,2})/(\d{1,2})", day_p.get_text())
                if not m:
                    continue
                # day_p の次の p が出勤情報
                info_p = day_p.find_next_sibling("p")
                info = info_p.get_text(strip=True) if info_p else ""
                if "休み" in info or "未定" in info:
                    _saw_off[0] = True
                    continue
                if not _has_work_info(info):
                    continue
                month, day = int(m.group(1)), int(m.group(2))
                d = datetime(current_year, month, day)
                if d >= start_date:
                    candidates.append((d, f"{month}/{day}"))
            if candidates:
                log.info(f"    📅 形式K(krc_cast_calendar)でマッチ")

    # パターンM: men-este形式（tokyo-fairy-land・omiya-mens-este等）
    # 複数のHTML構造バリエーションに対応:
    #   M1: div.sch-date > dt に日付、div.sch-work > dd に出勤情報（複数週対応）
    #   M2: dt.sch-date / dd.sch-work が直接 dl 内に並ぶ形式
    #   M3: sch-tbl 内の dl > dt + dd ペア（sch-date/sch-work サブクラスなし）
    #   M4: sch-date/sch-work が div 以外の要素（span, li 等）に付与されている形式
    if not candidates:
        all_sch_dates = soup.find_all("div", class_=re.compile(r"sch-date"))
        all_sch_works = soup.find_all("div", class_=re.compile(r"sch-work"))
        if all_sch_dates and all_sch_works:
            log.info(f"    🔧 形式M1: sch-date={len(all_sch_dates)}個, sch-work={len(all_sch_works)}個")
            for sch_date, sch_work in zip(all_sch_dates, all_sch_works):
                dts = sch_date.find_all("dt")
                dds = sch_work.find_all("dd")
                if not dts or not dds:
                    # dt/dd がない場合、子要素のタグ名をログに記録し直接テキストで試行
                    child_tags = [c.name for c in sch_date.children if hasattr(c, 'name') and c.name]
                    log.info(f"    🔧 形式M1: dt/ddなし (sch-date子要素: {child_tags[:5]}), テキスト抽出を試行")
                    date_text = sch_date.get_text(strip=True)
                    work_text = sch_work.get_text(strip=True)
                    m = re.search(r"(\d{1,2})/(\d{1,2})", date_text)
                    if m and ("休み" not in work_text and "未定" not in work_text):
                        if _has_work_info(work_text):
                            month, day = int(m.group(1)), int(m.group(2))
                            d = datetime(current_year, month, day)
                            if d >= start_date:
                                candidates.append((d, f"{month}/{day}"))
                    continue
                for dt_el, dd_el in zip(dts, dds):
                    info = dd_el.get_text(strip=True)
                    if "休み" in info or "未定" in info:
                        _saw_off[0] = True
                        continue
                    if not _has_work_info(info):
                        continue
                    m = re.search(r"(\d{1,2})/(\d{1,2})", dt_el.get_text())
                    if not m:
                        continue
                    month, day = int(m.group(1)), int(m.group(2))
                    d = datetime(current_year, month, day)
                    if d >= start_date:
                        candidates.append((d, f"{month}/{day}"))
            if candidates:
                log.info(f"    📅 形式M1(sch-date/sch-work div)でマッチ")

    # M2: dt.sch-date / dd.sch-work がdl内に直接並ぶ形式
    if not candidates:
        dt_sch = soup.find_all("dt", class_=re.compile(r"sch-date"))
        dd_sch = soup.find_all("dd", class_=re.compile(r"sch-work"))
        if dt_sch and dd_sch:
            log.info(f"    🔧 形式M2: dt.sch-date={len(dt_sch)}個, dd.sch-work={len(dd_sch)}個")
            for dt_el, dd_el in zip(dt_sch, dd_sch):
                info = dd_el.get_text(strip=True)
                if "休み" in info or "未定" in info:
                    _saw_off[0] = True
                    continue
                if not _has_work_info(info):
                    continue
                m = re.search(r"(\d{1,2})/(\d{1,2})", dt_el.get_text())
                if not m:
                    continue
                month, day = int(m.group(1)), int(m.group(2))
                d = datetime(current_year, month, day)
                if d >= start_date:
                    candidates.append((d, f"{month}/{day}"))
            if candidates:
                log.info(f"    📅 形式M2(dt.sch-date/dd.sch-work)でマッチ")

    # M3: sch-tbl 内の dl > dt + dd ペア
    if not candidates:
        sch_tbl = soup.find(class_=re.compile(r"sch-tbl"))
        if sch_tbl:
            dts = sch_tbl.find_all("dt")
            dds = sch_tbl.find_all("dd")
            if dts and dds:
                log.info(f"    🔧 形式M3: sch-tbl内 dt={len(dts)}個, dd={len(dds)}個")
                for dt_el, dd_el in zip(dts, dds):
                    info = dd_el.get_text(strip=True)
                    if "休み" in info or "未定" in info:
                        _saw_off[0] = True
                        continue
                    if not _has_work_info(info):
                        continue
                    m = re.search(r"(\d{1,2})/(\d{1,2})", dt_el.get_text())
                    if not m:
                        continue
                    month, day = int(m.group(1)), int(m.group(2))
                    d = datetime(current_year, month, day)
                    if d >= start_date:
                        candidates.append((d, f"{month}/{day}"))
                if candidates:
                    log.info(f"    📅 形式M3(sch-tbl dl)でマッチ")

    # M4: sch-date/sch-work が任意の要素（span, li, p 等）に付与されている形式
    if not candidates:
        any_sch_dates = soup.find_all(class_=re.compile(r"sch-date"))
        any_sch_works = soup.find_all(class_=re.compile(r"sch-work"))
        # div版で見つからなかった場合のみ（div版は上で処理済み）
        if any_sch_dates and any_sch_works and not all_sch_dates:
            log.info(f"    🔧 形式M4: sch-date({any_sch_dates[0].name})={len(any_sch_dates)}個, sch-work({any_sch_works[0].name})={len(any_sch_works)}個")
            for date_el, work_el in zip(any_sch_dates, any_sch_works):
                info = work_el.get_text(strip=True)
                if "休み" in info or "未定" in info:
                    _saw_off[0] = True
                    continue
                if not _has_work_info(info):
                    continue
                m = re.search(r"(\d{1,2})/(\d{1,2})", date_el.get_text())
                if not m:
                    continue
                month, day = int(m.group(1)), int(m.group(2))
                d = datetime(current_year, month, day)
                if d >= start_date:
                    candidates.append((d, f"{month}/{day}"))
            if candidates:
                log.info(f"    📅 形式M4(sch-date/sch-work 汎用)でマッチ")

    # パターンP: profile_list形式（liora2024等）
    # div.profile_list > p.p_day(日のみ: "25(水)") + p.p_check(時刻: "10:00 - 15:00")
    if not candidates:
        prof_lists = soup.find_all("div", class_=re.compile(r"profile_list"))
        if prof_lists:
            current_month = today.month
            for pl in prof_lists:
                day_p = pl.find("p", class_=re.compile(r"p_day"))
                check_p = pl.find("p", class_=re.compile(r"p_check"))
                if not day_p or not check_p:
                    continue
                m = re.search(r"(\d{1,2})\s*\(", day_p.get_text())
                if not m:
                    continue
                info = check_p.get_text(strip=True)
                if info == "-" or "休み" in info or "未定" in info:
                    _saw_off[0] = True
                    continue
                if not _has_work_info(info):
                    continue
                day = int(m.group(1))
                # 月情報がないので当月を基準に、日が今日より小さければ翌月と推定
                month = current_month
                if day < today.day - 7:
                    month = current_month + 1 if current_month < 12 else 1
                d = datetime(current_year if month >= current_month else current_year + 1, month, day)
                if d >= start_date:
                    candidates.append((d, f"{month}/{day}"))
            if candidates:
                log.info(f"    📅 形式P(profile_list)でマッチ")

    # 形式L: <li>内に<p>×2 (日本語日付+時刻/―) 形式 (aroma-miely等)
    # <li><p>08月15日（土）</p><p>21:00～翌05:00</p></li>
    # <li><p>08月17日（月）</p><p>―</p></li>
    if not candidates:
        for li in soup.find_all("li"):
            ps = li.find_all("p")
            if len(ps) < 2:
                continue
            date_text = ps[0].get_text(strip=True)
            time_text = ps[1].get_text(strip=True)
            m = re.search(r"(\d{1,2})月\s*(\d{1,2})日", date_text)
            if not m:
                continue
            # ―(オフマーカー) / 休み / 未定 / 空文字
            if (time_text in ("―", "-", "－", "‐", "")
                    or "休み" in time_text or "未定" in time_text):
                _saw_off[0] = True
                continue
            if not _has_work_info(time_text):
                continue
            month, day = int(m.group(1)), int(m.group(2))
            d = datetime(current_year, month, day)
            if d >= start_date:
                candidates.append((d, f"{month}/{day}"))
        if candidates:
            log.info(f"    📅 形式L(li>p×2 日本語日付+時刻)でマッチ")

    # パターン3: div構造の日付+出勤情報（tennesu等）
    if not candidates:
        date_divs = soup.find_all("div", class_=re.compile(r"date"))
        sche_divs = soup.find_all("div", class_=re.compile(r"sche"))
        if date_divs and sche_divs:
            for i, date_div in enumerate(date_divs):
                m = re.search(r"(\d{1,2})/(\d{1,2})", date_div.get_text())
                if not m:
                    continue
                month, day = int(m.group(1)), int(m.group(2))
                d = datetime(current_year, month, day)
                if d < start_date:
                    continue
                if i < len(sche_divs):
                    info = sche_divs[i].get_text(" ", strip=True)
                    if "未定" in info or "お休み" in info:
                        continue
                    if not _has_work_info(info):
                        continue
                candidates.append((d, f"{month}/{day}"))

    # パターン5: 「3/5(木)20:00」同一行形式（tokyo-menes・galaxy等）
    if not candidates:
        for m in re.finditer(r"(\d{1,2})/(\d{1,2})\([月火水木金土日]\)[^\n]{0,5}(\d{2}:\d{2})", soup.get_text()):
            month, day = int(m.group(1)), int(m.group(2))
            d = datetime(current_year, month, day)
            if d >= start_date:
                candidates.append((d, f"{month}/{day}"))

    # パターン4: 「03/05\n(木)\n武蔵小杉出勤 13:00」形式（tennesu等・日付と時刻が別行）
    if not candidates:
        text = soup.get_text()
        for m in re.finditer(r"(\d{1,2})/(\d{1,2})\s*\n\s*\([月火水木金土日]\)((?:\n[^\n]*){1,5}?)(\d{2}:\d{2})", text):
            month, day = int(m.group(1)), int(m.group(2))
            # 間の行が「未定」のみなら出勤なし
            between = m.group(3)
            if "未定" in between and re.search(r"\d{2}:\d{2}", between) is None:
                continue
            d = datetime(current_year, month, day)
            if d >= start_date:
                candidates.append((d, f"{month}/{day}"))

    # パターン5: 「3月7日」テキスト形式
    if not candidates:
        for m in re.finditer(r"(\d{1,2})月(\d{1,2})日[^\n]*?(\d{2}:\d{2})", soup.get_text()):
            month, day = int(m.group(1)), int(m.group(2))
            d = datetime(current_year, month, day)
            if d >= start_date:
                candidates.append((d, f"{month}/{day}"))

    # パターン6: 「4/5(土) 出勤」形式（時刻なし・出勤/◯のみ）
    if not candidates:
        text = soup.get_text()
        for m in re.finditer(r"(\d{1,2})/(\d{1,2})\s*\([月火水木金土日]\)\s*([^\n]{0,30})", text):
            line_rest = m.group(3).strip()
            if "休み" in line_rest or "未定" in line_rest:
                _saw_off[0] = True
                continue
            if not _has_work_info(line_rest) and line_rest:
                continue
            if not line_rest:
                continue
            month, day = int(m.group(1)), int(m.group(2))
            d = datetime(current_year, month, day)
            if d >= start_date:
                candidates.append((d, f"{month}/{day}"))
        if candidates:
            log.info(f"    📅 パターン6(日付+出勤テキスト)でマッチ")

    # 全パーサー失敗 → Playwright未使用ならフォールバック再取得して再解析
    if not candidates and not _used_playwright:
        log.info(f"    🔧 全パーサー失敗 → Playwrightで再取得を試行")
        pw_soup = _fetch_with_playwright(schedule_url)
        if pw_soup:
            soup = pw_soup
            _used_playwright = True
            # 再帰ではなく主要パターンだけ再チェック
            # Format M (men-este) — 複数バリエーション対応
            # M1: div.sch-date > dt / div.sch-work > dd
            all_sch_dates = soup.find_all("div", class_=re.compile(r"sch-date"))
            all_sch_works = soup.find_all("div", class_=re.compile(r"sch-work"))
            if all_sch_dates and all_sch_works:
                log.info(f"    🔧 PW形式M1: sch-date={len(all_sch_dates)}個, sch-work={len(all_sch_works)}個")
                for sch_date, sch_work in zip(all_sch_dates, all_sch_works):
                    dts = sch_date.find_all("dt")
                    dds = sch_work.find_all("dd")
                    if not dts or not dds:
                        date_text = sch_date.get_text(strip=True)
                        work_text = sch_work.get_text(strip=True)
                        m = re.search(r"(\d{1,2})/(\d{1,2})", date_text)
                        if m and ("休み" not in work_text and "未定" not in work_text):
                            if _has_work_info(work_text):
                                month, day = int(m.group(1)), int(m.group(2))
                                d = datetime(current_year, month, day)
                                if d >= start_date:
                                    candidates.append((d, f"{month}/{day}"))
                        continue
                    for dt_el, dd_el in zip(dts, dds):
                        info = dd_el.get_text(strip=True)
                        if "休み" in info or "未定" in info:
                            _saw_off[0] = True
                            continue
                        if not _has_work_info(info):
                            continue
                        m = re.search(r"(\d{1,2})/(\d{1,2})", dt_el.get_text())
                        if m:
                            month, day = int(m.group(1)), int(m.group(2))
                            d = datetime(current_year, month, day)
                            if d >= start_date:
                                candidates.append((d, f"{month}/{day}"))
            # M2: dt.sch-date / dd.sch-work がdl内に直接並ぶ形式
            if not candidates:
                dt_sch = soup.find_all("dt", class_=re.compile(r"sch-date"))
                dd_sch = soup.find_all("dd", class_=re.compile(r"sch-work"))
                if dt_sch and dd_sch:
                    log.info(f"    🔧 PW形式M2: dt.sch-date={len(dt_sch)}個, dd.sch-work={len(dd_sch)}個")
                    for dt_el, dd_el in zip(dt_sch, dd_sch):
                        info = dd_el.get_text(strip=True)
                        if "休み" in info or "未定" in info:
                            _saw_off[0] = True
                            continue
                        if not _has_work_info(info):
                            continue
                        m = re.search(r"(\d{1,2})/(\d{1,2})", dt_el.get_text())
                        if m:
                            month, day = int(m.group(1)), int(m.group(2))
                            d = datetime(current_year, month, day)
                            if d >= start_date:
                                candidates.append((d, f"{month}/{day}"))
            # M3: sch-tbl 内の dl > dt + dd ペア
            if not candidates:
                sch_tbl = soup.find(class_=re.compile(r"sch-tbl"))
                if sch_tbl:
                    dts = sch_tbl.find_all("dt")
                    dds = sch_tbl.find_all("dd")
                    if dts and dds:
                        log.info(f"    🔧 PW形式M3: sch-tbl内 dt={len(dts)}個, dd={len(dds)}個")
                        for dt_el, dd_el in zip(dts, dds):
                            info = dd_el.get_text(strip=True)
                            if "休み" in info or "未定" in info:
                                _saw_off[0] = True
                                continue
                            if not _has_work_info(info):
                                continue
                            m = re.search(r"(\d{1,2})/(\d{1,2})", dt_el.get_text())
                            if m:
                                month, day = int(m.group(1)), int(m.group(2))
                                d = datetime(current_year, month, day)
                                if d >= start_date:
                                    candidates.append((d, f"{month}/{day}"))
            # M4: sch-date/sch-work が任意の要素に付与
            if not candidates and not all_sch_dates:
                any_sch_dates = soup.find_all(class_=re.compile(r"sch-date"))
                any_sch_works = soup.find_all(class_=re.compile(r"sch-work"))
                if any_sch_dates and any_sch_works:
                    log.info(f"    🔧 PW形式M4: sch-date({any_sch_dates[0].name})={len(any_sch_dates)}個")
                    for date_el, work_el in zip(any_sch_dates, any_sch_works):
                        info = work_el.get_text(strip=True)
                        if "休み" in info or "未定" in info:
                            _saw_off[0] = True
                            continue
                        if not _has_work_info(info):
                            continue
                        m = re.search(r"(\d{1,2})/(\d{1,2})", date_el.get_text())
                        if m:
                            month, day = int(m.group(1)), int(m.group(2))
                            d = datetime(current_year, month, day)
                            if d >= start_date:
                                candidates.append((d, f"{month}/{day}"))
            # Format P (profile_list)
            if not candidates:
                prof_lists = soup.find_all("div", class_=re.compile(r"profile_list"))
                if prof_lists:
                    current_month = today.month
                    for pl in prof_lists:
                        day_p = pl.find("p", class_=re.compile(r"p_day"))
                        check_p = pl.find("p", class_=re.compile(r"p_check"))
                        if not day_p or not check_p:
                            continue
                        m = re.search(r"(\d{1,2})\s*\(", day_p.get_text())
                        if not m:
                            continue
                        info = check_p.get_text(strip=True)
                        if info == "-" or "休み" in info or "未定" in info:
                            _saw_off[0] = True
                            continue
                        if not _has_work_info(info):
                            continue
                        day = int(m.group(1))
                        month = current_month
                        if day < today.day - 7:
                            month = current_month + 1 if current_month < 12 else 1
                        d = datetime(current_year if month >= current_month else current_year + 1, month, day)
                        if d >= start_date:
                            candidates.append((d, f"{month}/{day}"))
            # テーブル系 (W, A, B) — 同一行 th+td と、別行(headerTr/bodyTr)の両方に対応
            if not candidates:
                for table in soup.find_all("table"):
                    # まず同一行内の th+td をチェック
                    for row in table.find_all("tr"):
                        ths = row.find_all("th")
                        tds = row.find_all("td")
                        for th, td in zip(ths, tds):
                            m = re.search(r"(\d{1,2})/(\d{1,2})", th.get_text())
                            if not m:
                                continue
                            info = td.get_text(" ", strip=True)
                            if "お休み" in info or "未定" in info:
                                continue
                            if not _has_work_info(info):
                                continue
                            month, day = int(m.group(1)), int(m.group(2))
                            d = datetime(current_year, month, day)
                            if d >= start_date:
                                candidates.append((d, f"{month}/{day}"))
                    # 別行(headerTr=th, bodyTr=td)の場合: テーブル全体のth/tdをzip
                    if not candidates:
                        headers = table.find_all("th")
                        cells = table.find_all("td")
                        if headers and cells:
                            for header, cell in zip(headers, cells):
                                h_text = header.get_text()
                                m = re.search(r"(\d{1,2})/(\d{1,2})", h_text)
                                if not m:
                                    continue
                                info = cell.get_text(" ", strip=True)
                                if "お休み" in info or "未定" in info:
                                    continue
                                if not _has_work_info(info):
                                    continue
                                month, day = int(m.group(1)), int(m.group(2))
                                d = datetime(current_year, month, day)
                                if d >= start_date:
                                    candidates.append((d, f"{month}/{day}"))
                    if candidates:
                        break
            # テキスト正規表現
            if not candidates:
                for m in re.finditer(r"(\d{1,2})/(\d{1,2})\([月火水木金土日]\)[^\n]{0,5}(\d{2}:\d{2})", soup.get_text()):
                    month, day = int(m.group(1)), int(m.group(2))
                    d = datetime(current_year, month, day)
                    if d >= start_date:
                        candidates.append((d, f"{month}/{day}"))
            # パターン6: 時刻なし・出勤/◯のみ
            if not candidates:
                text = soup.get_text()
                for m in re.finditer(r"(\d{1,2})/(\d{1,2})\s*\([月火水木金土日]\)\s*([^\n]{0,30})", text):
                    line_rest = m.group(3).strip()
                    if "休み" in line_rest or "未定" in line_rest:
                        _saw_off[0] = True
                        continue
                    if not _has_work_info(line_rest) and line_rest:
                        continue
                    if not line_rest:
                        continue
                    month, day = int(m.group(1)), int(m.group(2))
                    d = datetime(current_year, month, day)
                    if d >= start_date:
                        candidates.append((d, f"{month}/{day}"))
            if candidates:
                log.info(f"    📅 Playwrightフォールバックでマッチ")

    if not candidates:
        # デバッグ: どのパターンにもマッチしなかった場合、HTML構造をダンプ
        text_snippet = soup.get_text()[:500].replace("\n", "\\n")
        log.warning(f"    🔧 スケジュール解析失敗 URL={schedule_url}")
        log.warning(f"    🔧 テキスト先頭500字: {text_snippet}")
        # テーブル構造のダンプ
        tables = soup.find_all("table")
        log.warning(f"    🔧 table数={len(tables)}")
        for i, t in enumerate(tables[:3]):
            log.warning(f"    🔧 table[{i}] HTML先頭300字: {str(t)[:300]}")
        # div構造のダンプ（スケジュール関連クラス）
        for cls in ("schedule", "sche", "date", "shift", "calendar", "week", "profile"):
            divs = soup.find_all(["div", "dl", "ul", "li", "span"], class_=re.compile(cls, re.I))
            if divs:
                log.warning(f"    🔧 class~'{cls}' 要素数={len(divs)} 先頭: {str(divs[0])[:200]}")
        return [], False, False, _saw_off[0]

    candidates.sort(key=lambda x: x[0])
    # 重複除去
    seen = set()
    unique = []
    for dt, s in candidates:
        if s not in seen:
            seen.add(s)
            unique.append((dt, s))

    # 本日出勤の判定
    is_today = any(dt.date() == today.date() for dt, _ in unique)

    # タイトル用の日付（直近3件まで）
    # start_from_tomorrow=True (16:30モード): 明日以降
    # start_from_tomorrow=False (0時モード): 本日以降
    cutoff = (today + timedelta(days=1)).date() if start_from_tomorrow else today.date()
    future = [(dt, s) for dt, s in unique if dt.date() >= cutoff]
    future = future[:3]

    if not future:
        return [], False, is_today, _saw_off[0]

    dates = [s for _, s in future]
    tomorrow = today + timedelta(days=1)
    is_tomorrow = (future[0][0].date() == tomorrow.date())
    return dates, is_tomorrow, is_today, _saw_off[0]


# ============================================================
# タイトルの【日付出勤】部分を置換
# ============================================================
def format_dates(dates):
    """日付リストを月ごとにグループ化してフォーマット
    同月の日付はドットで繋ぎ月を省略、異なる月は | で区切る
    例: ["3/28", "4/3", "4/4", "4/5"] → "3/28 | 4/3.4.5"
    例: ["3/21", "3/22", "4/2"] → "3/21.22 | 4/2"
    """
    if not dates:
        return ""
    from collections import OrderedDict
    groups = OrderedDict()
    for d in dates:
        if "/" in d:
            month, day = d.split("/", 1)
            groups.setdefault(month, []).append(day)
    parts = []
    for month, days in groups.items():
        parts.append(f"{month}/{'.'.join(days)}")
    return " | ".join(parts)


TODAY_TAG = " #本日出勤"
# カテゴリ → タイトル末尾に付与する地域タグ
CATEGORY_AREA_TAG = {
    "東京都":   " #東京都内",
    "新宿":     " #東京都内",
    "池袋":     " #東京都内",
    "神奈川県": " #神奈川",
    "千葉県":   " #千葉",
    "埼玉県":   " #埼玉",
}
# _strip_today_tag で除去する地域タグ（重複防止用）
_AREA_TAG_STRIP_RE = re.compile(r"\s*#(?:東京都内|神奈川|千葉|埼玉)")

def _strip_today_tag(title):
    """タイトルから #本日出勤 タグと日付ハッシュタグ、地域タグを除去する"""
    title = title.replace(TODAY_TAG, "")
    title = _AREA_TAG_STRIP_RE.sub("", title)
    title = re.sub(r"\s*#[\d/,]+$", "", title)
    return title.rstrip()


def _clear_shift_dates_from_title(title):
    """タイトルから【M/D...出勤】バケツと日付ハッシュタグ、本日出勤/地域タグを全て除去。
    「スケジュール取得成功・全休み」時に使用（古いシフト日付を残さない）。
    例: 【8/10.11出勤】【Fカップ】... #8/10,8/11 #本日出勤 #東京都内
         → 【Fカップ】...
    """
    title = _strip_today_tag(title)

    def _replace_bracket(m):
        inner = m.group(1)
        if not re.search(r"[\d/.,｜|\s]+出勤", inner):
            return m.group(0)
        # 日付+出勤を除去してカップ数等が残ればbracket維持
        cleaned = re.sub(r"[\d/.,｜|\s]+出勤", "", inner)
        cleaned = re.sub(r"[\d/.,｜|\s]+", "", cleaned).strip()
        return f"【{cleaned}】" if cleaned else ""

    title = re.sub(r"【([^】]*)】", _replace_bracket, title, count=1)
    return title.strip()


def _extract_dates_from_title(title):
    """タイトルの【日付出勤】部分から日付リストを抽出するフォールバック。

    例: "【4/12.13出勤Iカップ】名前" → ["4/12", "4/13"]
    例: "【3/28 | 4/3.4出勤】名前" → ["3/28", "4/3", "4/4"]
    日付が見つからない場合は空リストを返す。
    """
    m = re.search(r"【([\d/.,｜|\s]+)出勤", title)
    if not m:
        return []
    raw = m.group(1).strip()
    dates = []
    groups = re.split(r"[|｜]", raw)
    for group in groups:
        group = group.strip()
        if "/" not in group:
            continue
        m_group = re.match(r"(\d+)/([\d.]+)", group)
        if m_group:
            month = m_group.group(1)
            days_str = m_group.group(2)
            days = [d for d in days_str.split(".") if d]
            for d in days:
                dates.append(f"{month}/{d}")
    return dates


def _fallback_dates_from_title_or_state(title, post_id, state):
    """スケジュール取得失敗時にタイトルまたはstateから日付を復元する。

    優先順位:
      1. タイトルの【日付出勤】から抽出（最新のタイトルが最も信頼できる）
      2. stateファイルの前回保存日付
    戻り値: (dates_str, dates_list) - dates_strはカンマ区切り文字列、dates_listはリスト
            日付が見つからない場合は (None, [])
    """
    # 1. タイトルから抽出
    title_dates = _extract_dates_from_title(title)
    if title_dates:
        # 過去の日付を除外（今日以降のみ）
        today_dt = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        current_year = today_dt.year
        future_dates = []
        for d in title_dates:
            try:
                parts = d.split("/")
                m, dy = int(parts[0]), int(parts[1])
                dt = datetime(current_year, m, dy)
                if dt >= today_dt:
                    future_dates.append(d)
            except (ValueError, IndexError):
                continue
        if future_dates:
            dates_str = ",".join(future_dates)
            log.info(f"    📋 タイトルから日付を復元: {dates_str}")
            return dates_str, future_dates

    # 2. stateから復元
    post_state = state.get(post_id, {})
    saved_dates = post_state.get("dates")
    if saved_dates:
        # 過去の日付を除外
        today_dt = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        current_year = today_dt.year
        date_list = [d.strip() for d in saved_dates.split(",") if "/" in d]
        future_dates = []
        for d in date_list:
            try:
                parts = d.split("/")
                m, dy = int(parts[0]), int(parts[1])
                dt = datetime(current_year, m, dy)
                if dt >= today_dt:
                    future_dates.append(d)
            except (ValueError, IndexError):
                continue
        if future_dates:
            dates_str = ",".join(future_dates)
            log.info(f"    📋 前回のstateから日付を復元: {dates_str}")
            return dates_str, future_dates

    return None, []


def build_new_title(current_title, dates):
    # dates: リスト（例: ["3/13", "3/14", "3/15"]）
    # 【】内に日付+出勤パターンがあれば置換（カップ数等は保持）
    # 重複（【3/5出勤3/5出勤Iカップ】等）も同時に修正する
    # replacedフラグで「置換が実際に起きたか」を管理し、二重追加を防ぐ
    current_title = _strip_today_tag(current_title)  # 前回の #本日出勤 を除去
    # 既存のアルファベットタグバッジ（【PZ】【CK | F】等）を除去
    current_title = re.sub(r"【[A-Za-z]+(?:\s*\|\s*[A-Za-z]+)*】", "", current_title)
    date_str = format_dates(dates)
    replaced = [False]

    def replace_bracket(m):
        inner = m.group(1)
        if not re.search(r"[\d/.,｜|\s]+出勤", inner):
            return m.group(0)  # 日付+出勤がなければそのまま
        # 日付+出勤パターンを除去（全角・半角パイプ両対応）
        inner_clean = re.sub(r"[\d/.,｜|\s]+出勤", "", inner)
        # 前回のバグ等で残った孤立日付フラグメント（例: "3/28 | "）も除去
        inner_clean = re.sub(r"[\d/.,｜|\s]+", "", inner_clean)
        replaced[0] = True
        return f"【{date_str}出勤{inner_clean}】"

    new_title = re.sub(r"【([^】]*)】", replace_bracket, current_title, count=1)

    if not replaced[0]:
        new_title = f"【{date_str}出勤】" + current_title
    return new_title


# ============================================================
# 回遊リスト（本日・直近出勤の他記事リンク）の生成・注入
# ============================================================
def build_related_html(all_post_infos, current_post_id, current_category=None, title_only=False):
    """出勤グループ別の回遊リストを生成（更新した全記事対象）

    title_only=False (0時モード): グループ1=本日出勤、グループ2=明日出勤
    title_only=True (16:30モード): グループ1=明日出勤、グループ2=明後日出勤

    カテゴリ回遊ルール:
      - 神奈川県: 神奈川県内のみで回遊
      - 埼玉県: 埼玉県内のみで回遊
      - 千葉県: 千葉県内のみで回遊
      - 多摩: 多摩内のみで回遊
      - 東京都/池袋/新宿: 互いに回遊OK
    """
    others = [p for p in all_post_infos if p["post"]["id"] != current_post_id]

    # カテゴリ別回遊フィルタリング
    # 神奈川県/埼玉県/千葉県: 同県同士のみ / それ以外: 神奈川県・埼玉県・千葉県以外すべてで回遊
    LOCAL_ONLY_CATEGORIES = {"神奈川県", "埼玉県", "千葉県", "多摩"}
    if current_category:
        if current_category in LOCAL_ONLY_CATEGORIES:
            others = [p for p in others if p["post"].get("category") == current_category]
        else:
            others = [p for p in others if p["post"].get("category") not in LOCAL_ONLY_CATEGORIES]

    from datetime import datetime
    today_dt = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    tomorrow_dt = today_dt + timedelta(days=1)
    day_after_dt = today_dt + timedelta(days=2)

    def _first_date_dt(info):
        """next_dateの最初の日付をdatetimeに変換"""
        if info["next_date"] is None:
            return None
        try:
            first_date = info["next_date"].split(",")[0]
            m, d = first_date.split("/")
            return datetime(today_dt.year, int(m), int(d))
        except Exception:
            return None

    # title_only=False (0時モード): グループ1=本日、グループ2=明日
    # title_only=True (16:30モード): グループ1=明日、グループ2=明後日
    if title_only:
        group1_dt = tomorrow_dt
        group2_dt = day_after_dt
        label1 = f"📅 明日{tomorrow_dt.month}/{tomorrow_dt.day}出勤予定の他の記事もチェック！"
        label2 = f"📆 明後日{day_after_dt.month}/{day_after_dt.day}出勤予定の他の記事もチェック！"
    else:
        group1_dt = today_dt
        group2_dt = tomorrow_dt
        label1 = f"📅 本日{today_dt.month}/{today_dt.day}出勤予定の他の記事もチェック！"
        label2 = f"📆 明日{tomorrow_dt.month}/{tomorrow_dt.day}出勤予定の他の記事もチェック！"

    group1 = [p for p in others if _first_date_dt(p) is not None and _first_date_dt(p).date() == group1_dt.date()]
    group2 = [p for p in others if _first_date_dt(p) is not None and _first_date_dt(p).date() == group2_dt.date()]

    if not group1 and not group2:
        return ""

    def _parse_title_badges(title):
        """タイトルから【】バッジ部分とメイン見出しを分離する"""
        brackets = re.findall(r"【([^】]+)】", title)
        schedule = ""
        area = ""
        cup = ""
        for b in brackets:
            if re.search(r"[A-Z]カップ", b):
                cup = re.search(r"[A-Z]カップ", b).group()
            elif "出勤" in b:
                schedule = b
            else:
                area = b
        # メイン見出し = バッジ部分をすべて除去した残り
        main = re.sub(r"【[^】]*】", "", title).strip()
        return schedule, area, cup, main

    def _build_card_list(group, label):
        """グループを2列カード型HTMLに変換する（CTA付きスマホ最適化）"""
        group = sorted(group, key=lambda p: p["post"].get("sales_count") or 0, reverse=True)
        group = group[:2]
        rows = ""
        for idx in range(0, len(group), 2):
            rows += '<tr>'
            for col in range(2):
                if idx + col < len(group):
                    info = group[idx + col]
                    title = _strip_today_tag(info["new_title"] or info["post"]["title"])
                    url   = info["post"]["url"]
                    schedule, area, cup, main = _parse_title_badges(title)
                    badge_html = ""
                    if schedule:
                        badge_html += (
                            f'<span style="display:inline-block;background:#2d8a4e;color:#fff;'
                            f'font-size:11px;padding:2px 8px;border-radius:4px;margin-right:4px">'
                            f'{schedule}</span>'
                        )
                    if area:
                        badge_html += (
                            f'<span style="display:inline-block;background:#4a90d9;color:#fff;'
                            f'font-size:11px;padding:2px 8px;border-radius:4px;margin-right:4px">'
                            f'{area}</span>'
                        )
                    if cup:
                        badge_html += (
                            f'<span style="display:inline-block;background:#e85d75;color:#fff;'
                            f'font-size:11px;padding:2px 8px;border-radius:4px;margin-right:4px">'
                            f'{cup}</span>'
                        )
                    post_tags = info.get("tags", [])
                    if post_tags:
                        badge_html += (
                            f'<span style="display:inline-block;background:#d48806;color:#fff;'
                            f'font-size:11px;padding:2px 8px;border-radius:4px;margin-right:4px">'
                            f'{" | ".join(post_tags)}</span>'
                        )
                    cell_content = ""
                    if badge_html:
                        cell_content += f'<div style="margin-bottom:4px">{badge_html}</div>'
                    cell_content += (
                        f'<div style="font-size:12px;line-height:1.4;font-weight:500;'
                        f'color:#6db3f2;margin-bottom:6px">{main}</div>'
                    )
                    img_url = info.get("image_url")
                    if img_url:
                        cell_content += (
                            f'<div style="margin-bottom:8px">'
                            f'<img src="{img_url}" alt="{main}" '
                            f'style="width:100%;height:auto;border-radius:6px;'
                            f'object-fit:cover;display:block" />'
                            f'</div>'
                        )
                    # CTAボタン
                    cell_content += (
                        f'<div style="text-align:center">'
                        f'<a href="{url}" style="display:block;background:linear-gradient(135deg,#e91e8c,#ff69b4);'
                        f'color:#fff;text-decoration:none;font-size:13px;font-weight:bold;'
                        f'padding:8px 12px;border-radius:6px;'
                        f'box-shadow:0 2px 8px rgba(233,30,140,0.3)">'
                        f'この子を見る &raquo;</a>'
                        f'</div>'
                    )
                    rows += (
                        f'<td style="width:50%;vertical-align:top;padding:4px">'
                        f'<a href="{url}" style="text-decoration:none;color:inherit;display:block">'
                        f'<div style="background:rgba(255,255,255,0.05);border-radius:8px;'
                        f'padding:8px 10px;border:1px solid rgba(255,255,255,0.08)">'
                        f'{cell_content}</div></a></td>'
                    )
                else:
                    rows += '<td style="width:50%"></td>'
            rows += '</tr>'
        return (
            f'<p style="margin-bottom:8px"><strong>{label}</strong></p>\n'
            f'<table style="width:100%;border-collapse:collapse;border-spacing:0"><tbody>'
            f'{rows}</tbody></table>\n'
        )

    inner = "<hr/>\n"

    if group1:
        inner += _build_card_list(group1, label1)

    if group1 and group2:
        inner += (
            '<hr style="border:none;border-top:1px solid #555;margin:12px 0"/>\n'
        )

    if group2:
        inner += _build_card_list(group2, label2)

    # カテゴリに対応するカレンダー記事へのリンク
    if current_category and current_category in CATEGORY_CALENDAR_URL:
        cal_info = CATEGORY_CALENDAR_URL[current_category]
        inner += (
            '<hr style="border:none;border-top:1px solid #555;margin:12px 0"/>\n'
            f'<div style="text-align:center;padding:8px 0">'
            f'<a href="{cal_info["url"]}" style="display:inline-block;background:linear-gradient(135deg,#6c5ce7,#a29bfe);'
            f'color:#fff;text-decoration:none;font-size:13px;font-weight:bold;'
            f'padding:8px 16px;border-radius:8px;box-shadow:0 2px 6px rgba(0,0,0,0.15)">'
            f'🗓️ {cal_info["label"]} 出勤カレンダーを見る</a>'
            f'</div>\n'
        )

    return f'\n{RELATED_BLOCK_START}\n{inner}{RELATED_BLOCK_END}\n'


def build_paid_preview_html(image_url=None):
    """有料パートの案内ブロックHTMLを生成する。

    回遊リスト・カレンダー誘導の後に挿入し、
    購入後に閲覧できる情報を読者に提示する。
    """
    img_html = ""
    if image_url:
        img_html = (
            '<div style="text-align:center;padding:0 16px 12px">'
            f'<img src="{image_url}" alt="" '
            'style="width:100%;height:auto;border-radius:6px;display:block;pointer-events:none" />'
            '</div>'
        )
    return (
        f'\n{PAID_PREVIEW_START}\n'
        '<div style="margin:24px 0 16px;border-radius:8px;overflow:hidden">'
        '<div style="background:linear-gradient(90deg,#e91e8c,#ff69b4);'
        'padding:10px 16px;display:flex;align-items:center">'
        '<span style="font-size:18px;margin-right:8px">🔒</span>'
        '<span style="color:#fff;font-size:16px;font-weight:bold">この子に会いたくなったら…</span>'
        '</div>'
        '<div style="padding:12px 16px;font-size:14px;line-height:1.8">'
        '<p style="margin:0">有料パートで在籍店舗・セラピスト名をチェック！</p>'
        '</div>'
        f'{img_html}'
        '</div>'
        f'\n{PAID_PREVIEW_END}\n'
    )


def inject_paid_preview_html(original_html, image_url=None):
    """edit_text_1に有料パートプレビューを注入する。

    挿入位置: 回遊リスト・カレンダー誘導の後（末尾）。
    既存ブロックがあれば置換する。
    """
    preview_html = build_paid_preview_html(image_url=image_url)

    # 既存ブロックを除去
    if PAID_PREVIEW_START in original_html:
        original_html = re.sub(
            rf"{re.escape(PAID_PREVIEW_START)}.*?{re.escape(PAID_PREVIEW_END)}\s*",
            "",
            original_html,
            flags=re.DOTALL,
        )

    # 回遊リスト・カレンダー誘導の後（末尾）に追加
    return original_html.rstrip() + "\n" + preview_html


def inject_paid_disclaimer(text2):
    """edit_text_2（有料パート）の末尾に注記を注入する。

    既存ブロックがあれば置換する。
    """
    disclaimer_html = (
        f"\n{PAID_DISCLAIMER_START}\n"
        '<p style="margin-top:24px;font-size:13px;color:#888;">'
        "※本記事は個人の体験をもとにした内容です。"
        "同様の内容が必ず受けられるとは限りませんので、参考程度にご覧ください。"
        "</p>"
        f"\n{PAID_DISCLAIMER_END}\n"
    )

    # 既存ブロックを除去
    if PAID_DISCLAIMER_START in text2:
        text2 = re.sub(
            rf"{re.escape(PAID_DISCLAIMER_START)}.*?{re.escape(PAID_DISCLAIMER_END)}\s*",
            "",
            text2,
            flags=re.DOTALL,
        )

    return text2.rstrip() + "\n" + disclaimer_html


def inject_related_html(original_html, related_html):
    # 旧形式の直近ブロックが残っていれば全て削除
    if RELATED_NEXT_BLOCK_START in original_html:
        original_html = re.sub(
            rf"{re.escape(RELATED_NEXT_BLOCK_START)}.*?{re.escape(RELATED_NEXT_BLOCK_END)}\s*",
            "",
            original_html,
            flags=re.DOTALL,
        )
    # メインブロックをすべて除去してから新しいものを追加
    if RELATED_BLOCK_START in original_html:
        # 複数マーカーブロックが存在する場合もすべて除去
        cleaned = re.sub(
            rf"{re.escape(RELATED_BLOCK_START)}.*?{re.escape(RELATED_BLOCK_END)}\s*",
            "",
            original_html,
            flags=re.DOTALL,
        )
        if related_html:
            return cleaned.rstrip() + "\n" + related_html
        return cleaned
    if related_html:
        return original_html.rstrip() + "\n" + related_html
    return original_html


# ============================================================
# まとめ記事: 出勤カレンダーHTML生成
# ============================================================
def build_calendar_html(all_post_infos, summary_post_id=None, start_from_tomorrow=False):
    """指定まとめ記事の対象カテゴリの記事を日付別にまとめた出勤カレンダーHTMLを生成する。
    start_from_tomorrow=True の場合（16:30モード）、明日以降の日付のみ表示。
    """
    from datetime import datetime as _dt

    if summary_post_id is None:
        summary_post_id = list(SUMMARY_POSTS.keys())[0]
    sp_config = SUMMARY_POSTS[summary_post_id]
    target_categories = sp_config["categories"]
    area_label = sp_config["area_label"]

    # 対象カテゴリの記事を抽出
    target = [
        info for info in all_post_infos
        if info["post"].get("category") in target_categories
        and info["post"]["id"] not in SUMMARY_POST_IDS
    ]

    if not target:
        return ""

    # 日付→記事リストのマッピングを構築
    date_map = defaultdict(list)  # {"3/26": [info, ...], ...}
    for info in target:
        next_date = info.get("next_date")
        if not next_date:
            continue
        # "3/21,3/22,4/2" → ["3/21", "3/22", "4/2"]
        dates = [d.strip() for d in next_date.split(",") if "/" in d]
        for d in dates:
            date_map[d].append(info)

    if not date_map:
        # 日付なし記事のみの場合
        pass

    # 過去の日付を除外（モードに応じて今日以降 or 明日以降のみ表示）
    today_dt = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    if start_from_tomorrow:
        cutoff_dt = today_dt + timedelta(days=1)
    else:
        cutoff_dt = today_dt
    current_year = today_dt.year

    def _is_future_date(date_str):
        """カットオフ日以降の日付かどうかを判定"""
        try:
            parts = date_str.split("/")
            m, d = int(parts[0]), int(parts[1])
            dt = _dt(current_year, m, d)
            return dt >= cutoff_dt
        except (ValueError, IndexError):
            return False

    # カットオフより前の日付のみ持つ記事を特定（未定セクションに回す）
    past_only_infos = []
    for info in target:
        next_date = info.get("next_date")
        if not next_date:
            continue
        dates = [d.strip() for d in next_date.split(",") if "/" in d]
        if dates and not any(_is_future_date(d) for d in dates):
            past_only_infos.append(info)

    date_map = {d: infos for d, infos in date_map.items() if _is_future_date(d)}

    # 未定セクションに回す記事があるかチェック
    _no_date_candidates = [i for i in target if not i.get("next_date")]
    if not date_map and not past_only_infos and not _no_date_candidates:
        return ""

    # 日付をソート（月/日の数値順）
    def _date_sort_key(d):
        parts = d.split("/")
        return (int(parts[0]), int(parts[1]))

    sorted_dates = sorted(date_map.keys(), key=_date_sort_key)

    # 曜日取得用
    now = datetime.now(JST)
    year = now.year

    def _get_weekday(date_str):
        m, d = date_str.split("/")
        try:
            dt = _dt(year, int(m), int(d))
            return ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]
        except ValueError:
            return ""

    def _parse_title_short(title):
        """タイトルからメイン名前部分を抽出"""
        main = re.sub(r"【[^】]*】", "", title).strip()
        return main

    # カレンダーHTML構築
    inner = ""
    for date_str in sorted_dates:
        infos = date_map[date_str]
        weekday = _get_weekday(date_str)
        # 日付ヘッダー - 曜日で色分け
        if weekday == "日":
            header_bg = "linear-gradient(135deg, #ff6b6b, #ee5a24)"
        elif weekday == "土":
            header_bg = "linear-gradient(135deg, #74b9ff, #0984e3)"
        else:
            header_bg = "linear-gradient(135deg, #00b894, #00cec9)"
        inner += (
            f'<div style="margin-bottom:14px;border-radius:10px;overflow:hidden;'
            f'box-shadow:0 2px 8px rgba(0,0,0,0.15)">'
            f'<div style="background:{header_bg};padding:10px 14px">'
            f'<span style="font-size:15px;font-weight:bold;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,0.2)">'
            f'📅 {date_str}（{weekday}）'
            f'</span>'
            f'</div>'
        )
        # 記事カード - 2列テーブル
        sorted_infos = sorted(infos, key=lambda x: x["post"].get("sales_count") or 0, reverse=True)
        inner += '<table style="width:100%;border-collapse:collapse;border-spacing:0"><tbody>'
        for idx in range(0, len(sorted_infos), 2):
            inner += '<tr>'
            for col in range(2):
                if idx + col < len(sorted_infos):
                    info = sorted_infos[idx + col]
                    title = _strip_today_tag(info["new_title"] or info["post"]["title"])
                    url = info["post"]["url"]
                    category = info["post"].get("category", "")
                    schedule, area, cup, main = _parse_title_badges_calendar(title)
                    badge_html = ""
                    if area:
                        badge_html += (
                            f'<span style="display:inline-block;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;'
                            f'font-size:10px;padding:2px 8px;border-radius:10px;margin-right:4px;'
                            f'font-weight:bold;letter-spacing:0.5px">'
                            f'{area}</span>'
                        )
                    elif category:
                        badge_html += (
                            f'<span style="display:inline-block;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;'
                            f'font-size:10px;padding:2px 8px;border-radius:10px;margin-right:4px;'
                            f'font-weight:bold;letter-spacing:0.5px">'
                            f'{category}</span>'
                        )
                    if cup:
                        badge_html += (
                            f'<span style="display:inline-block;background:linear-gradient(135deg,#fd79a8,#e84393);color:#fff;'
                            f'font-size:10px;padding:2px 8px;border-radius:10px;margin-right:4px;'
                            f'font-weight:bold">'
                            f'{cup}</span>'
                        )
                    post_tags = info.get("tags", [])
                    if post_tags:
                        badge_html += (
                            f'<span style="display:inline-block;background:linear-gradient(135deg,#fdcb6e,#e17055);color:#fff;'
                            f'font-size:10px;padding:2px 8px;border-radius:10px;margin-right:4px;'
                            f'font-weight:bold">'
                            f'{" | ".join(post_tags)}</span>'
                        )
                    cell_content = ""
                    if badge_html:
                        cell_content += f'<div style="margin-bottom:4px">{badge_html}</div>'
                    cell_content += (
                        f'<a href="{url}" style="color:#74b9ff;text-decoration:none;'
                        f'font-size:12px;line-height:1.4;font-weight:500">{main}</a>'
                    )
                    # タイトル画像を表示（カード横幅いっぱい）
                    img_url = info.get("image_url")
                    if img_url:
                        cell_content += (
                            f'<div style="margin-top:6px">'
                            f'<a href="{url}">'
                            f'<img src="{img_url}" alt="{main}" '
                            f'style="width:100%;height:auto;border-radius:6px;'
                            f'object-fit:cover;display:block" />'
                            f'</a></div>'
                        )
                    inner += (
                        f'<td style="width:50%;vertical-align:top;padding:4px">'
                        f'<div style="background:rgba(255,255,255,0.05);border-radius:8px;'
                        f'padding:8px 10px;border:1px solid rgba(255,255,255,0.08)">'
                        f'{cell_content}</div></td>'
                    )
                else:
                    inner += '<td style="width:50%"></td>'
            inner += '</tr>'
        inner += '</tbody></table></div>\n'

    # 日付なし or カットオフ前の日付しかない記事（出勤日未定扱い）
    no_date = [
        info for info in target
        if not info.get("next_date")
    ] + past_only_infos
    if no_date:
        sorted_no_date = sorted(no_date, key=lambda x: x["post"].get("sales_count") or 0, reverse=True)
        inner += (
            f'<div style="margin-top:18px;margin-bottom:14px;border-radius:10px;overflow:hidden;'
            f'box-shadow:0 2px 8px rgba(0,0,0,0.15)">'
            f'<div style="background:linear-gradient(135deg,#636e72,#2d3436);padding:10px 14px">'
            f'<span style="font-size:14px;font-weight:bold;color:#dfe6e9">'
            f'📋 出勤日未定</span></div>'
            f'<table style="width:100%;border-collapse:collapse;border-spacing:0"><tbody>'
        )
        for idx in range(0, len(sorted_no_date), 2):
            inner += '<tr>'
            for col in range(2):
                if idx + col < len(sorted_no_date):
                    info = sorted_no_date[idx + col]
                    title = _strip_today_tag(info["new_title"] or info["post"]["title"])
                    url = info["post"]["url"]
                    main = _parse_title_short(title)
                    category = info["post"].get("category", "")
                    cell_content = ""
                    cell_content += (
                        f'<span style="display:inline-block;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;'
                        f'font-size:10px;padding:2px 8px;border-radius:10px;margin-right:4px;margin-bottom:4px;'
                        f'font-weight:bold">'
                        f'{category}</span>'
                        f'<a href="{url}" style="color:#74b9ff;text-decoration:none;'
                        f'font-size:12px;line-height:1.4;font-weight:500">{main}</a>'
                    )
                    # タイトル画像を表示（カード横幅いっぱい）
                    img_url = info.get("image_url")
                    if img_url:
                        cell_content += (
                            f'<div style="margin-top:6px">'
                            f'<a href="{url}">'
                            f'<img src="{img_url}" alt="{main}" '
                            f'style="width:100%;height:auto;border-radius:6px;'
                            f'object-fit:cover;display:block" />'
                            f'</a></div>'
                        )
                    inner += (
                        f'<td style="width:50%;vertical-align:top;padding:4px">'
                        f'<div style="background:rgba(255,255,255,0.05);border-radius:8px;'
                        f'padding:8px 10px;border:1px solid rgba(255,255,255,0.08)">'
                        f'{cell_content}'
                        f'</div></td>'
                    )
                else:
                    inner += '<td style="width:50%"></td>'
            inner += '</tr>'
        inner += '</tbody></table></div>\n'

    now_str = f"{now.month}月{now.day}日更新"
    html = (
        f'{CALENDAR_BLOCK_START}\n'
        f'<div style="background:linear-gradient(135deg,#6c5ce7,#a29bfe);padding:14px 16px;'
        f'border-radius:12px;margin-bottom:16px;text-align:center">'
        f'<p style="font-size:18px;font-weight:bold;color:#fff;margin:0;text-shadow:0 1px 3px rgba(0,0,0,0.2)">'
        f'🗓️ {area_label} 出勤カレンダー</p>'
        f'<p style="font-size:12px;color:rgba(255,255,255,0.8);margin:4px 0 0">'
        f'{now_str}</p>'
        f'</div>\n'
        f'{inner}'
        f'{CALENDAR_BLOCK_END}\n'
    )
    return html


def _parse_title_badges_calendar(title):
    """タイトルから【】バッジ部分とメイン見出しを分離する（カレンダー用）"""
    brackets = re.findall(r"【([^】]+)】", title)
    schedule = ""
    area = ""
    cup = ""
    for b in brackets:
        if re.search(r"[A-Z]カップ", b):
            cup = re.search(r"[A-Z]カップ", b).group()
        elif "出勤" in b:
            schedule = b
        else:
            area = b
    main = re.sub(r"【[^】]*】", "", title).strip()
    return schedule, area, cup, main


def inject_calendar_html(original_html, calendar_html):
    """まとめ記事のedit_text_1にカレンダーHTMLを注入する。"""
    # 既存カレンダーブロックを除去（新マーカー: 非表示div）
    if CALENDAR_BLOCK_START in original_html:
        original_html = re.sub(
            rf"{re.escape(CALENDAR_BLOCK_START)}.*?{re.escape(CALENDAR_BLOCK_END)}\s*",
            "",
            original_html,
            flags=re.DOTALL,
        )
    # 旧マーカー（HTMLコメント版）で囲まれたカレンダーを除去
    if _OLD_CALENDAR_BLOCK_START in original_html:
        original_html = re.sub(
            rf"{re.escape(_OLD_CALENDAR_BLOCK_START)}.*?{re.escape(_OLD_CALENDAR_BLOCK_END)}\s*",
            "",
            original_html,
            flags=re.DOTALL,
        )
    # 旧マーカーが部分的に消えた場合（endだけ残っている等）
    if _OLD_CALENDAR_BLOCK_END in original_html:
        original_html = re.sub(
            r'<div[^>]*background:\s*linear-gradient[^>]*>.*?出勤カレンダー.*?' + re.escape(_OLD_CALENDAR_BLOCK_END) + r'\s*',
            "",
            original_html,
            flags=re.DOTALL,
        )
    # 孤立した旧endマーカーも除去
    original_html = original_html.replace(_OLD_CALENDAR_BLOCK_END, "")
    original_html = original_html.replace(_OLD_CALENDAR_BLOCK_START, "")
    # 既存の回遊リストも除去（マーカーあり）
    if RELATED_BLOCK_START in original_html:
        original_html = re.sub(
            rf"{re.escape(RELATED_BLOCK_START)}.*?{re.escape(RELATED_BLOCK_END)}\s*",
            "",
            original_html,
            flags=re.DOTALL,
        )
    # マーカーなしの古い回遊リスト（様々な形式）も除去
    # パターン1: <hr>から始まる形式（「出勤予定の他の記事もチェック」「出勤中の他の記事もチェック」）
    original_html = re.sub(
        r'<hr\s*/?>?\s*.*?出勤[^\n]*の他の記事もチェック.*',
        "",
        original_html,
        flags=re.DOTALL,
    )
    # パターン2: <hr>なしで直接「出勤」テキストから始まる形式
    original_html = re.sub(
        r'<p[^>]*>\s*<strong>\s*📅[^<]*出勤[^<]*の他の記事もチェック.*',
        "",
        original_html,
        flags=re.DOTALL,
    )
    # パターン3: 「カレンダーを見る」リンクが残っている場合
    original_html = re.sub(
        r'<div[^>]*>\s*<a[^>]*>🗓️[^<]*出勤カレンダーを見る</a>\s*</div>\s*',
        "",
        original_html,
        flags=re.DOTALL,
    )
    # 旧形式の直近ブロックマーカーも除去
    if RELATED_NEXT_BLOCK_START in original_html:
        original_html = re.sub(
            rf"{re.escape(RELATED_NEXT_BLOCK_START)}.*?{re.escape(RELATED_NEXT_BLOCK_END)}\s*",
            "",
            original_html,
            flags=re.DOTALL,
        )
    return original_html.rstrip() + "\n" + calendar_html


# ============================================================
# 更新日の注入
# ============================================================
def inject_updated_date(html):
    """edit_text_1の冒頭に「〇月〇日更新」と値上げ告知を注入（既存があれば置換）"""
    now = datetime.now(JST)
    date_html = (
        f'{UPDATED_DATE_START}'
        f'<p><strong>{now.month}月{now.day}日更新</strong></p>'
        f'<p>※販売回数が{POINT_SALES_PER_STEP}回増えるごとに{POINT_STEP}pt値上げします</p>'
        f'<br/>{UPDATED_DATE_END}'
    )

    # マーカー無しの既存「〇月〇日更新」テキストを除去（重複防止）
    bare_pattern = r'<p>\s*<strong>\s*\d{1,2}月\d{1,2}日更新\s*</strong>\s*</p>\s*(?:<br\s*/?>)?\s*'
    html = re.sub(bare_pattern, '', html)
    # マーカー無しの既存値上げ告知テキストを除去（重複防止）
    bare_notice_pattern = r'<p>\s*※?\s*販売回数が\d+回増えるごとに\d+pt値上げします\s*</p>\s*(?:<br\s*/?>)?\s*'
    html = re.sub(bare_notice_pattern, '', html)

    # マーカー付きの既存テキストがあれば全除去してから先頭に追加
    if UPDATED_DATE_START in html:
        html = re.sub(
            rf"{re.escape(UPDATED_DATE_START)}.*?{re.escape(UPDATED_DATE_END)}\s*",
            "",
            html,
            flags=re.DOTALL,
        )
    return date_html + "\n" + html.lstrip()


# ============================================================
# 販売ポイント変更の検出
# ============================================================
def compute_point_change(post, details):
    """現在の販売ポイントと販売回数から算出した新しいポイントを返す。

    戻り値: (current_point, new_point, changed)
    - current_point: 現在フォームに入っている販売ポイント（int）。取得不能ならNone
    - new_point: 販売回数から計算した新しいポイント（int）。検出不能ならNone
    - changed: current_point != new_point の場合True
    """
    point_field = (details or {}).get("point_field")
    if not point_field:
        return None, None, False
    payload = (details or {}).get("payload", {}) or {}
    current_raw = payload.get(point_field, "")
    m = re.search(r"\d+", str(current_raw))
    current_point = int(m.group(0)) if m else None
    sales_count = (post or {}).get("sales_count") or 0
    new_point = calculate_sales_point(sales_count)
    changed = (current_point != new_point)
    return current_point, new_point, changed


# ============================================================
# 記事の更新
# ============================================================
def update_post(session, post, details, new_title, do_repost=False, all_post_infos=None, image_url=None):
    payload = dict(details["payload"])

    payload["edit_title"] = new_title

    # 販売ポイントを販売回数に応じて更新
    # （1000スタート、販売1回ごとに+100、上限2000）
    point_field = details.get("point_field")
    if point_field and point_field in payload:
        sales_count = post.get("sales_count") or 0
        new_point = calculate_sales_point(sales_count)
        old_point_raw = payload.get(point_field)
        if str(old_point_raw) != str(new_point):
            log.info(f"    💰 販売ポイント: {old_point_raw} → {new_point} (販売{sales_count}回)")
        payload[point_field] = str(new_point)

    if "edit_text_1" in payload:
        # decode_contents()がHTMLエンティティを返し、さらにWordPress側で
        # 多重エンコードされる場合がある（&lt; → &amp;lt; 等）。
        # 変化がなくなるまで繰り返しunescapeしてコメントマーカーを確実にデコードする。
        text = payload["edit_text_1"]
        for _round in range(5):
            decoded = html_module.unescape(text)
            if decoded == text:
                break
            text = decoded
        else:
            log.warning(f"    ⚠️  unescape 5回でも安定しません")
        payload["edit_text_1"] = text
        payload["edit_text_1"] = inject_updated_date(payload["edit_text_1"])
        # まとめ記事には回遊リストを入れない
        if post["id"] not in SUMMARY_POST_IDS:
            related_html = build_related_html(all_post_infos or [], post["id"], post.get("category"), title_only=TITLE_ONLY)
            payload["edit_text_1"] = inject_related_html(payload["edit_text_1"], related_html)
            all_others = [p for p in (all_post_infos or []) if p["post"]["id"] != post["id"]]
            # ログもカテゴリ回遊ルールに合わせてフィルタ
            cur_cat = post.get("category")
            LOCAL_ONLY = {"神奈川県", "埼玉県", "千葉県", "多摩"}
            if cur_cat in LOCAL_ONLY:
                all_others = [p for p in all_others if p["post"].get("category") == cur_cat]
            else:
                all_others = [p for p in all_others if p["post"].get("category") not in LOCAL_ONLY]
            if TITLE_ONLY:
                g1_label, g2_label = "明日", "明後日以降"
                g1_count = len([p for p in all_others if p.get("is_tomorrow")])
                g2_count = len([p for p in all_others if not p.get("is_tomorrow") and p["next_date"] is not None])
            else:
                g1_label, g2_label = "本日", "明日以降"
                g1_count = len([p for p in all_others if p.get("is_today")])
                g2_count = len([p for p in all_others if not p.get("is_today") and p["next_date"] is not None])
            if all_others:
                log.info(f"    📎 回遊リスト: {g1_label}{g1_count}件 / {g2_label}{g2_count}件")
            else:
                log.info(f"    📎 回遊リストなし")
            # 有料パートプレビューを回遊リスト・カレンダー誘導の後に注入
            payload["edit_text_1"] = inject_paid_preview_html(payload["edit_text_1"], image_url=image_url)

    # edit_text_2に残っている旧形式の回遊リストブロックを除去
    if "edit_text_2" in payload:
        text2 = payload["edit_text_2"]
        for _round in range(5):
            decoded = html_module.unescape(text2)
            if decoded == text2:
                break
            text2 = decoded
        if RELATED_BLOCK_START in text2:
            text2 = re.sub(
                rf"{re.escape(RELATED_BLOCK_START)}.*?{re.escape(RELATED_BLOCK_END)}\s*",
                "",
                text2,
                flags=re.DOTALL,
            )
        if RELATED_NEXT_BLOCK_START in text2:
            text2 = re.sub(
                rf"{re.escape(RELATED_NEXT_BLOCK_START)}.*?{re.escape(RELATED_NEXT_BLOCK_END)}\s*",
                "",
                text2,
                flags=re.DOTALL,
            )
        payload["edit_text_2"] = text2

    # 有料パート末尾に注記を注入
    if "edit_text_2" in payload and payload["edit_text_2"].strip():
        payload["edit_text_2"] = inject_paid_disclaimer(payload["edit_text_2"])

    # repostフィールドを明示的に制御（フォームHTMLから紛れ込み防止）
    payload.pop(REPOST_FIELD, None)
    if do_repost:
        payload[REPOST_FIELD] = "on"
        log.info(f"    🔄 再投稿チェックON")

    for attempt in range(3):
        try:
            res = session.post(EDIT_FORM_ACTION, files=_to_multipart(payload), timeout=60)
            if res.status_code == 200:
                action_str = "再投稿＋タイトル更新" if do_repost else "タイトル更新（編集のみ）"
                log.info(f"    ✅ {action_str}: {new_title}")
                return True
            log.error(f"    ❌ 更新失敗 (status: {res.status_code})")
            return False
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < 2:
                wait = [2, 5][attempt]
                log.warning(f"    ⚠️  通信エラー (試行{attempt+1}/3), {wait}秒後にリトライ: {e}")
                time.sleep(wait)
            else:
                log.error(f"    ❌ 通信エラー (3回失敗): {e}")
                return False


# ============================================================
# セット販売の再構築
# ============================================================
def fetch_set_list_full(session):
    """現在の全セットの [(set_id, title)] を返す（全ページ対応）"""
    result = []
    seen_ids = set()
    page = 1
    while True:
        url = f"{SETPRICE_LIST_URL}&cp={page}" if page > 1 else SETPRICE_LIST_URL
        try:
            res = session.get(url, timeout=30)
        except requests.RequestException as e:
            log.warning(f"    ⚠️ セット一覧取得エラー (page={page}): {e}")
            break
        if res.status_code != 200:
            log.warning(f"    ⚠️ セット一覧取得失敗 (page={page}, HTTP {res.status_code})")
            break
        soup = BeautifulSoup(res.text, "html.parser")
        page_items = []
        for tr in soup.find_all("tr"):
            i_del = tr.find("i", class_=re.compile(r"delete_set"))
            if not i_del:
                continue
            sid = i_del.get("data-id")
            a = tr.find("a", href=re.compile(r"setlist/\?set_id="))
            title = a.get_text(strip=True) if a else ""
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                page_items.append((sid, title))
        if not page_items:
            # このページは0件 = 最終ページの次
            break
        result.extend(page_items)
        # 次ページの存在確認
        next_page = page + 1
        next_link = soup.find("a", href=re.compile(rf"cp={next_page}\b"))
        if not next_link:
            for a in soup.find_all("a", href=re.compile(r"cp=(\d+)")):
                m_cp = re.search(r"cp=(\d+)", a["href"])
                if m_cp and int(m_cp.group(1)) > page:
                    next_link = a
                    break
        if not next_link:
            break
        page += 1
        time.sleep(0.5)
    log.info(f"    📋 セット取得数: {len(result)}件（{page}ページ）")
    return result


def fetch_set_list_ids(session):
    """現在の全セットIDを取得（後方互換用）"""
    return [sid for sid, _ in fetch_set_list_full(session)]


def delete_one_set(session, set_id):
    """指定IDのセットを削除する（3回リトライ）"""
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": SETPRICE_LIST_URL,
        "Origin": BASE_URL,
    }
    for attempt in range(3):
        try:
            res = session.post(EDIT_SET_URL, data={"delete_set_id": str(set_id)},
                               headers=headers, timeout=30)
            return res.status_code == 200
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < 2:
                time.sleep([2, 5][attempt])
            else:
                log.error(f"    ❌ セット削除通信エラー [id={set_id}]: {e}")
                return False


def delete_all_sets(session):
    """既存の全セットを削除する"""
    ids = fetch_set_list_ids(session)
    log.info(f"🗑️  既存セット削除: {len(ids)}件")
    ok = 0
    for sid in ids:
        if delete_one_set(session, sid):
            log.info(f"  ✅ 削除 [{sid}]")
            ok += 1
        else:
            log.warning(f"  ❌ 削除失敗 [{sid}]")
        time.sleep(SET_POST_INTERVAL)
    log.info(f"  📊 削除完了: {ok}/{len(ids)}件")


def create_one_set(session, name, price, post_ids):
    """1件のセットを作成する（3回リトライ）"""
    headers = {
        "Referer": f"{SETPRICE_LIST_URL}&newitem",
        "Origin": BASE_URL,
    }
    data = [("s_n_1", name), ("post_price", str(price))]
    for pid in post_ids:
        data.append(("inpost_ck[]", str(pid)))
    for pid in post_ids:
        data.append(("add_setpost[]", str(pid)))
    data.append(("post_price_aff", "0"))
    data.append(("add_setprice", "true"))
    for attempt in range(3):
        try:
            res = session.post(EDIT_SET_URL, data=data, headers=headers,
                               timeout=30, allow_redirects=False)
            return res.status_code in (200, 302)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < 2:
                time.sleep([2, 5][attempt])
            else:
                log.error(f"    ❌ セット作成通信エラー [{name}]: {e}")
                return False


def _organize_sets(post_infos):
    """post_infosを分類して (name, price, [post_ids]) のリストを返す"""
    now = datetime.now(JST)
    date_label = f"{now.month}/{now.day}"

    today_groups   = defaultdict(list)   # area -> [(pid, unit_price)]
    tagged_groups  = defaultdict(list)   # (area, tag) -> [(pid, unit_price)]

    # 除外理由の集計（診断用）
    excluded = defaultdict(list)  # reason -> [(pid, title, extra)]
    in_any = set()  # A or Bグループに入ったpid

    for info in post_infos:
        post = info["post"]
        pid = post["id"]
        title = post.get("title", "")[:40]
        cat = post.get("category")

        if pid in SUMMARY_POST_IDS:
            excluded["summary"].append((pid, title, cat))
            continue
        area = CATEGORY_TO_SET_AREA.get(cat)
        if not area:
            excluded["カテゴリ対象外"].append((pid, title, cat))
            continue
        sales_count = post.get("sales_count") or 0
        unit_price  = calculate_sales_point(sales_count)

        is_today = bool(info.get("is_today"))
        has_future = bool(info.get("next_date"))

        if not is_today and not has_future:
            excluded["出勤情報なし"].append((pid, title, cat))
            continue

        # A. 本日出勤×地域
        if is_today:
            today_groups[area].append((pid, unit_price))
            in_any.add(pid)

        # B. 地域×プレイタグ
        tags = info.get("tags") or []
        matched = next((t for t in SET_TAG_PRIORITY if t in tags), None)
        if matched:
            tagged_groups[(area, matched)].append((pid, unit_price))
            in_any.add(pid)
        else:
            if not is_today:
                excluded[f"タグなし(地域={area})"].append((pid, title, tags))

    # 除外理由サマリをログ
    if excluded:
        log.info(f"\n📋 セット組成: 除外理由サマリ")
        for reason, items in sorted(excluded.items()):
            log.info(f"  • {reason}: {len(items)}件")
            for pid, title, extra in items[:10]:
                log.info(f"      [{pid}] {title}  ({extra})")
            if len(items) > 10:
                log.info(f"      ... 他 {len(items)-10}件")

    def _build(base_name, items):
        total = sum(p for _, p in items)
        price = _calc_set_price(total, len(items))
        diff = total - price
        name = f"{base_name} {total}pt→{price}pt({diff}pt引)"
        return name, price, [pid for pid, _ in items]

    sets = []
    dropped_small = []
    # A. 本日出勤×地域（先に作成）
    for area in sorted(today_groups.keys()):
        items = today_groups[area]
        if len(items) < SET_MIN_POSTS:
            dropped_small.append((f"本日出勤{area}", [pid for pid, _ in items]))
            continue
        sets.append(_build(f"本日出勤{date_label}{area}セット", items))

    # B. 地域×プレイタグ
    for (area, tag) in sorted(tagged_groups.keys()):
        items = tagged_groups[(area, tag)]
        if len(items) < SET_MIN_POSTS:
            dropped_small.append((f"{area}{tag}", [pid for pid, _ in items]))
            continue
        sets.append(_build(f"{area}{tag}セット", items))

    if dropped_small:
        log.info(f"\n📋 セット組成: 2件未満で見送ったグループ")
        for label, pids in dropped_small:
            log.info(f"  • {label}: {len(pids)}件  記事ID={pids}")

    return sets


def fetch_profile_form(session):
    """プロフィール編集フォームの現在値を取得"""
    try:
        res = session.get(USERPROFILE_URL, timeout=30)
    except requests.RequestException as e:
        log.warning(f"    ⚠️ プロフィール取得エラー: {e}")
        return None
    if res.status_code != 200:
        log.warning(f"    ⚠️ プロフィール取得失敗 (HTTP {res.status_code})")
        return None
    soup = BeautifulSoup(res.text, "html.parser")
    # フォーム特定: action属性で判定、なければu_p_textを含むフォームを探す
    form = soup.find("form", action=lambda a: a and "edit_profile" in a)
    if form is None:
        text_area = soup.find("textarea", {"name": "u_p_text"})
        if text_area:
            form = text_area.find_parent("form")
    if form is None:
        log.warning(f"    ⚠️ プロフィールフォームが見つかりません")
        return None
    payload = {}
    for el in form.find_all(["input", "textarea", "select"]):
        name = el.get("name")
        if not name:
            continue
        if el.name == "textarea":
            payload[name] = el.get_text() or ""
        elif el.name == "select":
            sel_opt = el.find("option", selected=True)
            payload[name] = (sel_opt.get("value") if sel_opt else "") or ""
        else:
            itype = (el.get("type") or "text").lower()
            if itype in ("file", "submit", "button", "reset", "image"):
                continue
            if itype in ("checkbox", "radio") and not el.has_attr("checked"):
                continue
            payload[name] = el.get("value") or ""
    return payload


def update_profile_links(session, links):
    """プロフィールのフリーリンク5枠を更新する。

    links: [(text, url), ...] 最大5件。不足分は空でクリア。
    """
    payload = fetch_profile_form(session)
    if payload is None:
        return False
    for i, slot in enumerate(PROFILE_LINK_SLOTS):
        text = links[i][0] if i < len(links) else ""
        url  = links[i][1] if i < len(links) else ""
        payload[f"u_l_{slot}_1"] = text
        payload[f"u_l_{slot}_2"] = url
    # multipart/form-dataとして送信（値はutf-8のstrのままでOK）
    files = [(k, (None, v)) for k, v in payload.items()]
    for attempt in range(3):
        try:
            res = session.post(EDIT_PROFILE_URL, files=files, timeout=30,
                               allow_redirects=False)
            return res.status_code in (200, 302)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < 2:
                time.sleep([2, 5][attempt])
            else:
                log.error(f"    ❌ プロフィール更新通信エラー: {e}")
                return False


def _update_profile_with_today_sets(session):
    """本日出勤セットのURLをプロフィールのフリーリンクに設定"""
    now = datetime.now(JST)
    date_label = f"{now.month}/{now.day}"

    set_list = fetch_set_list_full(session)
    log.info(f"\n🔗 プロフィールフリーリンクを更新")
    log.info(f"  📋 現在のセット総数: {len(set_list)}件")

    links = []
    for area in PROFILE_LINK_AREAS:
        prefix = f"本日出勤{date_label}{area}セット"
        matched = next(((sid, title) for sid, title in set_list
                        if title.startswith(prefix)), None)
        if matched:
            sid, title = matched
            # "本日出勤7/20新宿セット 3900pt→2800pt(1100pt引)"
            #  → "本日出勤7/20新宿セット(1100pt引)"
            link_name = re.sub(r"\s*\d+pt→\d+pt\((\d+pt引)\)\s*$",
                               r"(\1)", title)
            url = SETLIST_URL_FMT.format(sid)
            links.append((link_name, url))
            log.info(f"  ✅ {area}: {link_name} → {url}")
        else:
            log.info(f"  ⏭️  {area}: 本日出勤セットなし（空欄化）")

    if update_profile_links(session, links):
        log.info(f"  ✅ プロフィール更新完了 ({len(links)}件のリンク設定)")
    else:
        log.warning(f"  ⚠️ プロフィール更新失敗")


# ============================================================
# codoc連携
# ============================================================
def _codoc_skip_post(post):
    """codoc投稿・同期の対象外判定"""
    if post["id"] in SUMMARY_POST_IDS:
        return True  # まとめ記事
    if post.get("is_reserved"):
        return True  # 予約投稿
    if not post.get("is_published", True):
        return True  # 非公開/下書き
    return False


def _codoc_login_any():
    """CODOC_COOKIEがあればCookie注入、なければ通常ログイン"""
    from wakust_codoc import codoc_login, codoc_login_via_cookie
    if CODOC_COOKIE:
        log.info("🍪 CODOC_COOKIE検出 → Cookie注入で認証")
        session = codoc_login_via_cookie(CODOC_COOKIE)
        if session:
            return session
        log.warning("⚠️ Cookie無効。通常ログインにフォールバック（2FA有効時は失敗）")
    return codoc_login(WAKUST_EMAIL, WAKUST_PASSWORD)


def run_codoc_post_new(session):
    """codocに未投稿記事のうち販売回数が最多のものを1件投稿する"""
    from wakust_codoc import codoc_create_entry
    log.info(f"\n{'='*55}")
    log.info(f"📝 codoc新規投稿 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")
    log.info(f"{'='*55}")

    all_posts = fetch_post_list(session)
    if not all_posts:
        log.error("❌ 記事一覧が空、codoc投稿中止")
        return

    state = load_state()
    # 対象記事: スキップ条件を除いた、かつcodoc未投稿
    candidates = []
    for p in all_posts:
        if _codoc_skip_post(p):
            continue
        if state.get(p["id"], {}).get("codoc_entry_id"):
            continue
        candidates.append(p)

    if not candidates:
        log.info("📝 codoc投稿候補なし（対象記事すべて投稿済み）")
        return

    # 販売回数の多い順にソート
    candidates.sort(key=lambda p: p.get("sales_count") or 0, reverse=True)
    target = candidates[0]
    log.info(f"📝 codoc投稿対象: [{target['id']}] {target['title']}  "
             f"(販売{target.get('sales_count') or 0}回, 残候補{len(candidates)}件)")

    # 記事詳細取得
    try:
        details = fetch_post_details(session, target)
    except Exception as e:
        log.error(f"❌ 記事詳細取得失敗: {e}")
        return
    payload = details.get("payload") or {}
    body_free = payload.get("edit_text_1", "") or ""
    body_paywalled = payload.get("edit_text_2", "") or ""
    point_field = details.get("point_field") or "post_price"
    try:
        price = int(payload.get(point_field) or 0)
    except (TypeError, ValueError):
        price = 0
    if price < 100:
        # codoc最低価格100円
        price = calculate_sales_point(target.get("sales_count") or 0)
    binded_url = target.get("url", "")

    # codocログイン (Cookie注入 or 通常ログイン)
    session_codoc = _codoc_login_any()
    if not session_codoc:
        log.error("❌ codocログイン失敗")
        return

    # 投稿
    entry_id = codoc_create_entry(
        session_codoc,
        title=target["title"],
        body_free=body_free,
        body_paywalled=body_paywalled,
        price=price,
        binded_url=binded_url,
    )
    if not entry_id:
        log.error(f"❌ codoc投稿失敗: [{target['id']}]")
        session_codoc.close()
        return

    log.info(f"✅ codoc投稿成功: [{target['id']}] → entry_id={entry_id}")

    # state更新
    if target["id"] not in state:
        state[target["id"]] = {}
    state[target["id"]]["codoc_entry_id"] = entry_id
    state[target["id"]]["codoc_title"] = target["title"]
    state[target["id"]]["codoc_price"] = price
    state[target["id"]]["codoc_posted_at"] = jst_strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    session_codoc.close()


def run_codoc_sync(session, posts, post_infos):
    """codoc投稿済み記事のタイトル/価格を最新に同期する（0時モード内で呼ばれる）"""
    from wakust_codoc import codoc_update_entry
    log.info(f"\n{'='*55}")
    log.info(f"🔄 codoc投稿記事の同期 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")
    log.info(f"{'='*55}")

    state = load_state()
    posts_by_id = {p["id"]: p for p in posts}
    infos_by_id = {info["post"]["id"]: info for info in post_infos}

    # codoc投稿済み記事のみ
    synced_ids = [pid for pid, s in state.items() if s.get("codoc_entry_id")]
    if not synced_ids:
        log.info("📝 codoc同期対象なし")
        return

    log.info(f"📝 codoc投稿済み: {len(synced_ids)}件")

    session_codoc = None
    updated = 0
    skipped = 0
    failed = 0
    for pid in synced_ids:
        s = state[pid]
        entry_id = s["codoc_entry_id"]
        p = posts_by_id.get(pid)
        if not p or _codoc_skip_post(p):
            continue
        info = infos_by_id.get(pid)
        if not info:
            continue
        # 記事更新後のタイトル・価格
        new_title = info.get("new_title") or p["title"]
        payload = info.get("details", {}).get("payload") or {}
        point_field = info.get("details", {}).get("point_field") or "post_price"
        try:
            new_price = int(payload.get(point_field) or 0)
        except (TypeError, ValueError):
            new_price = 0
        if new_price < 100:
            new_price = calculate_sales_point(p.get("sales_count") or 0)

        # 差分判定（タイトル or 価格に変化があった場合のみ更新）
        if (new_title == s.get("codoc_title")
                and new_price == s.get("codoc_price")):
            skipped += 1
            continue

        # 初回ログイン
        if session_codoc is None:
            session_codoc = _codoc_login_any()
            if not session_codoc:
                log.error("❌ codocログイン失敗、同期中止")
                return

        body_free = payload.get("edit_text_1", "") or ""
        body_paywalled = payload.get("edit_text_2", "") or ""
        binded_url = p.get("url", "")

        if codoc_update_entry(session_codoc, entry_id, new_title,
                              body_free, body_paywalled, new_price, binded_url):
            log.info(f"  ✅ 更新 [{pid}→{entry_id}] {new_title} / {new_price}円")
            state[pid]["codoc_title"] = new_title
            state[pid]["codoc_price"] = new_price
            state[pid]["codoc_updated_at"] = jst_strftime("%Y-%m-%d %H:%M:%S")
            save_state(state)
            updated += 1
        else:
            log.warning(f"  ⚠️ 更新失敗 [{pid}→{entry_id}]")
            failed += 1
        time.sleep(1)

    log.info(f"📊 codoc同期完了: 更新{updated}件 / 差分なし{skipped}件 / 失敗{failed}件")
    if session_codoc:
        session_codoc.close()


def _run_codoc_post_only():
    """朝昼夜モードで呼ばれる。wakustログイン→codoc投稿→終了"""
    log.info(f"\n{'='*55}")
    log.info(f"🚀 codoc投稿モード ({jst_strftime('%Y-%m-%d %H:%M:%S')})")
    log.info(f"{'='*55}")
    session = login_wakust()
    if not session:
        log.error("❌ wakustログイン失敗のため処理を中断します")
        sys.exit(1)
    try:
        run_codoc_post_new(session)
    finally:
        session.close()
    log.info(f"\n✅ codoc投稿処理完了 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")


# ============================================================
# 自社サイト販売（codocをペイウォールとして自社サイトに埋め込む）
# ============================================================
# 記事の「無料部分」は自社サイトの静的HTMLとして出力し、「有料部分」はcodocの
# エントリーに保存する。有料部分を静的HTMLに置くとソースを見るだけで読めてしまう
# ため、本文はサイト側には一切書き出さない。
# codocエントリーは binded_url を自社サイトの記事URLに向けて作成する。

SITE_CONTENT_DIR = "site_content/articles"


def _site_config():
    """site_config.json を読み込む（wakust_site と同じローダーを使う）"""
    from wakust_site import load_config
    return load_config()


def _site_article_url(cfg, post_id):
    return f"{cfg['base_url']}/articles/{post_id}.html"


def _shift_dates_iso(title):
    """タイトルの【8/20,21出勤】から実日付(YYYY-MM-DD)のリストを作る。

    年はタイトルに入っていないので、JSTの今日を基準に推定する
    （60日以上前の日付になる場合は翌年扱い）。
    """
    today = datetime.now(JST).date()
    out = []
    for md in _extract_dates_from_title(title):
        try:
            month, day = (int(x) for x in md.split("/", 1))
        except (TypeError, ValueError):
            continue
        for year in (today.year, today.year + 1):
            try:
                d = datetime(year, month, day).date()
            except ValueError:
                break  # 2/30 のような不正日付
            if (today - d).days <= 60:
                iso = d.isoformat()
                if iso not in out:
                    out.append(iso)
                break
    return sorted(out)


def _site_area_of(category):
    """カテゴリ名を一覧の絞り込み用エリア名に正規化する"""
    return CATEGORY_TO_SET_AREA.get(category, category or "その他")


def _site_price_for(post, details):
    """記事の販売価格を決める（ワクスト側の販売ポイントが基準）"""
    payload = details.get("payload") or {}
    point_field = details.get("point_field") or "post_price"
    try:
        price = int(payload.get(point_field) or 0)
    except (TypeError, ValueError):
        price = 0
    if price < 100:
        price = calculate_sales_point(post.get("sales_count") or 0)
    # codocの価格レンジ 100〜50,000円 にクランプ
    return max(100, min(price, 50000))


def _write_site_article(cfg, post, details, price, entry_id, entry_code,
                        tags=None, image_url=None, published_at=None):
    """site_content/articles/{id}.json を書き出す"""
    payload = details.get("payload") or {}
    os.makedirs(SITE_CONTENT_DIR, exist_ok=True)
    path = os.path.join(SITE_CONTENT_DIR, f"{post['id']}.json")
    prev = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                prev = json.load(f)
        except (OSError, ValueError):
            prev = {}
    now = jst_strftime("%Y-%m-%d %H:%M:%S")
    category = details.get("category") or prev.get("category") or "その他"
    article = {
        "id": post["id"],
        "title": post["title"],
        "category": category,
        # 一覧の絞り込みに使うフィールド群
        "area": _site_area_of(category),
        "tags": tags if tags is not None else prev.get("tags", []),
        "shift_dates": _shift_dates_iso(post["title"]),
        "sales_count": post.get("sales_count") or prev.get("sales_count") or 0,
        "pv_total": post.get("pv_total") or prev.get("pv_total") or 0,
        "price": price,
        # 無料部分だけをサイトに出す（有料部分 edit_text_2 は絶対に含めない）
        "free_html": payload.get("edit_text_1", "") or "",
        "image_url": image_url or prev.get("image_url") or "",
        "codoc_entry_id": entry_id,
        "codoc_entry_code": entry_code or "",
        "source_url": post.get("url", ""),
        "published_at": prev.get("published_at") or published_at or now,
        "content_updated_at": now,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(article, f, ensure_ascii=False, indent=2)
    return path


def _site_fetch_tags_image(session, post):
    try:
        # 自社サイトの絞り込み用なので日本語タグも含めて取得する
        return fetch_post_tags(session, post["url"], alpha_only=False)
    except Exception as e:
        log.warning(f"    ⚠️ タグ/画像取得スキップ: {e}")
        return [], None


def run_meta_export(session):
    """SNS投稿用に、記事の「メタデータだけ」を書き出す

    codocには一切触れず、有料部分(edit_text_2)も無料部分(edit_text_1)も
    保存しない。タイトル・エリア・タグ・出勤日・価格・販売回数と、
    ワクストの記事URLだけを持つ。Threads投稿はこれだけで組み立てられる。
    """
    log.info(f"\n{'='*55}")
    log.info(f"🗂️  記事メタデータの書き出し ({jst_strftime('%Y-%m-%d %H:%M:%S')})")
    log.info(f"{'='*55}")

    all_posts = fetch_post_list(session)
    if not all_posts:
        log.error("❌ 記事一覧が空、書き出し中止")
        return

    os.makedirs(SITE_CONTENT_DIR, exist_ok=True)
    written = skipped = 0
    seen_ids = set()
    for p in all_posts:
        if _codoc_skip_post(p):
            skipped += 1
            continue
        seen_ids.add(p["id"])
        path = os.path.join(SITE_CONTENT_DIR, f"{p['id']}.json")
        prev = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    prev = json.load(f)
            except (OSError, ValueError):
                prev = {}

        category = prev.get("category") or "その他"
        article = {
            "id": p["id"],
            "title": p["title"],
            "category": category,
            "area": _site_area_of(category),
            "tags": prev.get("tags", []),
            "shift_dates": _shift_dates_iso(p["title"]),
            "sales_count": p.get("sales_count") or 0,
            "pv_total": p.get("pv_total") or 0,
            "price": prev.get("price") or calculate_sales_point(p.get("sales_count") or 0),
            # 本文は保存しない（無料部分・有料部分ともに書き出さない）
            "free_html": "",
            "image_url": prev.get("image_url") or "",
            "codoc_entry_id": prev.get("codoc_entry_id", ""),
            "codoc_entry_code": prev.get("codoc_entry_code", ""),
            "source_url": p.get("url", ""),
            "published_at": prev.get("published_at") or p.get("posted_at")
                            or jst_strftime("%Y-%m-%d %H:%M:%S"),
            "content_updated_at": jst_strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(article, f, ensure_ascii=False, indent=2)
        written += 1

    # 非公開・予約になった記事のJSONは消す（一覧に出したままにしない）
    removed = 0
    for path in glob.glob(os.path.join(SITE_CONTENT_DIR, "*.json")):
        pid = os.path.splitext(os.path.basename(path))[0]
        if pid not in seen_ids:
            os.remove(path)
            removed += 1

    log.info(f"📊 メタデータ書き出し完了: {written}件 / 対象外{skipped}件 / 削除{removed}件")


def _run_meta_export_only():
    """メタデータ書き出しモード。codocには触れない"""
    log.info(f"\n{'='*55}")
    log.info(f"🚀 メタデータ書き出しモード ({jst_strftime('%Y-%m-%d %H:%M:%S')})")
    log.info(f"{'='*55}")
    session = login_wakust()
    if not session:
        log.error("❌ wakustログイン失敗のため処理を中断します")
        sys.exit(1)
    try:
        run_meta_export(session)
    finally:
        session.close()
    log.info(f"\n✅ 書き出し完了 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")


def run_site_publish(session, limit=1):
    """自社サイト未掲載の記事から販売回数が多い順に codoc エントリーを作成し、
    サイト用の記事JSONを書き出す"""
    from wakust_codoc import codoc_create_entry, codoc_fetch_entry_code
    log.info(f"\n{'='*55}")
    log.info(f"🏬 自社サイト掲載 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")
    log.info(f"{'='*55}")

    cfg = _site_config()
    if not cfg.get("base_url"):
        log.error("❌ site_config.json の base_url が未設定です")
        return

    all_posts = fetch_post_list(session)
    if not all_posts:
        log.error("❌ 記事一覧が空、サイト掲載中止")
        return

    state = load_state()
    candidates = []
    for p in all_posts:
        if _codoc_skip_post(p):
            continue
        if state.get(p["id"], {}).get("site_entry_id"):
            continue
        if os.path.exists(os.path.join(SITE_CONTENT_DIR, f"{p['id']}.json")):
            continue
        candidates.append(p)

    if not candidates:
        log.info("📝 サイト掲載候補なし（対象記事すべて掲載済み）")
        return

    candidates.sort(key=lambda p: p.get("sales_count") or 0, reverse=True)
    targets = candidates[:max(1, limit)]
    log.info(f"📝 掲載対象 {len(targets)}件 / 残候補 {len(candidates)}件")

    session_codoc = _codoc_login_any()
    if not session_codoc:
        log.error("❌ codocログイン失敗")
        return

    published = 0
    try:
        for target in targets:
            log.info(f"\n📝 [{target['id']}] {target['title']}  "
                     f"(販売{target.get('sales_count') or 0}回)")
            try:
                details = fetch_post_details(session, target)
            except Exception as e:
                log.error(f"❌ 記事詳細取得失敗 [{target['id']}]: {e}")
                continue
            payload = details.get("payload") or {}
            body_free = payload.get("edit_text_1", "") or ""
            body_paywalled = payload.get("edit_text_2", "") or ""
            if not body_paywalled.strip():
                log.warning(f"  ⏭️  有料部分が空のためスキップ [{target['id']}]")
                continue
            price = _site_price_for(target, details)
            site_url = _site_article_url(cfg, target["id"])

            entry_id = codoc_create_entry(
                session_codoc,
                title=target["title"],
                body_free=body_free,
                body_paywalled=body_paywalled,
                price=price,
                binded_url=site_url,      # ← 自社サイトの記事URLに紐付ける
                limited=CODOC_LIMITED,    # ← codoc上は限定公開（自社サイトで販売）
            )
            if not entry_id:
                log.error(f"❌ codocエントリー作成失敗 [{target['id']}]")
                continue

            entry_code = codoc_fetch_entry_code(session_codoc, entry_id)
            tags, image_url = _site_fetch_tags_image(session, target)
            path = _write_site_article(cfg, target, details, price,
                                       entry_id, entry_code,
                                       tags=tags, image_url=image_url)
            log.info(f"  ✅ 掲載 {site_url}  ({price}円 / entry={entry_id}"
                     f"{'/' + entry_code if entry_code else ' ⚠️コード未取得'})")
            log.info(f"  💾 {path}")

            st = state.setdefault(target["id"], {})
            st["site_entry_id"] = entry_id
            st["site_entry_code"] = entry_code or ""
            st["site_title"] = target["title"]
            st["site_price"] = price
            st["site_url"] = site_url
            st["site_published_at"] = jst_strftime("%Y-%m-%d %H:%M:%S")
            save_state(state)
            published += 1
            time.sleep(1)
    finally:
        session_codoc.close()

    log.info(f"\n📊 自社サイト掲載完了: {published}件")


def run_site_sync(session, posts, post_infos):
    """掲載済み記事のタイトル・価格・無料部分を最新化する（0時モードから呼ばれる）"""
    from wakust_codoc import codoc_update_entry, codoc_fetch_entry_code
    log.info(f"\n{'='*55}")
    log.info(f"🔄 自社サイト掲載記事の同期 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")
    log.info(f"{'='*55}")

    cfg = _site_config()
    state = load_state()
    posts_by_id = {p["id"]: p for p in posts}
    infos_by_id = {info["post"]["id"]: info for info in post_infos}

    target_ids = [pid for pid, s in state.items() if s.get("site_entry_id")]
    if not target_ids:
        log.info("📝 サイト同期対象なし")
        return

    log.info(f"📝 掲載済み: {len(target_ids)}件")
    session_codoc = None
    updated = skipped = failed = 0
    try:
        for pid in target_ids:
            s = state[pid]
            p = posts_by_id.get(pid)
            info = infos_by_id.get(pid)
            if not p or not info or _codoc_skip_post(p):
                continue
            details = info.get("details") or {}
            payload = details.get("payload") or {}
            new_title = info.get("new_title") or p["title"]
            new_price = _site_price_for(p, details)
            body_free = payload.get("edit_text_1", "") or ""
            body_paywalled = payload.get("edit_text_2", "") or ""

            # サイト側のJSONは無料部分が変わっていれば毎回書き直す
            path = os.path.join(SITE_CONTENT_DIR, f"{pid}.json")
            prev = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        prev = json.load(f)
                except (OSError, ValueError):
                    prev = {}
            content_changed = (prev.get("free_html") != body_free
                               or prev.get("title") != new_title
                               or prev.get("price") != new_price)
            codoc_changed = (new_title != s.get("site_title")
                             or new_price != s.get("site_price"))

            if not content_changed and not codoc_changed:
                skipped += 1
                continue

            if codoc_changed:
                if session_codoc is None:
                    session_codoc = _codoc_login_any()
                    if not session_codoc:
                        log.error("❌ codocログイン失敗、同期中止")
                        return
                ok = codoc_update_entry(
                    session_codoc, s["site_entry_id"], new_title,
                    body_free, body_paywalled, new_price,
                    binded_url=_site_article_url(cfg, pid),
                    limited=CODOC_LIMITED,
                )
                if not ok:
                    log.warning(f"  ⚠️ codoc更新失敗 [{pid}]")
                    failed += 1
                    continue
                s["site_title"] = new_title
                s["site_price"] = new_price
                s["site_updated_at"] = jst_strftime("%Y-%m-%d %H:%M:%S")

            # エントリーコードが未取得なら再取得を試みる
            if not s.get("site_entry_code"):
                if session_codoc is None:
                    session_codoc = _codoc_login_any()
                if session_codoc:
                    code = codoc_fetch_entry_code(session_codoc, s["site_entry_id"])
                    if code:
                        s["site_entry_code"] = code

            _write_site_article(cfg, dict(p, title=new_title), details,
                                new_price, s["site_entry_id"],
                                s.get("site_entry_code"))
            save_state(state)
            log.info(f"  ✅ 同期 [{pid}] {new_title} / {new_price}円")
            updated += 1
            time.sleep(1)
    finally:
        if session_codoc:
            session_codoc.close()

    log.info(f"📊 サイト同期完了: 更新{updated}件 / 差分なし{skipped}件 / 失敗{failed}件")


def _run_site_publish_only():
    """朝昼夜モード: wakustログイン→自社サイト掲載→サイト生成"""
    log.info(f"\n{'='*55}")
    log.info(f"🚀 自社サイト掲載モード ({jst_strftime('%Y-%m-%d %H:%M:%S')})")
    log.info(f"{'='*55}")
    session = login_wakust()
    if not session:
        log.error("❌ wakustログイン失敗のため処理を中断します")
        sys.exit(1)
    try:
        run_site_publish(session, limit=SITE_PUBLISH_LIMIT)
    finally:
        session.close()
    try:
        from wakust_site import build
        build()
    except Exception as e:
        log.error(f"❌ サイト生成でエラー: {e}", exc_info=True)
    log.info(f"\n✅ 自社サイト掲載処理完了 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")


def run_organize_sets(session, post_infos):
    """既存セット全削除→新規セット組成→プロフィールフリーリンク更新"""
    log.info(f"\n{'='*55}")
    log.info(f"📦 セット販売の再構築 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")
    log.info(f"{'='*55}")

    delete_all_sets(session)

    sets = _organize_sets(post_infos)
    log.info(f"\n📝 作成予定セット: {len(sets)}件")
    for name, price, pids in sets:
        log.info(f"  - {name} ({price}pt, {len(pids)}件)")

    if sets:
        log.info(f"\n🚀 セット作成開始")
        ok = 0
        for name, price, pids in sets:
            if create_one_set(session, name, price, pids):
                log.info(f"  ✅ 作成: {name}  {price}pt  記事{len(pids)}件")
                ok += 1
            else:
                log.warning(f"  ❌ 作成失敗: {name}")
            time.sleep(SET_POST_INTERVAL)
        log.info(f"\n📊 セット組成完了: {ok}/{len(sets)}件")

    # 本日出勤セットのURLをプロフィールに反映
    try:
        _update_profile_with_today_sets(session)
    except Exception as e:
        log.error(f"❌ プロフィールリンク更新エラー: {e}", exc_info=True)


# ============================================================
# カレンダーのみ更新モード
# ============================================================
def run_calendar_only():
    """全まとめ記事（出勤カレンダー）を更新する。"""
    log.info(f"\n{'='*55}")
    log.info(f"📅 カレンダーのみ更新 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")
    log.info(f"{'='*55}")

    session = login_wakust()
    if not session:
        log.error("❌ ログイン失敗のため処理を中断します（GitHub Actionsで失敗扱い）")
        sys.exit(1)

    posts = fetch_post_list(session)
    if not posts:
        log.warning("⚠️  記事が0件。再ログインしてリトライします")
        session.close()
        time.sleep(30)
        session = login_wakust()
        if session:
            posts = fetch_post_list(session)
    if not posts:
        log.error("❌ 記事が見つかりませんでした（再ログイン後も0件）")
        if session:
            session.close()
        sys.exit(1)

    # まとめ記事が存在するか確認
    summary_posts_found = {}  # {post_id: post}
    summary_details_map = {}  # {post_id: details}
    for post in posts:
        if post["id"] in SUMMARY_POST_IDS:
            summary_posts_found[post["id"]] = post

    missing = SUMMARY_POST_IDS - set(summary_posts_found.keys())
    if missing:
        log.warning(f"⚠️  まとめ記事が見つかりません: {missing}")

    if not summary_posts_found:
        session.close()
        return

    # 対象カテゴリの記事情報を収集
    post_infos = []
    for post in posts:
        if post.get("is_reserved"):
            continue
        try:
            details = fetch_post_details(session, post)
        except Exception as e:
            log.error(f"    ❌ [{post['id']}] 記事詳細取得失敗: {e}")
            continue
        post["category"] = details["category"]

        # まとめ記事自体は詳細だけ保存
        if post["id"] in SUMMARY_POST_IDS:
            summary_details_map[post["id"]] = details
            continue

        # 対象カテゴリ以外はスキップ
        if post.get("category") not in SUMMARY_ALL_CATEGORIES:
            log.info(f"    ⏭️  [{post['id']}] カテゴリ「{post.get('category')}」: 対象外")
            continue

        log.info(f"\n📄 [{post['id']}] {post['title']} ({post.get('category')})")

        tags, image_url = fetch_post_tags(session, post["url"])

        dates, is_tomorrow, is_today = (None, False, False)
        if details["schedule_url"]:
            log.info(f"    🔗 {details['schedule_url']}")
            dates_list, is_tomorrow, is_today, _saw_off = fetch_next_date_from_schedule(details["schedule_url"])
            if dates_list:
                dates = ",".join(dates_list)
                log.info(f"    📅 直近の出勤日: {dates}")

        new_title = post["title"]
        if dates:
            dates_list_raw = []
            for part in dates.split(","):
                dates_list_raw.append(part)
            new_title = build_new_title(post["title"], dates_list_raw)
        else:
            new_title = _strip_today_tag(new_title)

        # 本日出勤タグの付与/除去
        if is_today:
            new_title = new_title.rstrip() + TODAY_TAG
        else:
            new_title = _strip_today_tag(new_title)

        post_infos.append({
            "post":      post,
            "details":   details,
            "next_date": dates,
            "is_tomorrow":  is_tomorrow,
            "is_today":    is_today,
            "new_title": new_title,
            "tags":      tags,
            "image_url": image_url,
        })

    # 各まとめ記事ごとにカレンダーHTML生成＆注入
    for sp_id, sp_post in summary_posts_found.items():
        if sp_id not in summary_details_map:
            log.warning(f"⚠️  [{sp_id}] まとめ記事の詳細取得できず。スキップ")
            continue

        area_label = SUMMARY_POSTS[sp_id]["area_label"]
        calendar_html = build_calendar_html(post_infos, summary_post_id=sp_id)
        if not calendar_html:
            log.warning(f"⚠️  [{sp_id}] {area_label}: カレンダーに掲載する記事なし")
            continue

        log.info(f"\n📝 [{sp_id}] {area_label} まとめ記事: 出勤カレンダー更新")
        sp_details = summary_details_map[sp_id]
        payload = dict(sp_details["payload"])
        payload["edit_title"] = sp_post["title"]
        if "edit_text_1" in payload:
            text = payload["edit_text_1"]
            for _round in range(5):
                decoded = html_module.unescape(text)
                if decoded == text:
                    break
                text = decoded
            payload["edit_text_1"] = text
            payload["edit_text_1"] = inject_calendar_html(payload["edit_text_1"], calendar_html)
        # edit_text_2にも回遊リストが残っている場合は除去
        if "edit_text_2" in payload:
            text2 = payload["edit_text_2"]
            for _round in range(5):
                decoded = html_module.unescape(text2)
                if decoded == text2:
                    break
                text2 = decoded
            if RELATED_BLOCK_START in text2:
                text2 = re.sub(
                    rf"{re.escape(RELATED_BLOCK_START)}.*?{re.escape(RELATED_BLOCK_END)}\s*",
                    "",
                    text2,
                    flags=re.DOTALL,
                )
            if RELATED_NEXT_BLOCK_START in text2:
                text2 = re.sub(
                    rf"{re.escape(RELATED_NEXT_BLOCK_START)}.*?{re.escape(RELATED_NEXT_BLOCK_END)}\s*",
                    "",
                    text2,
                    flags=re.DOTALL,
                )
            payload["edit_text_2"] = text2
        payload.pop(REPOST_FIELD, None)

        for _attempt in range(3):
            try:
                res = session.post(EDIT_FORM_ACTION, files=_to_multipart(payload), timeout=60)
                if res.status_code == 200:
                    log.info(f"    ✅ {area_label} まとめ記事更新完了")
                else:
                    log.warning(f"    ⚠️  {area_label} まとめ記事更新失敗 (HTTP {res.status_code})")
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if _attempt < 2:
                    _wait = [2, 5][_attempt]
                    log.warning(f"    ⚠️  まとめ記事通信エラー (試行{_attempt+1}/3), {_wait}秒後にリトライ: {e}")
                    time.sleep(_wait)
                else:
                    log.error(f"    ❌ まとめ記事通信エラー (3回失敗): {e}")
        time.sleep(1)

    session.close()
    log.info(f"\n✅ カレンダー更新完了 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")


# ============================================================
# メイン処理
# ============================================================
# 情報収集ループの並列化ワーカー数（HTTP/Playwrightが並列で走る）
POST_INFO_WORKERS = 6


def _collect_single_post_info(session, post, state, start_from_tomorrow=False):
    """1記事の情報収集（details/tags/スケジュール取得とタイトル構築）を行う。

    スレッド内で呼ばれることを想定。ログは (level, message) のタプルリストとして
    返し、呼び出し側でまとめて出力することで順序を保つ。
    戻り値: (info_dict_or_None, log_messages)
    """
    msgs = []
    def _log(level, m):
        msgs.append((level, m))

    _log("info", f"\n📄 [{post['id']}] {post['title']}")
    if post.get("is_reserved"):
        _log("info", f"    ⏭️  予約投稿のためスキップ")
        return None, msgs

    try:
        details = fetch_post_details(session, post)
    except Exception as e:
        _log("error", f"    ❌ 記事詳細取得失敗: {e}")
        return None, msgs
    post["category"] = details["category"]

    tags, image_url = fetch_post_tags(session, post["url"])

    if not details["schedule_url"]:
        _log("warning", f"    ⚠️  スケジュールURLなし。タイトル/stateから日付復元を試行")
        fb_dates_str, fb_dates_list = _fallback_dates_from_title_or_state(post["title"], post["id"], state)
        if fb_dates_list:
            new_title = build_new_title(post["title"], fb_dates_list)
            if start_from_tomorrow:
                new_title = _strip_today_tag(new_title)
            new_title = new_title.rstrip() + " #" + ",".join(fb_dates_list)
        else:
            new_title = _strip_today_tag(post["title"])
        _area_tag = CATEGORY_AREA_TAG.get(post.get("category"))
        if _area_tag:
            new_title = new_title.rstrip() + _area_tag
        return {
            "post":      post,
            "details":   details,
            "next_date": fb_dates_str,
            "is_tomorrow":  False,
            "is_today":    False,
            "new_title": new_title,
            "tags":      tags,
            "image_url": image_url,
        }, msgs

    _log("info", f"    🔗 {details['schedule_url']}")

    dates, is_tomorrow, is_today, saw_off = fetch_next_date_from_schedule(
        details["schedule_url"], start_from_tomorrow=start_from_tomorrow
    )

    if not dates and not is_today:
        if saw_off:
            # スケジュール取得成功、全休みまたは未来出勤なし
            # → 未来の出勤日が急に休みになることは稀なので、既存のタイトルを維持
            #   (site側の一時的な不整合や表示週の違いの可能性)
            _log("info", f"    ✅ スケジュール全休み確認 → 既存タイトルを維持（変更なし）")
            new_title = post["title"]  # そのまま
            return {
                "post":      post,
                "details":   details,
                "next_date": None,
                "is_tomorrow":  False,
                "is_today":    False,
                "new_title": new_title,
                "tags":      tags,
                "image_url": image_url,
            }, msgs
        _log("warning", f"    ⚠️  出勤日取得失敗。タイトル/stateから日付復元を試行")
        fb_dates_str, fb_dates_list = _fallback_dates_from_title_or_state(post["title"], post["id"], state)
        if fb_dates_list:
            new_title = build_new_title(post["title"], fb_dates_list)
            if start_from_tomorrow:
                new_title = _strip_today_tag(new_title)
            new_title = new_title.rstrip() + " #" + ",".join(fb_dates_list)
        else:
            new_title = _strip_today_tag(post["title"])
        _area_tag = CATEGORY_AREA_TAG.get(post.get("category"))
        if _area_tag:
            new_title = new_title.rstrip() + _area_tag
        return {
            "post":      post,
            "details":   details,
            "next_date": fb_dates_str,
            "is_tomorrow":  False,
            "is_today":    False,
            "new_title": new_title,
            "tags":      tags,
            "image_url": image_url,
        }, msgs

    if dates:
        dates_str = ",".join(dates)
        _log("info", f"    📅 直近の出勤日: {dates_str}")
        new_title = build_new_title(post["title"], dates)
        if start_from_tomorrow:
            new_title = _strip_today_tag(new_title)
        new_title = new_title.rstrip() + " #" + ",".join(dates)
    elif is_today:
        today_now = datetime.now(JST)
        today_date_str = f"{today_now.month}/{today_now.day}"
        dates_str = today_date_str
        new_title = _strip_today_tag(post["title"])
        new_title = new_title.rstrip() + " #" + today_date_str
        _log("info", f"    📅 本日のみ出勤: {today_date_str}")
    else:
        dates_str = None
        new_title = _strip_today_tag(post["title"])

    if is_today:
        new_title = new_title.rstrip() + TODAY_TAG

    _area_tag = CATEGORY_AREA_TAG.get(post.get("category"))
    if _area_tag:
        new_title = new_title.rstrip() + _area_tag

    return {
        "post":      post,
        "details":   details,
        "next_date": dates_str,
        "is_tomorrow":  is_tomorrow,
        "is_today":    is_today,
        "new_title": new_title,
        "tags":      tags,
        "image_url": image_url,
    }, msgs


def _collect_post_infos_parallel(session, posts, state, start_from_tomorrow=False, max_workers=POST_INFO_WORKERS):
    """記事情報収集を並列化して実行。順序は posts と同じに保って返す。"""
    results = [None] * len(posts)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(_collect_single_post_info, session, post, state, start_from_tomorrow): i
            for i, post in enumerate(posts)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                info, msgs = future.result()
            except Exception as e:
                log.error(f"    ❌ 記事情報収集中に例外: {e}")
                continue
            # ログを記事単位でまとめて出力（他記事と行がぶつかるのは許容）
            for level, m in msgs:
                getattr(log, level)(m)
            if info is not None:
                results[idx] = info
    return [r for r in results if r is not None]


def run_update():
    log.info(f"\n{'='*55}")
    log.info(f"🔍 更新チェック開始 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")
    log.info(f"{'='*55}")

    session = login_wakust()
    if not session:
        log.error("❌ ログイン失敗のため処理を中断します（GitHub Actionsで失敗扱い）")
        sys.exit(1)

    all_posts = fetch_post_list(session)
    if not all_posts:
        log.warning("⚠️  記事が0件。再ログインしてリトライします")
        session.close()
        time.sleep(30)
        session = login_wakust()
        if session:
            all_posts = fetch_post_list(session)
    if not all_posts:
        log.error("❌ 記事が見つかりませんでした（再ログイン後も0件）")
        if session:
            session.close()
        sys.exit(1)

    unpublished = [p for p in all_posts if not p.get("is_published", True)]
    if unpublished:
        log.info(f"⏭️  非公開/下書き記事をスキップ: {len(unpublished)}件 ({', '.join(p['id'] for p in unpublished)})")
    posts = [p for p in all_posts if p.get("is_published", True)]

    # PV記録は記事情報収集後に実行（0時モードのみ）

    state = load_state()

    # 各記事の情報を並列収集（HTTP/Playwrightが並列で走る）
    post_infos = _collect_post_infos_parallel(session, posts, state, start_from_tomorrow=False)

    # PVを記録＋比較レポート生成（非公開含む全記事）
    log_pv(all_posts, post_infos=post_infos, state=state)
    generate_pv_report(all_posts)

    # 再投稿対象を決定
    # カテゴリーごとに上限まで: 明日出勤(販売数降順) → 明後日以降(販売数降順) で補充
    repost_ids = set()
    log.info(f"\n{'─'*55}")
    log.info(f"📊 再投稿対象選定")

    # カテゴリーごとに記事を分類
    posts_by_category = defaultdict(list)
    for info in post_infos:
        posts_by_category[info["post"]["category"]].append(info)

    for category, infos in posts_by_category.items():
        # 再投稿の基本条件: 上限未達 & 有料セクションURL由来 & まとめ記事でない
        eligible = [i for i in infos
                    if not i["details"].get("at_limit", False)
                    and not i["details"].get("schedule_from_free", False)
                    and i["next_date"] is not None
                    and i["post"]["id"] not in SUMMARY_POST_IDS]

        if not eligible:
            continue

        # カテゴリの空き枠を計算（全記事で同じカテゴリの最初の1件から取得）
        cat_current = infos[0]["details"].get("category_current", 0)
        cat_max     = infos[0]["details"].get("category_max", 4)
        slots = max(0, cat_max - cat_current)

        if slots == 0:
            log.info(f"  🏷️  カテゴリー「{category}」: 上限{cat_current}/{cat_max} → 空き枠なし")
            continue

        # 本日出勤 → 明日出勤 → それ以降 の優先順で選定
        primary = [i for i in eligible if i["is_today"]]
        primary.sort(key=lambda x: x["post"].get("sales_count") or 0, reverse=True)
        secondary = [i for i in eligible if not i["is_today"] and i["is_tomorrow"]]
        secondary.sort(key=lambda x: x["post"].get("sales_count") or 0, reverse=True)
        tertiary = [i for i in eligible if not i["is_today"] and not i["is_tomorrow"]]
        tertiary.sort(key=lambda x: x["post"].get("sales_count") or 0, reverse=True)
        primary_label, secondary_label, tertiary_label = "本日", "明日", "明後日以降"

        # 上限まで埋める
        selected = []
        for info in primary:
            if len(selected) >= slots:
                break
            selected.append(info)

        for info in secondary:
            if len(selected) >= slots:
                break
            selected.append(info)

        for info in tertiary:
            if len(selected) >= slots:
                break
            selected.append(info)

        for info in selected:
            repost_ids.add(info["post"]["id"])
            if info in primary:
                label = primary_label
            elif info in secondary:
                label = secondary_label
            else:
                label = tertiary_label
            sc = info["post"].get("sales_count") or 0
            log.info(f"    [{info['post']['id']}] 再投稿対象（{label}, 販売={sc}）")

        log.info(f"  🏷️  カテゴリー「{category}」: 空き{slots}枠 → {primary_label}{len(primary)}件+{secondary_label}{len(secondary)}件+{tertiary_label}{len(tertiary)}件 → 選定{len(selected)}件")

    # 全記事更新＋再投稿
    log.info(f"\n{'─'*55}")
    log.info("🚀 更新処理開始（全記事更新＋再投稿）")
    log.info(f"{'─'*55}")

    all_ids_str = ",".join(sorted(i["post"]["id"] for i in post_infos))

    for info in post_infos:
        post_id       = info["post"]["id"]
        new_title     = info["new_title"]
        do_repost     = post_id in repost_ids
        post_state    = state.get(post_id, {})
        title_changed = (new_title != info["post"]["title"])
        date_changed  = (post_state.get("dates") != info["next_date"])
        # 更新記事の顔ぶれが変わっていたら回遊リストも更新が必要
        related_changed = post_state.get("all_ids") != all_ids_str
        # 販売回数に応じて販売ポイントを値上げする必要があるか
        _cp, _np, price_changed = compute_point_change(info["post"], info["details"])

        # ── まとめ記事: タイトル更新・再投稿スキップ、カレンダーのみ注入 ──
        if post_id in SUMMARY_POST_IDS:
            area_label = SUMMARY_POSTS[post_id]["area_label"]
            calendar_html = build_calendar_html(post_infos, summary_post_id=post_id)
            if not calendar_html and not related_changed:
                log.info(f"\n    ℹ️  [{post_id}] {area_label} まとめ記事: 変化なし。スキップ")
                continue
            log.info(f"\n📝 [{post_id}] {area_label} まとめ記事: 出勤カレンダー更新")
            payload = dict(info["details"]["payload"])
            # タイトルはそのまま維持
            payload["edit_title"] = info["post"]["title"]
            if "edit_text_1" in payload:
                text = payload["edit_text_1"]
                for _round in range(5):
                    decoded = html_module.unescape(text)
                    if decoded == text:
                        break
                    text = decoded
                payload["edit_text_1"] = text
                payload["edit_text_1"] = inject_calendar_html(payload["edit_text_1"], calendar_html)
            # edit_text_2にも回遊リストが残っている場合は除去
            if "edit_text_2" in payload:
                text2 = payload["edit_text_2"]
                for _round in range(5):
                    decoded = html_module.unescape(text2)
                    if decoded == text2:
                        break
                    text2 = decoded
                if RELATED_BLOCK_START in text2:
                    text2 = re.sub(
                        rf"{re.escape(RELATED_BLOCK_START)}.*?{re.escape(RELATED_BLOCK_END)}\s*",
                        "",
                        text2,
                        flags=re.DOTALL,
                    )
                if RELATED_NEXT_BLOCK_START in text2:
                    text2 = re.sub(
                        rf"{re.escape(RELATED_NEXT_BLOCK_START)}.*?{re.escape(RELATED_NEXT_BLOCK_END)}\s*",
                        "",
                        text2,
                        flags=re.DOTALL,
                    )
                payload["edit_text_2"] = text2
            # 再投稿しない
            payload.pop(REPOST_FIELD, None)
            for _attempt in range(3):
                try:
                    res = session.post(EDIT_FORM_ACTION, files=_to_multipart(payload), timeout=60)
                    if res.status_code == 200:
                        log.info(f"    ✅ {area_label} まとめ記事更新完了")
                        state[post_id] = {
                            "dates":       None,
                            "title":       info["post"]["title"],
                            "reposted":   False,
                            "all_ids":    all_ids_str,
                            "updated_at": jst_strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        save_state(state)
                    else:
                        log.warning(f"    ⚠️  {area_label} まとめ記事更新失敗 (HTTP {res.status_code})")
                    break
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                    if _attempt < 2:
                        _wait = [2, 5][_attempt]
                        log.warning(f"    ⚠️  まとめ記事通信エラー (試行{_attempt+1}/3), {_wait}秒後にリトライ: {e}")
                        time.sleep(_wait)
                    else:
                        log.error(f"    ❌ まとめ記事通信エラー (3回失敗): {e}")
            time.sleep(1)
            continue

        midnight_needs_swap = False  # モード統合により不要

        # next_date=Noneの記事はタイトル更新・再投稿しない（回遊リスト・値段更新のみ）
        if info["next_date"] is None:
            do_repost = False
            if not related_changed and not midnight_needs_swap and not price_changed:
                log.info(f"\n    ℹ️  [{post_id}] 出勤日不明・変化なし。スキップ")
                continue

        if not title_changed and not date_changed and not do_repost and not related_changed and not midnight_needs_swap and not price_changed:
            log.info(f"\n    ℹ️  [{post_id}] 変化なし。スキップ")
            continue

        if price_changed:
            log.info(f"    💰 販売ポイント更新予定: {_cp} → {_np} (販売{info['post'].get('sales_count') or 0}回)")

        log.info(f"\n📝 [{post_id}] {info['post']['title']}")
        log.info(f"    → {new_title}")

        if update_post(session, info["post"], info["details"], new_title, do_repost, post_infos, image_url=info.get("image_url")):
            state[post_id] = {
                "dates":       info["next_date"],
                "title":      new_title,
                "reposted":   do_repost,
                "reposted_at": jst_strftime("%Y-%m-%d %H:%M:%S") if do_repost else state.get(post_id, {}).get("reposted_at", ""),
                "all_ids":    all_ids_str,
                "updated_at": jst_strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at_date": jst_strftime("%Y-%m-%d"),
            }
            save_state(state)

        time.sleep(1)

    # 記事更新完了後にセット販売を再構築
    try:
        run_organize_sets(session, post_infos)
    except Exception as e:
        log.error(f"❌ セット再構築でエラー: {e}", exc_info=True)

    # codoc投稿済み記事のタイトル/価格を同期
    try:
        run_codoc_sync(session, posts, post_infos)
    except Exception as e:
        log.error(f"❌ codoc同期でエラー: {e}", exc_info=True)

    # 自社サイト掲載済み記事のタイトル/価格/無料部分を同期 → サイト再生成
    try:
        run_site_sync(session, posts, post_infos)
    except Exception as e:
        log.error(f"❌ 自社サイト同期でエラー: {e}", exc_info=True)
    try:
        from wakust_site import build
        build()
    except Exception as e:
        log.error(f"❌ サイト生成でエラー: {e}", exc_info=True)

    session.close()
    log.info(f"\n✅ 全処理完了 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")


# ============================================================
# タイトル＋回遊リストのみ更新（16:30モード）
# ============================================================
def run_title_only():
    """タイトルの出勤日と回遊リストのみ更新する（再投稿・PVなし）。
    16:30 JST に実行し、明日以降の出勤日でタイトルを更新する（本日分は含めない）。
    """
    log.info(f"\n{'='*55}")
    log.info(f"🔍 タイトル＋回遊リスト更新 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")
    log.info(f"{'='*55}")

    session = login_wakust()
    if not session:
        log.error("❌ ログイン失敗のため処理を中断します（GitHub Actionsで失敗扱い）")
        sys.exit(1)

    all_posts = fetch_post_list(session)
    if not all_posts:
        log.warning("⚠️  記事が0件。再ログインしてリトライします")
        session.close()
        time.sleep(30)
        session = login_wakust()
        if session:
            all_posts = fetch_post_list(session)
    if not all_posts:
        log.error("❌ 記事が見つかりませんでした（再ログイン後も0件）")
        if session:
            session.close()
        sys.exit(1)

    unpublished = [p for p in all_posts if not p.get("is_published", True)]
    if unpublished:
        log.info(f"⏭️  非公開/下書き記事をスキップ: {len(unpublished)}件 ({', '.join(p['id'] for p in unpublished)})")
    posts = [p for p in all_posts if p.get("is_published", True)]

    state = load_state()

    # 各記事の情報を並列収集（start_from_tomorrow=Trueで本日分は含めない）
    post_infos = _collect_post_infos_parallel(session, posts, state, start_from_tomorrow=True)

    # カテゴリ枠が余っていたら再投稿も行う
    repost_ids = set()
    log.info(f"\n{'─'*55}")
    log.info(f"📊 再投稿対象選定（枠が余っている場合のみ）")

    posts_by_category = defaultdict(list)
    for info in post_infos:
        posts_by_category[info["post"]["category"]].append(info)

    for category, infos in posts_by_category.items():
        eligible = [i for i in infos
                    if not i["details"].get("at_limit", False)
                    and not i["details"].get("schedule_from_free", False)
                    and i["next_date"] is not None
                    and i["post"]["id"] not in SUMMARY_POST_IDS]

        if not eligible:
            continue

        cat_current = infos[0]["details"].get("category_current", 0)
        cat_max     = infos[0]["details"].get("category_max", 4)
        slots = max(0, cat_max - cat_current)

        if slots == 0:
            log.info(f"  🏷️  カテゴリー「{category}」: 上限{cat_current}/{cat_max} → 空き枠なし")
            continue

        # 本日出勤 → 明日出勤 → それ以降 の優先順で選定
        primary = [i for i in eligible if i["is_today"]]
        primary.sort(key=lambda x: x["post"].get("sales_count") or 0, reverse=True)
        secondary = [i for i in eligible if not i["is_today"] and i["is_tomorrow"]]
        secondary.sort(key=lambda x: x["post"].get("sales_count") or 0, reverse=True)
        tertiary = [i for i in eligible if not i["is_today"] and not i["is_tomorrow"]]
        tertiary.sort(key=lambda x: x["post"].get("sales_count") or 0, reverse=True)

        selected = []
        for info in primary:
            if len(selected) >= slots:
                break
            selected.append(info)
        for info in secondary:
            if len(selected) >= slots:
                break
            selected.append(info)
        for info in tertiary:
            if len(selected) >= slots:
                break
            selected.append(info)

        for info in selected:
            repost_ids.add(info["post"]["id"])
            if info in primary:
                label = "本日"
            elif info in secondary:
                label = "明日"
            else:
                label = "明後日以降"
            sc = info["post"].get("sales_count") or 0
            log.info(f"    [{info['post']['id']}] 再投稿対象（{label}, 販売={sc}）")

        log.info(f"  🏷️  カテゴリー「{category}」: 空き{slots}枠 → 選定{len(selected)}件")

    # タイトル＋回遊リスト更新（枠があれば再投稿も）
    log.info(f"\n{'─'*55}")
    log.info("🚀 タイトル＋回遊リスト更新処理開始")
    log.info(f"{'─'*55}")

    all_ids_str = ",".join(sorted(i["post"]["id"] for i in post_infos))

    for info in post_infos:
        post_id       = info["post"]["id"]
        new_title     = info["new_title"]
        do_repost     = post_id in repost_ids
        post_state    = state.get(post_id, {})
        title_changed = (new_title != info["post"]["title"])
        date_changed  = (post_state.get("dates") != info["next_date"])
        related_changed = post_state.get("all_ids") != all_ids_str
        # 販売回数に応じて販売ポイントを値上げする必要があるか
        _cp, _np, price_changed = compute_point_change(info["post"], info["details"])

        # まとめ記事: カレンダーのみ注入（16:30モードは明日以降）
        if post_id in SUMMARY_POST_IDS:
            area_label = SUMMARY_POSTS[post_id]["area_label"]
            calendar_html = build_calendar_html(post_infos, summary_post_id=post_id, start_from_tomorrow=True)
            if not calendar_html and not related_changed:
                log.info(f"\n    ℹ️  [{post_id}] {area_label} まとめ記事: 変化なし。スキップ")
                continue
            log.info(f"\n📝 [{post_id}] {area_label} まとめ記事: 出勤カレンダー更新")
            payload = dict(info["details"]["payload"])
            payload["edit_title"] = info["post"]["title"]
            if "edit_text_1" in payload:
                text = payload["edit_text_1"]
                for _round in range(5):
                    decoded = html_module.unescape(text)
                    if decoded == text:
                        break
                    text = decoded
                payload["edit_text_1"] = text
                payload["edit_text_1"] = inject_calendar_html(payload["edit_text_1"], calendar_html)
            if "edit_text_2" in payload:
                text2 = payload["edit_text_2"]
                for _round in range(5):
                    decoded = html_module.unescape(text2)
                    if decoded == text2:
                        break
                    text2 = decoded
                if RELATED_BLOCK_START in text2:
                    text2 = re.sub(
                        rf"{re.escape(RELATED_BLOCK_START)}.*?{re.escape(RELATED_BLOCK_END)}\s*",
                        "", text2, flags=re.DOTALL,
                    )
                if RELATED_NEXT_BLOCK_START in text2:
                    text2 = re.sub(
                        rf"{re.escape(RELATED_NEXT_BLOCK_START)}.*?{re.escape(RELATED_NEXT_BLOCK_END)}\s*",
                        "", text2, flags=re.DOTALL,
                    )
                payload["edit_text_2"] = text2
            payload.pop(REPOST_FIELD, None)
            for _attempt in range(3):
                try:
                    res = session.post(EDIT_FORM_ACTION, files=_to_multipart(payload), timeout=60)
                    if res.status_code == 200:
                        log.info(f"    ✅ {area_label} まとめ記事更新完了")
                        state[post_id] = {
                            "dates":       None,
                            "title":       info["post"]["title"],
                            "reposted":   False,
                            "all_ids":    all_ids_str,
                            "updated_at": jst_strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        save_state(state)
                    else:
                        log.warning(f"    ⚠️  {area_label} まとめ記事更新失敗 (HTTP {res.status_code})")
                    break
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                    if _attempt < 2:
                        _wait = [2, 5][_attempt]
                        log.warning(f"    ⚠️  まとめ記事通信エラー (試行{_attempt+1}/3), {_wait}秒後にリトライ: {e}")
                        time.sleep(_wait)
                    else:
                        log.error(f"    ❌ まとめ記事通信エラー (3回失敗): {e}")
            time.sleep(1)
            continue

        if info["next_date"] is None:
            do_repost = False
            if not related_changed and not price_changed:
                log.info(f"\n    ℹ️  [{post_id}] 出勤日不明・変化なし。スキップ")
                continue

        if not title_changed and not date_changed and not do_repost and not related_changed and not price_changed:
            log.info(f"\n    ℹ️  [{post_id}] 変化なし。スキップ")
            continue

        if price_changed:
            log.info(f"    💰 販売ポイント更新予定: {_cp} → {_np} (販売{info['post'].get('sales_count') or 0}回)")

        log.info(f"\n📝 [{post_id}] {info['post']['title']}")
        log.info(f"    → {new_title}")

        if update_post(session, info["post"], info["details"], new_title, do_repost, post_infos, image_url=info.get("image_url")):
            state[post_id] = {
                "dates":       info["next_date"],
                "title":      new_title,
                "reposted":   do_repost,
                "reposted_at": jst_strftime("%Y-%m-%d %H:%M:%S") if do_repost else state.get(post_id, {}).get("reposted_at", ""),
                "all_ids":    all_ids_str,
                "updated_at": jst_strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at_date": jst_strftime("%Y-%m-%d"),
            }
            save_state(state)

        time.sleep(1)

    session.close()
    log.info(f"\n✅ タイトル＋回遊リスト更新完了 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")


# ============================================================
# エントリーポイント
# ============================================================
if __name__ == "__main__":
    if CODOC_MODE == "meta_export":
        log.info(f"🚀 ワクスト自動更新スクリプト起動 [メタデータ書き出しモード]")
        _run_meta_export_only()
    elif CODOC_MODE == "site_publish":
        log.info(f"🚀 ワクスト自動更新スクリプト起動 [自社サイト掲載モード]")
        _run_site_publish_only()
    elif CODOC_MODE == "site_build":
        log.info(f"🚀 サイト生成のみ")
        from wakust_site import build
        build()
    elif CODOC_MODE == "post_new":
        log.info(f"🚀 ワクスト自動更新スクリプト起動 [codoc投稿モード]")
        _run_codoc_post_only()
    elif CALENDAR_ONLY:
        log.info(f"🚀 ワクスト自動更新スクリプト起動 [カレンダーのみモード]")
        run_calendar_only()
    elif TITLE_ONLY:
        log.info(f"🚀 ワクスト自動更新スクリプト起動 [16:30タイトル更新モード]")
        run_title_only()
    else:
        log.info(f"🚀 ワクスト自動更新スクリプト起動 [0時統合モード]")
        run_update()
