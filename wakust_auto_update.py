"""
ワクスト 記事タイトル自動更新 ＋ カテゴリー別再投稿スクリプト
================================================================
毎日 0:00 に以下を実行します:

1. 記事一覧から全記事のURLとタイトルを取得
2. 各記事の編集画面(edit_text_2)からスケジュールURLを取得
3. スケジュールページから当日以降で最も近い出勤日を取得
4. タイトルの【日付出勤】部分を更新
5. 無料部分(edit_text_1)の末尾に本日出勤中の他記事リンクを追記
   ※ 元の本文は絶対に変更しない
6. 当日出勤がある記事はカテゴリーごとに最大4件まで再投稿

使い方:
  pip install requests beautifulsoup4 schedule
  python wakust_auto_update.py          # 常駐モード（毎日0:00）
  python wakust_auto_update.py --once   # 1回だけ実行して終了
"""

import requests
from bs4 import BeautifulSoup
import schedule
import time
import re
import json
import os
import sys
from datetime import datetime
from collections import defaultdict

# ============================================================
# ★ 設定（必要に応じて変更してください）
# ============================================================
WAKUST_EMAIL    = os.environ.get("WAKUST_EMAIL", "")
WAKUST_PASSWORD = os.environ.get("WAKUST_PASSWORD", "")

MAX_REPOST_PER_CATEGORY = 4

# ============================================================
# 定数
# ============================================================
STATE_FILE          = "wakust_state.json"
BASE_URL            = "https://wakust.com"
LOGIN_AJAX_URL      = "https://wakust.com/wp-content/themes/wakust/user_edit/login_mypage.php"
POST_LIST_URL       = f"{BASE_URL}/mypage/?post_list"
EDIT_FORM_ACTION    = f"{BASE_URL}/useredit/"
REPOST_FIELD        = "repost"
RELATED_BLOCK_START       = "<!-- related_posts_start -->"
RELATED_BLOCK_END         = "<!-- related_posts_end -->"
RELATED_NEXT_BLOCK_START  = "<!-- related_next_posts_start -->"
RELATED_NEXT_BLOCK_END    = "<!-- related_next_posts_end -->"


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
        print("✅ ログイン成功")
        return session

    print(f"❌ ログイン失敗: {res.text[:100]}")
    return None


# ============================================================
# 記事一覧の取得
# ============================================================
def fetch_post_list(session):
    res  = session.get(POST_LIST_URL)
    soup = BeautifulSoup(res.text, "html.parser")

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
        posts.append({
            "id":       post_id,
            "title":    title,
            "url":      url,
            "edit_url": f"{BASE_URL}/mypage/?post_edit={post_id}",
            "category": "未分類",
        })

    print(f"📋 取得記事数: {len(posts)}")
    return posts


# ============================================================
# 編集画面の詳細取得
# ============================================================
def fetch_post_details(session, post):
    res  = session.get(post["edit_url"])
    soup = BeautifulSoup(res.text, "html.parser")
    form    = soup.find("form", action=lambda a: a and "useredit" in a)
    cat_sel = soup.find("select", {"name": "categorys"})

    # カテゴリーIDをHTMLから直接取得
    # selected属性は値なし属性（selected のみ）なのでhas_attr()で判定する
    # X/4 のカウントを読み取り、4/4なら再投稿不可フラグを立てる
    category_id       = None
    category          = "未分類"
    category_at_limit = False  # True=上限4/4に達している
    if cat_sel:
        for opt in cat_sel.find_all("option"):
            if opt.has_attr("selected"):
                category_id = opt.get("value")
                category    = opt.get_text(strip=True)
                m = re.search(r"\((\d+)/(\d+)\)", category)
                if m and int(m.group(1)) >= int(m.group(2)):
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

    # edit_text_2（有料部分）からスケジュールURLを抽出
    # URLは <a href="..."> タグ内またはプレーンテキストで記載されている
    schedule_url = None
    edit_text_2  = payload.get("edit_text_2", "")

    soup_t2 = BeautifulSoup(edit_text_2, "html.parser")
    for a in reversed(soup_t2.find_all("a", href=True)):
        href = a["href"].strip()
        if re.match(r"https?://", href) and "wakust.com" not in href:
            schedule_url = href
            break

    if not schedule_url:
        for line in reversed(edit_text_2.splitlines()):
            clean = re.sub(r"<[^>]+>", "", line).strip()
            if re.match(r"https?://", clean) and "wakust.com" not in clean:
                schedule_url = clean
                break

    return {
        "category":        category,
        "schedule_url":    schedule_url,
        "payload":         payload,
        "at_limit":        category_at_limit,
    }


# ============================================================
# スケジュールページから直近の出勤日を取得
# ============================================================
def fetch_next_date_from_schedule(schedule_url):
    try:
        res = requests.get(schedule_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")
    except Exception as e:
        print(f"    ❌ スケジュール取得失敗: {e}")
        return None, False

    today        = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    current_year = today.year
    candidates   = []

    for table in soup.find_all("table"):
        # 形式A: thに月日、tdに出勤情報（zexterior・rex-luxury等）
        headers = table.find_all("th")
        cells   = table.find_all("td")
        if headers and cells:
            for header, cell in zip(headers, cells):
                info = cell.get_text(strip=True)
                if not info or "お休み" in info or not re.search(r"\d{2}:\d{2}", info):
                    continue
                # 「3月5日」または「3/5(木)」形式どちらも対応
                m = re.search(r"(\d+)月\s*(\d+)日", header.get_text())
                if not m:
                    m = re.search(r"(\d{1,2})/(\d{1,2})", header.get_text())
                if m:
                    month, day = int(m.group(1)), int(m.group(2))
                    d = datetime(current_year, month, day)
                    if d >= today:
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
                        if d < today:
                            continue
                        info = info_cells[i].get_text(" ", strip=True) if i < len(info_cells) else ""
                        if "未定" in info or "お休み" in info or not re.search(r"\d{2}:\d{2}", info):
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
            if d >= today:
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
                if d < today:
                    continue
                if re.search(r"\d{2}:\d{2}", info_text) and "お休み" not in info_text:
                    candidates.append((d, f"{month}/{day}"))
            if candidates:
                break

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
                if d < today:
                    continue
                if i < len(sche_divs):
                    info = sche_divs[i].get_text(" ", strip=True)
                    if "未定" in info or not re.search(r"\d{2}:\d{2}", info):
                        continue
                candidates.append((d, f"{month}/{day}"))

    # パターン5: 「3/5(木)20:00」同一行形式（tokyo-menes・galaxy等）
    if not candidates:
        for m in re.finditer(r"(\d{1,2})/(\d{1,2})\([月火水木金土日]\)[^\n]{0,5}(\d{2}:\d{2})", soup.get_text()):
            month, day = int(m.group(1)), int(m.group(2))
            d = datetime(current_year, month, day)
            if d >= today:
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
            if d >= today:
                candidates.append((d, f"{month}/{day}"))

    # パターン5: 「3月7日」テキスト形式
    if not candidates:
        for m in re.finditer(r"(\d{1,2})月(\d{1,2})日[^\n]*?(\d{2}:\d{2})", soup.get_text()):
            month, day = int(m.group(1)), int(m.group(2))
            d = datetime(current_year, month, day)
            if d >= today:
                candidates.append((d, f"{month}/{day}"))

    if not candidates:
        return None, False

    candidates.sort(key=lambda x: x[0])
    nearest_date, nearest_str = candidates[0]
    is_today = (nearest_date.date() == today.date())
    return nearest_str, is_today


# ============================================================
# タイトルの【日付出勤】部分を置換
# ============================================================
def build_new_title(current_title, new_date):
    # 【】内に日付+出勤パターンがあれば置換（カップ数等は保持）
    # 重複（【3/5出勤3/5出勤Iカップ】等）も同時に修正する
    # replacedフラグで「置換が実際に起きたか」を管理し、二重追加を防ぐ
    replaced = [False]

    def replace_bracket(m):
        inner = m.group(1)
        if not re.search(r"[\d/,]+出勤", inner):
            return m.group(0)  # 日付+出勤がなければそのまま
        inner_clean = re.sub(r"[\d/,\s]+出勤", "", inner)
        replaced[0] = True
        return f"【{new_date}出勤{inner_clean}】"

    new_title = re.sub(r"【([^】]*)】", replace_bracket, current_title, count=1)

    if not replaced[0]:
        new_title = f"【{new_date}出勤】" + current_title
    return new_title


# ============================================================
# 回遊リスト（本日・直近出勤の他記事リンク）の生成・注入
# ============================================================
def build_related_html(all_post_infos, current_post_id):
    """本日出勤・明日以降出勤を1ブロック内にまとめて生成（更新した全記事対象）"""
    others = [p for p in all_post_infos if p["post"]["id"] != current_post_id]
    today_others  = [p for p in others if p["is_today"]]
    # next_date=Noneや今日以前の日付は除外
    from datetime import datetime
    today_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    def is_future(info):
        if info["is_today"] or info["next_date"] is None:
            return False
        try:
            m, d = info["next_date"].split("/")
            dt = datetime(today_dt.year, int(m), int(d))
            return dt > today_dt
        except Exception:
            return False

    future_others = [p for p in others if is_future(p)]

    if not today_others and not future_others:
        return ""

    inner = "<hr/>\n"

    # 本日出勤セクション
    if today_others:
        items_html = ""
        for info in today_others:
            title = info["new_title"] or info["post"]["title"]
            url   = info["post"]["url"]
            items_html += f'<li><a href="{url}">{title}</a></li>\n'
        inner += (
            f'<p><strong>📅 本日出勤中の他の記事もチェック！</strong></p>\n'
            f'<ul>\n{items_html}</ul>\n'
        )

    # 明日以降出勤セクション（日付昇順）
    if future_others:
        future_others = sorted(future_others, key=lambda p: (
            int(p["next_date"].split("/")[0]),
            int(p["next_date"].split("/")[1])
        ))
        items_html = ""
        for info in future_others:
            title = info["new_title"] or info["post"]["title"]
            url   = info["post"]["url"]
            items_html += f'<li><a href="{url}">{title}</a></li>\n'
        inner += (
            f'<p><strong>📆 明日以降出勤予定の他の記事もチェック！</strong></p>\n'
            f'<ul>\n{items_html}</ul>\n'
        )

    return f'\n{RELATED_BLOCK_START}\n{inner}{RELATED_BLOCK_END}\n'


def inject_related_html(original_html, related_html):
    # 旧形式の直近ブロックが残っていれば削除
    if RELATED_NEXT_BLOCK_START in original_html:
        original_html = re.sub(
            rf"{re.escape(RELATED_NEXT_BLOCK_START)}.*?{re.escape(RELATED_NEXT_BLOCK_END)}",
            "",
            original_html,
            flags=re.DOTALL,
        )
    # メインブロックを置換または末尾追記
    if RELATED_BLOCK_START in original_html:
        return re.sub(
            rf"{re.escape(RELATED_BLOCK_START)}.*?{re.escape(RELATED_BLOCK_END)}",
            related_html.strip() if related_html else "",
            original_html,
            flags=re.DOTALL,
        )
    if related_html:
        return original_html.rstrip() + "\n" + related_html
    return original_html


# ============================================================
# 記事の更新
# ============================================================
def update_post(session, post, details, new_title, do_repost=False, all_post_infos=None):
    payload = dict(details["payload"])

    payload["edit_title"] = new_title

    if "edit_text_1" in payload:
        related_html = build_related_html(all_post_infos or [], post["id"])
        payload["edit_text_1"] = inject_related_html(payload["edit_text_1"], related_html)
        all_others = [p for p in (all_post_infos or []) if p["post"]["id"] != post["id"]]
        today_count  = len([p for p in all_others if p["is_today"]])
        future_count = len([p for p in all_others if not p["is_today"]])
        if all_others:
            print(f"    📎 回遊リスト: 本日{today_count}件 / 明日以降{future_count}件")
        else:
            print(f"    📎 回遊リストなし")

    if do_repost:
        payload[REPOST_FIELD] = "on"
        print(f"    🔄 再投稿チェックON")

    res = session.post(EDIT_FORM_ACTION, data=payload)
    if res.status_code == 200:
        action_str = "再投稿＋タイトル更新" if do_repost else "タイトル更新（編集のみ）"
        print(f"    ✅ {action_str}: {new_title}")
        return True

    print(f"    ❌ 更新失敗 (status: {res.status_code})")
    return False


# ============================================================
# メイン処理
# ============================================================
def run_update():
    print(f"\n{'='*55}")
    print(f"🔍 更新チェック開始 ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"{'='*55}")

    session = login_wakust()
    if not session:
        return

    posts = fetch_post_list(session)
    if not posts:
        print("⚠️  記事が見つかりませんでした")
        session.close()
        return

    state = load_state()

    # 各記事の情報を収集
    post_infos = []
    for post in posts:
        print(f"\n📄 [{post['id']}] {post['title']}")

        details = fetch_post_details(session, post)
        post["category"] = details["category"]

        if not details["schedule_url"]:
            print(f"    ⚠️  スケジュールURLなし。スキップ")
            continue

        print(f"    🔗 {details['schedule_url']}")

        next_date, is_today = fetch_next_date_from_schedule(details["schedule_url"])
        if not next_date:
            print(f"    ⚠️  出勤日取得失敗。回遊リストのみ対象")
            # 出勤日不明でもタイトル更新・回遊リスト対象として追加
            post_infos.append({
                "post":      post,
                "details":   details,
                "next_date": None,
                "is_today":  False,
                "new_title": post["title"],  # タイトルは変えない
            })
            continue

        print(f"    📅 直近の出勤日: {next_date} {'【本日出勤！】' if is_today else ''}")

        new_title = build_new_title(post["title"], next_date)
        post_infos.append({
            "post":      post,
            "details":   details,
            "next_date": next_date,
            "is_today":  is_today,
            "new_title": new_title,
        })
        time.sleep(1)

    # カテゴリー別に再投稿対象を決定（IDが大きい＝新しい順に最大4件）
    today_posts_by_category = defaultdict(list)
    for info in post_infos:
        if info["is_today"]:
            today_posts_by_category[info["post"]["category"]].append(info)

    repost_ids = set()
    print(f"\n{'─'*55}")
    print("📊 再投稿対象の選定")
    for category, infos in today_posts_by_category.items():
        # カテゴリーが上限4/4の記事は再投稿しない
        eligible = [i for i in infos if not i["details"].get("at_limit", False)]
        selected = sorted(eligible, key=lambda x: int(x["post"]["id"]), reverse=True)[:MAX_REPOST_PER_CATEGORY]
        for info in selected:
            repost_ids.add(info["post"]["id"])
        skipped = len(infos) - len(eligible)
        skip_str = f"（上限超え{skipped}件スキップ）" if skipped else ""
        print(f"  🏷️  カテゴリー「{category}」: 本日出勤{len(infos)}件 → 再投稿{len(selected)}件{skip_str}")

    today_post_infos = [i for i in post_infos if i["is_today"]]

    # 直近出勤グループ: 本日以外で直近出勤日ごとにグルーピング
    # 各記事の「直近出勤日」が同じ記事をまとめる（本日出勤は除く）
    from collections import defaultdict as _dd
    next_date_groups = _dd(list)
    for info in post_infos:
        if not info["is_today"]:
            next_date_groups[info["next_date"]].append(info)

    # 更新実行
    print(f"\n{'─'*55}")
    print("🚀 更新処理開始")
    print(f"{'─'*55}")

    # 本日出勤記事のIDセット（回遊リスト比較用）
    today_ids_str = ",".join(sorted(i["post"]["id"] for i in today_post_infos))

    for info in post_infos:
        post_id       = info["post"]["id"]
        new_title     = info["new_title"]
        do_repost     = post_id in repost_ids
        post_state    = state.get(post_id, {})
        title_changed = (new_title != info["post"]["title"])
        date_changed  = (post_state.get("date") != info["next_date"])
        # 更新記事の顔ぶれが変わっていたら回遊リストも更新が必要
        all_ids_str = ",".join(sorted(i["post"]["id"] for i in post_infos))
        related_changed = post_state.get("all_ids") != all_ids_str

        # next_date=Noneの記事はタイトル更新・再投稿しない（回遊リストのみ）
        if info["next_date"] is None:
            do_repost = False
            if not related_changed:
                print(f"\n    ℹ️  [{post_id}] 出勤日不明・変化なし。スキップ")
                continue

        if not title_changed and not date_changed and not do_repost and not related_changed:
            print(f"\n    ℹ️  [{post_id}] 変化なし。スキップ")
            continue

        print(f"\n📝 [{post_id}] {info['post']['title']}")
        print(f"    → {new_title}")

        if update_post(session, info["post"], info["details"], new_title, do_repost, post_infos):
            state[post_id] = {
                "date":       info["next_date"],
                "title":      new_title,
                "reposted":   do_repost,
                "all_ids":    all_ids_str,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_state(state)

        time.sleep(2)

    session.close()
    print(f"\n✅ 全処理完了 ({time.strftime('%Y-%m-%d %H:%M:%S')})")


# ============================================================
# エントリーポイント
# ============================================================
if __name__ == "__main__":
    once_mode = "--once" in sys.argv

    print("🚀 ワクスト自動更新スクリプト起動")
    print(f"   モード: {'1回実行して終了' if once_mode else '常駐（毎日 0:00 に実行）'}")
    print(f"   カテゴリー別再投稿上限: {MAX_REPOST_PER_CATEGORY}件\n")

    run_update()

    if not once_mode:
        schedule.every().day.at("00:00").do(run_update)
        while True:
            schedule.run_pending()
            time.sleep(30)
