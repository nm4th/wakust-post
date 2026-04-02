"""
ワクスト 記事タイトル自動更新 ＋ 翌日出勤記事再投稿スクリプト
====================================================================
毎日17:00 JSTと0:00 JSTに実行し、以下を行います。

■ 17:00モード（通常）:
  1. 記事一覧から全記事のURLとタイトルを取得
  2. 各記事の編集画面(edit_text_2)からスケジュールURLを取得
  3. スケジュールページから翌日以降で最も近い出勤日を最大3件取得
  4. タイトルの【日付出勤】部分を更新
     - 同月: 【3/13,14,15出勤】  月またぎ: 【3/13,14|4/4出勤】
  5. 無料部分に「〇月〇日更新」を挿入
  6. 無料部分の回遊リスト: 明日出勤(グループ1)・明後日以降出勤(グループ2)
  7. 翌日出勤の記事を再投稿（カテゴリ上限4/4・無料部分URLの記事は除外）
  8. PVデータをCSVに記録

■ 0:00モード（MIDNIGHT_RUN=1）:
  - 17時に作成済みの回遊リストのラベルを文字置換:
    明日出勤予定→本日出勤中、明後日以降出勤予定→明日以降出勤予定
  - 神奈川県・埼玉県カテゴリーの記事を再投稿（17時モードではスキップ）
  - 「〇月〇日更新」の書き換えもしない

使い方:
  pip install requests beautifulsoup4
  python wakust_auto_update.py                # 17:00モード
  MIDNIGHT_RUN=1 python wakust_auto_update.py # 0:00モード
"""

import requests
from bs4 import BeautifulSoup
import time
import re
import json
import os
import sys
import csv
import html as html_module
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from urllib.parse import urlparse, parse_qs, unquote
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

# メール通知設定（GitHub Secretsで管理）
REPORT_EMAIL    = os.environ.get("REPORT_EMAIL", "")       # 送信先
SMTP_USER       = os.environ.get("SMTP_USER", "")          # Gmail アドレス
SMTP_PASSWORD   = os.environ.get("SMTP_PASSWORD", "")      # Gmail アプリパスワード

# タイムゾーン（GitHub ActionsはUTCで動くため、JST明示が必須）
JST = timezone(timedelta(hours=9))

def jst_strftime(fmt):
    """time.strftimeのJST版"""
    return datetime.now(JST).strftime(fmt)

# MIDNIGHT_RUN: 実際のJST時刻で自動判定（19:00-05:59 → 0時モード）
# 環境変数での明示指定も可能（"1"=強制0時モード, "0"=強制通常モード）
_midnight_env = os.environ.get("MIDNIGHT_RUN", "")
if _midnight_env in ("0", "1"):
    MIDNIGHT_RUN = _midnight_env == "1"
else:
    _jst_hour = datetime.now(JST).hour
    MIDNIGHT_RUN = _jst_hour >= 19 or _jst_hour < 6

# CALENDAR_ONLY: まとめ記事（出勤カレンダー）のみ更新
CALENDAR_ONLY = os.environ.get("CALENDAR_ONLY", "0") == "1"


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

# まとめ記事（出勤カレンダー）: タイトル更新・再投稿をスキップ
# {post_id: {"categories": set, "area_label": str}}
SUMMARY_POSTS = {
    "1657099": {"categories": {"東京都", "池袋", "新宿"}, "area_label": "東京エリア"},
    "1657101": {"categories": {"多摩"},                   "area_label": "多摩エリア"},
    "1657104": {"categories": {"神奈川県"},               "area_label": "神奈川エリア"},
    "1657105": {"categories": {"埼玉県"},                 "area_label": "埼玉エリア"},
}
SUMMARY_POST_IDS = set(SUMMARY_POSTS.keys())
# 0時モードで再投稿するカテゴリー（17時モードでは再投稿しない）
MIDNIGHT_REPOST_CATEGORIES = {"神奈川県", "埼玉県"}
# 全まとめ記事の対象カテゴリ（情報収集用）
SUMMARY_ALL_CATEGORIES = set()
# カテゴリ→カレンダー記事URL のマッピング
CATEGORY_CALENDAR_URL = {}
for _sp_id, _sp in SUMMARY_POSTS.items():
    SUMMARY_ALL_CATEGORIES |= _sp["categories"]
    _cal_url = f"https://wakust.com/Risingnoboru/{_sp_id}/"
    for _cat in _sp["categories"]:
        CATEGORY_CALENDAR_URL[_cat] = {"url": _cal_url, "label": _sp["area_label"]}



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


# ============================================================
# ログイン
# ============================================================
def login_wakust():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    res = session.post(LOGIN_AJAX_URL, data={
        "login_email":    WAKUST_EMAIL,
        "login_password": WAKUST_PASSWORD,
    })

    if res.status_code == 200 and "loginok" in res.text:
        log.info("✅ ログイン成功")
        return session

    log.error(f"❌ ログイン失敗: {res.text[:100]}")
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
        })
    return posts


def fetch_post_list(session):
    """全ページの記事一覧を取得"""
    all_posts = []
    page = 1
    while True:
        url = f"{POST_LIST_URL}&cp={page}" if page > 1 else POST_LIST_URL
        res = session.get(url)
        soup = BeautifulSoup(res.text, "html.parser")
        posts = _parse_post_list_page(soup)
        if not posts:
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
            last_lines = list(reversed(text.splitlines()))[:5]
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
    }


# ============================================================
# 記事公開ページからタグを取得
# ============================================================
def fetch_post_tags(session, post_url):
    """記事の公開ページからアルファベットのみのタグとタイトル画像URLを抽出する。

    タグは「KEYWORD(NUMBER)」形式で表示されている。
    例: CKB(127), F(1473), HR(23397), 中野(989), 巨乳(19987)
    → アルファベットのみ: ["CKB", "F", "HR"]

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
        for el in soup.find_all(["a", "span"]):
            text = el.get_text(strip=True)
            m = re.match(r'^([A-Za-z]+)\(\d+\)$', text)
            if m:
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
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                java_script_enabled=True,
            )
            page = context.new_page()
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
                page.wait_for_selector(".sch-date, .sch-work, .weekSchedule, table", timeout=5000)
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
}

def fetch_next_date_from_schedule(schedule_url):
    try:
        _used_playwright = False
        _parsed_host = urlparse(schedule_url).hostname or ""
        _force_playwright = any(d in _parsed_host for d in PLAYWRIGHT_PREFER_DOMAINS)

        if _force_playwright:
            log.info(f"    🔧 Playwright優先ドメイン → Playwrightで取得")
            soup = _fetch_with_playwright(schedule_url)
            _used_playwright = True
            if soup is None:
                return [], False, False
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
                    return [], False, False
            elif res.status_code != 200:
                log.error(f"    ❌ スケジュール取得失敗 (HTTP {res.status_code}): {schedule_url}")
                return [], False, False
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
            return [], False, False

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
    # 17時モード: 翌日以降の出勤日のみ / 0時モード: 当日以降の出勤日
    start_date   = today if MIDNIGHT_RUN else today + timedelta(days=1)
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
                if not re.search(r"\d{2}:\d{2}", info) and "満枠" not in info:
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
                    if not re.search(r"\d{2}:\d{2}", info) and "満枠" not in info:
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
                            if not re.search(r"\d{2}:\d{2}", info) and "満枠" not in info:
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
                            if not re.search(r"\d{2}:\d{2}", info) and "満枠" not in info:
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

    # パターン2: 「3/7 土 10:00〜」形式のテーブル（namexspa・bellee等）
    # ※各行が「日付 | 時刻 | 予約リンク」の縦型テーブル
    if not candidates:
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                date_text = cells[0].get_text(strip=True)
                info_text = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                m = re.search(r"(\d{1,2})/(\d{1,2})", date_text)
                if not m:
                    continue
                month, day = int(m.group(1)), int(m.group(2))
                d = datetime(current_year, month, day)
                if d < start_date:
                    continue
                if "お休み" in info_text or "未定" in info_text:
                    continue
                if re.search(r"\d{2}:\d{2}", info_text) or "満枠" in info_text:
                    candidates.append((d, f"{month}/{day}"))
            if candidates:
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
                    continue
                if not re.search(r"\d{2}:\d{2}", info) and "満枠" not in info:
                    continue
                month, day = int(m.group(1)), int(m.group(2))
                d = datetime(current_year, month, day)
                if d >= start_date:
                    candidates.append((d, f"{month}/{day}"))
            if candidates:
                log.info(f"    📅 形式K(krc_cast_calendar)でマッチ")

    # パターンM: men-este形式（tokyo-fairy-land等）
    # div.sch-date 内の dt に日付、div.sch-work 内の dd に出勤情報
    # 複数週(複数sch-date/sch-work)がある場合は全ペアを処理
    if not candidates:
        all_sch_dates = soup.find_all("div", class_=re.compile(r"sch-date"))
        all_sch_works = soup.find_all("div", class_=re.compile(r"sch-work"))
        if all_sch_dates and all_sch_works:
            log.info(f"    🔧 形式M: sch-date={len(all_sch_dates)}個, sch-work={len(all_sch_works)}個")
            for sch_date, sch_work in zip(all_sch_dates, all_sch_works):
                dts = sch_date.find_all("dt")
                dds = sch_work.find_all("dd")
                for dt_el, dd_el in zip(dts, dds):
                    info = dd_el.get_text(strip=True)
                    if "休み" in info or "未定" in info:
                        continue
                    if not re.search(r"\d{2}:\d{2}", info) and "満枠" not in info:
                        continue
                    m = re.search(r"(\d{1,2})/(\d{1,2})", dt_el.get_text())
                    if not m:
                        continue
                    month, day = int(m.group(1)), int(m.group(2))
                    d = datetime(current_year, month, day)
                    if d >= start_date:
                        candidates.append((d, f"{month}/{day}"))
            if candidates:
                log.info(f"    📅 形式M(sch-date/sch-work)でマッチ")

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
                    continue
                if not re.search(r"\d{2}:\d{2}", info) and "満枠" not in info:
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
                    if not re.search(r"\d{2}:\d{2}", info) and "満枠" not in info:
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

    # 全パーサー失敗 → Playwright未使用ならフォールバック再取得して再解析
    if not candidates and not _used_playwright:
        log.info(f"    🔧 全パーサー失敗 → Playwrightで再取得を試行")
        pw_soup = _fetch_with_playwright(schedule_url)
        if pw_soup:
            soup = pw_soup
            _used_playwright = True
            # 再帰ではなく主要パターンだけ再チェック
            # Format M (men-este) — 複数週対応
            all_sch_dates = soup.find_all("div", class_=re.compile(r"sch-date"))
            all_sch_works = soup.find_all("div", class_=re.compile(r"sch-work"))
            if all_sch_dates and all_sch_works:
                log.info(f"    🔧 PW形式M: sch-date={len(all_sch_dates)}個, sch-work={len(all_sch_works)}個")
                for sch_date, sch_work in zip(all_sch_dates, all_sch_works):
                    dts = sch_date.find_all("dt")
                    dds = sch_work.find_all("dd")
                    for dt_el, dd_el in zip(dts, dds):
                        info = dd_el.get_text(strip=True)
                        if "休み" in info or "未定" in info:
                            continue
                        if not re.search(r"\d{2}:\d{2}", info) and "満枠" not in info:
                            continue
                        m = re.search(r"(\d{1,2})/(\d{1,2})", dt_el.get_text())
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
                            continue
                        if not re.search(r"\d{2}:\d{2}", info) and "満枠" not in info:
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
                            if not re.search(r"\d{2}:\d{2}", info) and "満枠" not in info:
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
                                if not re.search(r"\d{2}:\d{2}", info) and "満枠" not in info:
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
        return [], False, False

    candidates.sort(key=lambda x: x[0])
    # 重複除去しつつ直近3件まで取得
    seen = set()
    unique = []
    for dt, s in candidates:
        if s not in seen:
            seen.add(s)
            unique.append((dt, s))
        if len(unique) >= 3:
            break

    dates = [s for _, s in unique]
    tomorrow = today + timedelta(days=1)
    is_tomorrow = (unique[0][0].date() == tomorrow.date())
    is_today = (unique[0][0].date() == today.date())
    return dates, is_tomorrow, is_today


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

def _strip_today_tag(title):
    """タイトルから #本日出勤 タグを除去する（回遊リスト・カレンダー表示用）"""
    return title.replace(TODAY_TAG, "").rstrip()


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
def build_related_html(all_post_infos, current_post_id, current_category=None):
    """出勤グループ別の回遊リストを生成（更新した全記事対象）

    17:00モード: グループ1=明日出勤、グループ2=明後日以降出勤
    0:00モード:  グループ1=今日出勤、グループ2=明日以降出勤

    カテゴリ回遊ルール:
      - 神奈川県: 神奈川県内のみで回遊
      - 埼玉県: 埼玉県内のみで回遊
      - 多摩: 多摩内のみで回遊
      - 東京都/池袋/新宿: 互いに回遊OK
    """
    others = [p for p in all_post_infos if p["post"]["id"] != current_post_id]

    # カテゴリ別回遊フィルタリング
    # 神奈川県/埼玉県: 同県同士のみ / それ以外: 神奈川県・埼玉県以外すべてで回遊
    LOCAL_ONLY_CATEGORIES = {"神奈川県", "埼玉県", "多摩"}
    if current_category:
        if current_category in LOCAL_ONLY_CATEGORIES:
            others = [p for p in others if p["post"].get("category") == current_category]
        else:
            others = [p for p in others if p["post"].get("category") not in LOCAL_ONLY_CATEGORIES]

    from datetime import datetime
    today_dt = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)

    if MIDNIGHT_RUN:
        # 0時モード: グループ1=今日出勤(is_today)、グループ2=明日以降
        group1 = [p for p in others if p.get("is_today")]
        tomorrow_dt = today_dt + timedelta(days=1)

        def is_tomorrow_or_later(info):
            if info.get("is_today") or info["next_date"] is None:
                return False
            try:
                first_date = info["next_date"].split(",")[0]
                m, d = first_date.split("/")
                dt = datetime(today_dt.year, int(m), int(d))
                return dt >= tomorrow_dt
            except Exception:
                return False

        group2 = [p for p in others if is_tomorrow_or_later(p)]
        label1 = "📅 本日出勤中の他の記事もチェック！"
        label2 = "📆 明日以降出勤予定の他の記事もチェック！"
    else:
        # 17時モード: グループ1=明日出勤(is_tomorrow)、グループ2=明後日以降
        group1 = [p for p in others if p["is_tomorrow"]]
        day_after_tomorrow = today_dt + timedelta(days=2)

        def is_after_tomorrow(info):
            if info["is_tomorrow"] or info["next_date"] is None:
                return False
            try:
                first_date = info["next_date"].split(",")[0]
                m, d = first_date.split("/")
                dt = datetime(today_dt.year, int(m), int(d))
                return dt >= day_after_tomorrow
            except Exception:
                return False

        group2 = [p for p in others if is_after_tomorrow(p)]
        label1 = "📅 明日出勤予定の他の記事もチェック！"
        label2 = "📆 明後日以降出勤予定の他の記事もチェック！"

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
def build_calendar_html(all_post_infos, summary_post_id=None):
    """指定まとめ記事の対象カテゴリの記事を日付別にまとめた出勤カレンダーHTMLを生成する。"""
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

    # 過去の日付を除外（今日以降のみ表示）
    today_dt = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    current_year = today_dt.year

    def _is_future_date(date_str):
        """今日以降の日付かどうかを判定"""
        try:
            parts = date_str.split("/")
            m, d = int(parts[0]), int(parts[1])
            dt = _dt(current_year, m, d)
            return dt >= today_dt
        except (ValueError, IndexError):
            return False

    date_map = {d: infos for d, infos in date_map.items() if _is_future_date(d)}

    if not date_map:
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

    # 日付なしの記事（出勤日不明）
    no_date = [
        info for info in target
        if not info.get("next_date")
    ]
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
    """edit_text_1の冒頭に「〇月〇日更新」を注入（既存があれば置換）"""
    now = datetime.now(JST)
    date_html = f'{UPDATED_DATE_START}<p><strong>{now.month}月{now.day}日更新</strong></p><br/>{UPDATED_DATE_END}'

    # マーカー無しの既存「〇月〇日更新」テキストを除去（重複防止）
    bare_pattern = r'<p>\s*<strong>\s*\d{1,2}月\d{1,2}日更新\s*</strong>\s*</p>\s*(?:<br\s*/?>)?\s*'
    html = re.sub(bare_pattern, '', html)

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
# 記事の更新
# ============================================================
def update_post(session, post, details, new_title, do_repost=False, all_post_infos=None, image_url=None):
    payload = dict(details["payload"])

    payload["edit_title"] = new_title

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
        if not MIDNIGHT_RUN:
            payload["edit_text_1"] = inject_updated_date(payload["edit_text_1"])
        # まとめ記事には回遊リストを入れない
        if post["id"] not in SUMMARY_POST_IDS:
            if MIDNIGHT_RUN:
                # 0時モード: 既存ブロックを全除去してから本日ラベルで再生成
                related_html = build_related_html(all_post_infos or [], post["id"], post.get("category"))
                payload["edit_text_1"] = inject_related_html(payload["edit_text_1"], related_html)
                log.info(f"    📎 回遊リスト: 再生成（0時モード）")
            else:
                related_html = build_related_html(all_post_infos or [], post["id"], post.get("category"))
                payload["edit_text_1"] = inject_related_html(payload["edit_text_1"], related_html)
            all_others = [p for p in (all_post_infos or []) if p["post"]["id"] != post["id"]]
            # ログもカテゴリ回遊ルールに合わせてフィルタ
            cur_cat = post.get("category")
            LOCAL_ONLY = {"神奈川県", "埼玉県", "多摩"}
            if cur_cat in LOCAL_ONLY:
                all_others = [p for p in all_others if p["post"].get("category") == cur_cat]
            else:
                all_others = [p for p in all_others if p["post"].get("category") not in LOCAL_ONLY]
            tomorrow_count = len([p for p in all_others if p["is_tomorrow"]])
            future_count   = len([p for p in all_others if not p["is_tomorrow"] and p["next_date"] is not None])
            if all_others:
                log.info(f"    📎 回遊リスト: 明日{tomorrow_count}件 / 明後日以降{future_count}件")
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

    # repostフィールドを明示的に制御（フォームHTMLから紛れ込み防止）
    payload.pop(REPOST_FIELD, None)
    if do_repost:
        payload[REPOST_FIELD] = "on"
        log.info(f"    🔄 再投稿チェックON")

    res = session.post(EDIT_FORM_ACTION, data=payload)
    if res.status_code == 200:
        action_str = "再投稿＋タイトル更新" if do_repost else "タイトル更新（編集のみ）"
        log.info(f"    ✅ {action_str}: {new_title}")
        return True

    log.error(f"    ❌ 更新失敗 (status: {res.status_code})")
    return False


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
        return

    posts = fetch_post_list(session)
    if not posts:
        log.warning("⚠️  記事が見つかりませんでした")
        session.close()
        return

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
            dates_list, is_tomorrow, is_today = fetch_next_date_from_schedule(details["schedule_url"])
            if dates_list:
                dates = ",".join(dates_list)
                log.info(f"    📅 直近の出勤日: {dates}")

        new_title = post["title"]
        if dates:
            dates_list_raw = []
            for part in dates.split(","):
                dates_list_raw.append(part)
            new_title = build_new_title(post["title"], dates_list_raw)

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
        time.sleep(1)

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

        res = session.post(EDIT_FORM_ACTION, data=payload)
        if res.status_code == 200:
            log.info(f"    ✅ {area_label} まとめ記事更新完了")
        else:
            log.warning(f"    ⚠️  {area_label} まとめ記事更新失敗 (HTTP {res.status_code})")
        time.sleep(2)

    session.close()
    log.info(f"\n✅ カレンダー更新完了 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")


# ============================================================
# メイン処理
# ============================================================
def run_update():
    log.info(f"\n{'='*55}")
    log.info(f"🔍 更新チェック開始 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")
    log.info(f"{'='*55}")

    session = login_wakust()
    if not session:
        return

    posts = fetch_post_list(session)
    if not posts:
        log.warning("⚠️  記事が見つかりませんでした")
        session.close()
        return

    # PV記録は記事情報収集後に実行（0時モードのみ）

    state = load_state()

    # 各記事の情報を収集
    post_infos = []
    for post in posts:
        log.info(f"\n📄 [{post['id']}] {post['title']}")

        if post.get("is_reserved"):
            log.info(f"    ⏭️  予約投稿のためスキップ")
            continue

        try:
            details = fetch_post_details(session, post)
        except Exception as e:
            log.error(f"    ❌ 記事詳細取得失敗: {e}")
            continue
        post["category"] = details["category"]

        # 記事公開ページからアルファベットタグとタイトル画像を取得
        tags, image_url = fetch_post_tags(session, post["url"])

        if not details["schedule_url"]:
            log.warning(f"    ⚠️  スケジュールURLなし。回遊リストのみ対象")
            post_infos.append({
                "post":      post,
                "details":   details,
                "next_date": None,
                "is_tomorrow":  False,
                "is_today":    False,
                "new_title": _strip_today_tag(post["title"]),
                "tags":      tags,
                "image_url": image_url,
            })
            continue

        log.info(f"    🔗 {details['schedule_url']}")

        dates, is_tomorrow, is_today = fetch_next_date_from_schedule(details["schedule_url"])
        if not dates:
            log.warning(f"    ⚠️  出勤日取得失敗。回遊リストのみ対象")
            # 出勤日不明でもタイトル更新・回遊リスト対象として追加
            post_infos.append({
                "post":      post,
                "details":   details,
                "next_date": None,
                "is_tomorrow":  False,
                "is_today":    False,
                "new_title": _strip_today_tag(post["title"]),
                "tags":      tags,
                "image_url": image_url,
            })
            continue

        dates_str = ",".join(dates)
        log.info(f"    📅 直近の出勤日: {dates_str} {'【明日出勤！】' if is_tomorrow else ''}")

        new_title = build_new_title(post["title"], dates)
        # 0時モード: 本日出勤の記事にハッシュタグを付与
        if MIDNIGHT_RUN and is_today:
            new_title = new_title.rstrip() + TODAY_TAG
        post_infos.append({
            "post":      post,
            "details":   details,
            "next_date": dates_str,
            "is_tomorrow":  is_tomorrow,
            "is_today":    is_today,
            "new_title": new_title,
            "tags":      tags,
            "image_url": image_url,
        })
        time.sleep(1)

    # PVを記録＋比較レポート生成（0時モードのみ）
    if MIDNIGHT_RUN:
        log_pv(posts, post_infos=post_infos, state=state)
        generate_pv_report(posts)

    # 再投稿対象を決定
    # カテゴリーごとに上限まで: 明日出勤(ID降順) → 明後日以降(PV降順) で補充
    # 神奈川県・埼玉県は0時モードで再投稿、それ以外は17時モードで再投稿
    repost_ids = set()
    log.info(f"\n{'─'*55}")
    log.info(f"📊 再投稿対象選定")

    # カテゴリーごとに記事を分類
    posts_by_category = defaultdict(list)
    for info in post_infos:
        posts_by_category[info["post"]["category"]].append(info)

    for category, infos in posts_by_category.items():
        # 0時モードでは MIDNIGHT_REPOST_CATEGORIES のみ、17時モードではそれ以外を再投稿
        if MIDNIGHT_RUN and category not in MIDNIGHT_REPOST_CATEGORIES:
            log.info(f"  🌙 カテゴリー「{category}」: 0時モード対象外。スキップ")
            continue
        if not MIDNIGHT_RUN and category in MIDNIGHT_REPOST_CATEGORIES:
            log.info(f"  🕐 カテゴリー「{category}」: 0時モードで再投稿するためスキップ")
            continue

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

        if MIDNIGHT_RUN:
            # 0時モード: 本日出勤 → 明日以降出勤 の優先順で選定
            primary = [i for i in eligible if i["is_today"]]
            primary.sort(key=lambda x: x["post"].get("pv_total") or 0, reverse=True)
            secondary = [i for i in eligible if not i["is_today"]]
            secondary.sort(key=lambda x: x["post"].get("pv_total") or 0, reverse=True)
            primary_label, secondary_label = "本日", "明日以降"
        else:
            # 17時モード: 明日出勤 → 明後日以降出勤 の優先順で選定
            primary = [i for i in eligible if i["is_tomorrow"]]
            primary.sort(key=lambda x: x["post"].get("pv_total") or 0, reverse=True)
            secondary = [i for i in eligible if not i["is_tomorrow"]]
            secondary.sort(key=lambda x: x["post"].get("pv_total") or 0, reverse=True)
            primary_label, secondary_label = "明日", "明後日以降"

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

        for info in selected:
            repost_ids.add(info["post"]["id"])
            label = primary_label if info in primary else secondary_label
            pv = info["post"].get("pv_total") or 0
            log.info(f"    [{info['post']['id']}] 再投稿対象（{label}, PV={pv}）")

        log.info(f"  🏷️  カテゴリー「{category}」: 空き{slots}枠 → {primary_label}{len(primary)}件+{secondary_label}{len(secondary)}件 → 選定{len(selected)}件")

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
            res = session.post(EDIT_FORM_ACTION, data=payload)
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
            time.sleep(2)
            continue

        # 0時モード: ラベル切替が未実施なら常に更新
        midnight_needs_swap = MIDNIGHT_RUN and post_state.get("labels_swapped_date") != jst_strftime("%Y-%m-%d")

        # next_date=Noneの記事はタイトル更新・再投稿しない（回遊リストのみ）
        if info["next_date"] is None:
            do_repost = False
            if not related_changed and not midnight_needs_swap:
                log.info(f"\n    ℹ️  [{post_id}] 出勤日不明・変化なし。スキップ")
                continue

        if not title_changed and not date_changed and not do_repost and not related_changed and not midnight_needs_swap:
            log.info(f"\n    ℹ️  [{post_id}] 変化なし。スキップ")
            continue

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
                "labels_swapped_date": jst_strftime("%Y-%m-%d") if MIDNIGHT_RUN else "",
            }
            save_state(state)

        time.sleep(2)

    session.close()
    log.info(f"\n✅ 全処理完了 ({jst_strftime('%Y-%m-%d %H:%M:%S')})")


# ============================================================
# エントリーポイント
# ============================================================
if __name__ == "__main__":
    if CALENDAR_ONLY:
        log.info(f"🚀 ワクスト自動更新スクリプト起動 [カレンダーのみモード]")
        run_calendar_only()
    else:
        mode = "0時モード（回遊ラベル切替・神奈川/埼玉再投稿）" if MIDNIGHT_RUN else "17時モード（通常）"
        log.info(f"🚀 ワクスト自動更新スクリプト起動 [{mode}]")
        log.info(f"   MIDNIGHT_RUN={os.environ.get('MIDNIGHT_RUN', '(未設定)')}")
        run_update()
