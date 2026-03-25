"""
ワクスト 記事タイトル自動更新 ＋ 翌日出勤記事再投稿スクリプト
====================================================================
毎日16:00 JSTと0:00 JSTに実行し、以下を行います。

■ 16:00モード（通常）:
  1. 記事一覧から全記事のURLとタイトルを取得
  2. 各記事の編集画面(edit_text_2)からスケジュールURLを取得
  3. スケジュールページから翌日以降で最も近い出勤日を最大3件取得
  4. タイトルの【日付出勤】部分を更新
     - 同月: 【3/13,14,15出勤】  月またぎ: 【3/13,14｜4/4出勤】
  5. 無料部分に「〇月〇日更新」を挿入
  6. 無料部分の回遊リスト: 明日出勤(グループ1)・明後日以降出勤(グループ2)
  7. 翌日出勤の記事を再投稿（カテゴリ上限4/4・無料部分URLの記事は除外）
  8. PVデータをCSVに記録

■ 0:00モード（MIDNIGHT_RUN=1）:
  - 16時に作成済みの回遊リストのラベルを文字置換:
    明日出勤予定→本日出勤中、明後日以降出勤予定→明日以降出勤予定
  - 再投稿しない
  - 「〇月〇日更新」の書き換えもしない

使い方:
  pip install requests beautifulsoup4
  python wakust_auto_update.py                # 16:00モード
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

# タイムゾーン（GitHub ActionsはUTCで動くため、JST明示が必須）
JST = timezone(timedelta(hours=9))

def jst_strftime(fmt):
    """time.strftimeのJST版"""
    return datetime.now(JST).strftime(fmt)

# MIDNIGHT_RUN: 実際のJST時刻で自動判定（22:00-05:59 → 0時モード）
# 環境変数での明示指定も可能（"1"=強制0時モード, "0"=強制通常モード）
_midnight_env = os.environ.get("MIDNIGHT_RUN", "")
if _midnight_env in ("0", "1"):
    MIDNIGHT_RUN = _midnight_env == "1"
else:
    _jst_hour = datetime.now(JST).hour
    MIDNIGHT_RUN = _jst_hour >= 22 or _jst_hour < 6


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
                    m_sc = re.search(r"販売\s*[：:]\s*(\d+)", text)
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
    """記事の公開ページからアルファベットのみのタグを抽出する。

    タグは「KEYWORD(NUMBER)」形式で表示されている。
    例: CKB(127), F(1473), HR(23397), 中野(989), 巨乳(19987)
    → アルファベットのみ: ["CKB", "F", "HR"]
    """
    try:
        res = session.get(post_url)
        if res.status_code != 200:
            log.warning(f"    ⚠️  タグ取得失敗 (HTTP {res.status_code})")
            return []
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
        return tags
    except Exception as e:
        log.warning(f"    ⚠️  タグ取得エラー: {e}")
        return []


# ============================================================
# スケジュールページから直近の出勤日を取得
# ============================================================
def fetch_next_date_from_schedule(schedule_url):
    try:
        res = requests.get(schedule_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }, timeout=10)
        if res.status_code != 200:
            log.error(f"    ❌ スケジュール取得失敗 (HTTP {res.status_code}): {schedule_url}")
            return [], False, False
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
        log.error(f"    ❌ スケジュール取得失敗: {e}")
        return [], False, False

    # JSレンダリング判定: weekScheduleクラスがあるがtableが空の場合
    # → Playwrightでヘッドレスブラウザ経由で再取得
    if (soup.find(class_=re.compile(r"weekSchedule", re.I)) and
            not soup.find("table")):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(schedule_url, wait_until="networkidle", timeout=20000)
                js_html = page.content()
                browser.close()
            soup = BeautifulSoup(js_html, "html.parser")
            log.info(f"    🔧 JSレンダリングでHTML再取得成功")
        except Exception as e:
            log.warning(f"    ⚠️ Playwrightフォールバック失敗: {e}")

    today        = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    # 16時モード: 翌日以降の出勤日のみ / 0時モード: 当日以降の出勤日
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
    if not candidates:
        sch_date = soup.find("div", class_=re.compile(r"sch-date"))
        sch_work = soup.find("div", class_=re.compile(r"sch-work"))
        if sch_date and sch_work:
            dts = sch_date.find_all("dt")
            dds = sch_work.find_all("dd")
            for dt, dd in zip(dts, dds):
                info = dd.get_text(strip=True)
                if "休み" in info or "未定" in info:
                    continue
                if not re.search(r"\d{2}:\d{2}", info) and "満枠" not in info:
                    continue
                m = re.search(r"(\d{1,2})/(\d{1,2})", dt.get_text())
                if not m:
                    continue
                month, day = int(m.group(1)), int(m.group(2))
                d = datetime(current_year, month, day)
                if d >= start_date:
                    candidates.append((d, f"{month}/{day}"))

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
        for cls in ("schedule", "sche", "date", "shift", "calendar", "week"):
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
    """日付リストを短縮表記にフォーマット
    同月: "3/13,14,15"  月またぎ: "3/13,14｜4/4"
    """
    if not dates:
        return ""
    # dates: ["3/13", "3/14", "4/4"] 形式
    groups = []  # [(month, [day, day, ...]), ...]
    for d in dates:
        m, day = d.split("/")
        if groups and groups[-1][0] == m:
            groups[-1][1].append(day)
        else:
            groups.append((m, [day]))
    parts = []
    for m, days in groups:
        parts.append(f"{m}/{','.join(days)}")
    return "｜".join(parts)


def build_new_title(current_title, dates):
    # dates: リスト（例: ["3/13", "3/14", "3/15"]）
    # 【】内に日付+出勤パターンがあれば置換（カップ数等は保持）
    # 重複（【3/5出勤3/5出勤Iカップ】等）も同時に修正する
    # replacedフラグで「置換が実際に起きたか」を管理し、二重追加を防ぐ
    date_str = format_dates(dates)
    replaced = [False]

    def replace_bracket(m):
        inner = m.group(1)
        if not re.search(r"[\d/,｜]+出勤", inner):
            return m.group(0)  # 日付+出勤がなければそのまま
        inner_clean = re.sub(r"[\d/,｜\s]+出勤", "", inner)
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

    16:00モード: グループ1=明日出勤、グループ2=明後日以降出勤
    0:00モード:  グループ1=今日出勤、グループ2=明日以降出勤

    カテゴリ回遊ルール:
      - 神奈川県: 神奈川県内のみで回遊
      - 東京都/池袋/新宿: 互いに回遊OK
    """
    others = [p for p in all_post_infos if p["post"]["id"] != current_post_id]

    # カテゴリ別回遊フィルタリング
    # 神奈川県: 神奈川県同士のみ / それ以外: 神奈川県以外すべてで回遊
    if current_category:
        if current_category == "神奈川県":
            others = [p for p in others if p["post"].get("category") == "神奈川県"]
        else:
            others = [p for p in others if p["post"].get("category") != "神奈川県"]

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
        # 16時モード: グループ1=明日出勤(is_tomorrow)、グループ2=明後日以降
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
        """グループを縦積みカード型HTMLに変換する（スマホ最適化）"""
        group = sorted(group, key=lambda p: p["post"].get("sales_count") or 0, reverse=True)
        group = group[:5]
        cards = ""
        for info in group:
            title = info["new_title"] or info["post"]["title"]
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
            # アルファベットタグバッジ（例: CK | F | HR）を1つにまとめて表示
            post_tags = info.get("tags", [])
            if post_tags:
                badge_html += (
                    f'<span style="display:inline-block;background:#d48806;color:#fff;'
                    f'font-size:11px;padding:2px 8px;border-radius:4px;margin-right:4px">'
                    f'{" | ".join(post_tags)}</span>'
                )
            cards += (
                f'<div style="border:1px solid #333;border-radius:8px;padding:10px 12px;'
                f'margin-bottom:8px">'
            )
            if badge_html:
                cards += f'<div style="margin-bottom:6px">{badge_html}</div>'
            cards += (
                f'<a href="{url}" style="color:#6db3f2;text-decoration:none;'
                f'font-size:14px;line-height:1.5">{main}</a>'
                f'</div>\n'
            )
        return (
            f'<p style="margin-bottom:8px"><strong>{label}</strong></p>\n'
            f'{cards}'
        )

    inner = "<hr/>\n"

    if group1:
        inner += _build_card_list(group1, label1)

    if group2:
        inner += _build_card_list(group2, label2)

    return f'\n{RELATED_BLOCK_START}\n{inner}{RELATED_BLOCK_END}\n'


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
def update_post(session, post, details, new_title, do_repost=False, all_post_infos=None):
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
            if cur_cat == "神奈川県":
                all_others = [p for p in all_others if p["post"].get("category") == "神奈川県"]
            else:
                all_others = [p for p in all_others if p["post"].get("category") != "神奈川県"]
            tomorrow_count = len([p for p in all_others if p["is_tomorrow"]])
            future_count   = len([p for p in all_others if not p["is_tomorrow"] and p["next_date"] is not None])
            if all_others:
                log.info(f"    📎 回遊リスト: 明日{tomorrow_count}件 / 明後日以降{future_count}件")
            else:
                log.info(f"    📎 回遊リストなし")

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

        # 記事公開ページからアルファベットタグを取得
        tags = fetch_post_tags(session, post["url"])

        if not details["schedule_url"]:
            log.warning(f"    ⚠️  スケジュールURLなし。回遊リストのみ対象")
            post_infos.append({
                "post":      post,
                "details":   details,
                "next_date": None,
                "is_tomorrow":  False,
                "is_today":    False,
                "new_title": post["title"],
                "tags":      tags,
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
                "new_title": post["title"],  # タイトルは変えない
                "tags":      tags,
            })
            continue

        dates_str = ",".join(dates)
        log.info(f"    📅 直近の出勤日: {dates_str} {'【明日出勤！】' if is_tomorrow else ''}")

        new_title = build_new_title(post["title"], dates)
        post_infos.append({
            "post":      post,
            "details":   details,
            "next_date": dates_str,
            "is_tomorrow":  is_tomorrow,
            "is_today":    is_today,
            "new_title": new_title,
            "tags":      tags,
        })
        time.sleep(1)

    # PVを記録（0時モードのみ）
    if MIDNIGHT_RUN:
        log_pv(posts, post_infos=post_infos, state=state)

    # 再投稿対象を決定（0時モードでは再投稿しない）
    # カテゴリーごとに上限まで: 明日出勤(ID降順) → 明後日以降(PV降順) で補充
    repost_ids = set()
    if MIDNIGHT_RUN:
        log.info(f"\n{'─'*55}")
        log.info(f"🌙 0時モード: 再投稿チェックをスキップ")
    else:
        log.info(f"\n{'─'*55}")
        log.info(f"📊 再投稿対象選定")

        # カテゴリーごとに記事を分類
        posts_by_category = defaultdict(list)
        for info in post_infos:
            posts_by_category[info["post"]["category"]].append(info)

        for category, infos in posts_by_category.items():
            # 再投稿の基本条件: 上限未達 & 有料セクションURL由来
            eligible = [i for i in infos
                        if not i["details"].get("at_limit", False)
                        and not i["details"].get("schedule_from_free", False)
                        and i["next_date"] is not None]

            if not eligible:
                continue

            # カテゴリの空き枠を計算（全記事で同じカテゴリの最初の1件から取得）
            cat_current = infos[0]["details"].get("category_current", 0)
            cat_max     = infos[0]["details"].get("category_max", 4)
            slots = max(0, cat_max - cat_current)

            if slots == 0:
                log.info(f"  🏷️  カテゴリー「{category}」: 上限{cat_current}/{cat_max} → 空き枠なし")
                continue

            # 1) 明日出勤の記事をID降順で選定
            tomorrow = [i for i in eligible if i["is_tomorrow"]]
            tomorrow.sort(key=lambda x: x["post"]["id"], reverse=True)

            # 2) 明後日以降の記事をPV降順で選定
            future = [i for i in eligible if not i["is_tomorrow"]]
            future.sort(key=lambda x: x["post"].get("pv_total") or 0, reverse=True)

            # 上限まで埋める
            selected = []
            for info in tomorrow:
                if len(selected) >= slots:
                    break
                selected.append(info)

            for info in future:
                if len(selected) >= slots:
                    break
                selected.append(info)

            for info in selected:
                repost_ids.add(info["post"]["id"])
                is_tmr = "明日" if info["is_tomorrow"] else "明後日以降"
                pv = info["post"].get("pv_total") or 0
                log.info(f"    [{info['post']['id']}] 再投稿対象（{is_tmr}, PV={pv}）")

            log.info(f"  🏷️  カテゴリー「{category}」: 空き{slots}枠 → 明日{len(tomorrow)}件+明後日以降{len(future)}件 → 選定{len(selected)}件")

    # 全記事更新＋再投稿
    log.info(f"\n{'─'*55}")
    log.info("🚀 更新処理開始（全記事更新＋再投稿）")
    log.info(f"{'─'*55}")

    for info in post_infos:
        post_id       = info["post"]["id"]
        new_title     = info["new_title"]
        do_repost     = post_id in repost_ids
        post_state    = state.get(post_id, {})
        title_changed = (new_title != info["post"]["title"])
        date_changed  = (post_state.get("dates") != info["next_date"])
        # 更新記事の顔ぶれが変わっていたら回遊リストも更新が必要
        all_ids_str = ",".join(sorted(i["post"]["id"] for i in post_infos))
        related_changed = post_state.get("all_ids") != all_ids_str

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

        if update_post(session, info["post"], info["details"], new_title, do_repost, post_infos):
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
    mode = "0時モード（回遊ラベル切替・再投稿なし）" if MIDNIGHT_RUN else "16時モード（通常）"
    log.info(f"🚀 ワクスト自動更新スクリプト起動 [{mode}]")
    log.info(f"   MIDNIGHT_RUN={os.environ.get('MIDNIGHT_RUN', '(未設定)')}")
    run_update()
