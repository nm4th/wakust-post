"""livedoor Blog へ記事を転載する（AtomPub API）

ワクストの記事の「無料部分」だけを livedoor Blog に投稿し、続き（店名・
セラピスト名・詳細）はワクスト／codoc の購入ページへ誘導する。
有料部分（edit_text_2）は取得も保存も投稿もしない。

狙いは検索流入ではなく、livedoor の
  アダルト(一般) > メンズエステ(R-18)
カテゴリの「新着エントリー」と「カテゴリ内ランキング」からの流入。
そのため一度に大量投稿せず、1日数本ずつ出して新着枠に長く居座らせる。

必要な環境変数:
  LIVEDOOR_USER_ID    livedoor ID（WSSE のユーザー名）
  LIVEDOOR_BLOG_NAME  ブログ識別子（ルートエンドポイントの末尾）
  LIVEDOOR_API_KEY    AtomPub用パスワード（ログインパスワードとは別）

未設定のときは dry-run になり、投稿せずに内容を表示するだけ。

  python wakust_livedoor.py                 # 次に出す1本を表示（投稿しない）
  python wakust_livedoor.py --limit 3       # 3本ぶん表示
  python wakust_livedoor.py --limit 3 --post
  python wakust_livedoor.py --id 1601302 --post
"""

import os
import re
import sys
import json
import glob
import base64
import hashlib
import argparse
import logging
from datetime import datetime, timezone, timedelta
from xml.sax.saxutils import escape

import requests

log = logging.getLogger("wakust.livedoor")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

JST = timezone(timedelta(hours=9))
ATOM_BASE = "https://livedoor.blogcms.jp/atompub"
ARTICLES_DIR = "site_content/articles"
STATE_FILE = "wakust_state.json"
CONFIG_FILE = "site_config.json"

# 記事末尾に置く注意書き。参考にしている運用と同じで、
# 「同じ施術が受けられる保証はない」ことを明示しておく
DISCLAIMER = ("※メンズエステはセラピストとの相性が重要なため、"
              "記事と同じ施術が受けられる保証はございません。")


class LivedoorError(RuntimeError):
    pass


# ============================================================
# 認証（WSSE）
# ============================================================
def _wsse_header(user_id, api_key):
    """WSSE 認証ヘッダを組み立てる

    nonce は毎回変える必要があるので os.urandom を使う。
    PasswordDigest = Base64(SHA1(nonce + created + password))
    """
    nonce = os.urandom(20)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = hashlib.sha1(nonce + created.encode("utf-8")
                          + api_key.encode("utf-8")).digest()
    return ('UsernameToken Username="{}", PasswordDigest="{}", '
            'Nonce="{}", Created="{}"').format(
        user_id,
        base64.b64encode(digest).decode("ascii"),
        base64.b64encode(nonce).decode("ascii"),
        created,
    )


class LivedoorClient:
    def __init__(self, user_id=None, blog_name=None, api_key=None, dry_run=None):
        self.user_id = user_id or os.environ.get("LIVEDOOR_USER_ID", "").strip()
        self.blog_name = blog_name or os.environ.get("LIVEDOOR_BLOG_NAME", "").strip()
        self.api_key = api_key or os.environ.get("LIVEDOOR_API_KEY", "").strip()
        self.dry_run = (not (self.user_id and self.blog_name and self.api_key)
                        ) if dry_run is None else dry_run
        if self.dry_run:
            log.info("🧪 livedoor: dry-run モード（実際には投稿しません）")

    def _headers(self):
        return {"X-WSSE": _wsse_header(self.user_id, self.api_key),
                "Content-Type": "application/atom+xml; charset=utf-8"}

    def _send(self, method, url, entry, what):
        try:
            r = requests.request(method, url, data=entry.encode("utf-8"),
                                 timeout=60, headers=self._headers())
        except requests.RequestException as e:
            raise LivedoorError(f"{what}のリクエストに失敗: {e}") from e
        if r.status_code == 401:
            raise LivedoorError(
                "認証に失敗しました。LIVEDOOR_USER_ID（livedoor ID）と "
                "LIVEDOOR_API_KEY（AtomPub用パスワード。ログインパスワードとは別）"
                "を確認してください")
        if r.status_code not in (200, 201):
            raise LivedoorError(f"{what} HTTP {r.status_code}: {r.text[:300]}")
        return r.text

    def post(self, title, body_html, categories=None, draft=False):
        """記事を1件投稿して (公開URL, 編集URL) を返す

        編集URL は後からタイトル・本文を差し替えるのに使うので、
        呼び出し側で必ず保存しておくこと。
        """
        entry = _build_atom_entry(title, body_html, categories or [], draft)
        if self.dry_run:
            log.info(f"🧪 [dry-run] 投稿: {title}")
            return "", ""
        text = self._send("POST", f"{ATOM_BASE}/{self.blog_name}/article",
                          entry, "投稿")
        return _link(text, "alternate"), _link(text, "edit")

    def update(self, edit_url, title, body_html, categories=None, draft=False):
        """投稿済みの記事を差し替える（POSTだと新規になるので必ずPUT）"""
        entry = _build_atom_entry(title, body_html, categories or [], draft)
        if self.dry_run:
            log.info(f"🧪 [dry-run] 更新: {title}")
            return ""
        text = self._send("PUT", edit_url, entry, "更新")
        return _link(text, "alternate")


def _link(xml_text, rel):
    m = re.search(r'<link[^>]+rel="%s"[^>]*/?>' % re.escape(rel), xml_text)
    if not m:
        return ""
    h = re.search(r'href="([^"]+)"', m.group(0))
    return h.group(1) if h else ""


def _build_atom_entry(title, body_html, categories, draft):
    cats = "".join(f'<category term="{escape(c)}" />' for c in categories)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<entry xmlns="http://www.w3.org/2005/Atom"\n'
        '       xmlns:app="http://www.w3.org/2007/app">\n'
        f'  <title>{escape(title)}</title>\n'
        f'  <content type="text/html">{escape(body_html)}</content>\n'
        f'  {cats}\n'
        '  <app:control>\n'
        f'    <app:draft>{"yes" if draft else "no"}</app:draft>\n'
        '  </app:control>\n'
        '</entry>\n'
    )


# ============================================================
# 記事の組み立て
# ============================================================
def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_articles():
    out = []
    for path in glob.glob(os.path.join(ARTICLES_DIR, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


def clean_title(title):
    """ワクストのタイトルから、転載先で意味を持たない部分を落とす

    末尾のハッシュタグ（#8/19,8/20 #本日出勤 #東京都内）と、
    先頭の【8/19.20出勤】は、日が変われば嘘になるので消す。
    """
    t = re.sub(r"\s*#\S+", "", title or "").strip()
    t = re.sub(r"^【[\d./]+出勤】\s*", "", t)
    return t.strip()


def build_categories(article):
    """livedoor 側の記事カテゴリを決める（エリア軸 × プレイ内容軸）"""
    cats = []
    for key in ("station", "area"):
        v = (article.get(key) or "").strip()
        if v and v not in cats:
            cats.append(v)
    for t in (article.get("tags") or []):
        # カップ数と駅名はカテゴリにしない（駅は上で入れている）
        if t.endswith("カップ") or t == article.get("station"):
            continue
        if t not in cats:
            cats.append(t)
    return cats[:5]


def _buy_button(label, url, color):
    return (f'<a href="{escape(url)}" target="_blank" rel="noopener" '
            f'style="display:inline-block;margin:6px 8px;padding:12px 26px;'
            f'background:{color};color:#111;font-weight:bold;'
            f'border-radius:6px;text-decoration:none;">{escape(label)}</a>')


def build_cta(article, cfg):
    """記事末尾の購入導線

    ワクストは決済手段が限られるので、codoc のエントリーがある記事では
    そちらも並べて、読者に決済手段を選ばせる（参考にしている運用と同じ形）。
    codoc 未作成の記事ではワクストだけを出す。
    """
    ccfg = cfg.get("codoc") or {}
    sitecode = (ccfg.get("sitecode") or ccfg.get("usercode") or "").strip()
    entry_code = (article.get("codoc_entry_code") or "").strip()
    codoc_url = (f"https://codoc.jp/sites/{sitecode}/entries/{entry_code}"
                 if sitecode and entry_code else "")

    buttons = []
    if article.get("source_url"):
        buttons.append(_buy_button("ワクスト購入ページへ", article["source_url"],
                                   "#4ade80"))
    if codoc_url:
        buttons.append(_buy_button("codoc購入ページへ", codoc_url, "#93c5fd"))

    if not buttons:
        return ""

    note = ("（codocはVisaとMastercardがご利用できます）"
            if codoc_url else "")
    return (
        '<div style="margin:32px 0;padding:20px;text-align:center;'
        'border:1px solid #ddd;border-radius:8px;">'
        '<p style="font-weight:bold;margin:0 0 4px;">'
        '▼ セラピスト名、店名、詳しい施術内容はこちら</p>'
        + (f'<p style="font-size:13px;margin:0 0 12px;">{note}</p>' if note else "")
        + "".join(buttons)
        + '</div>'
    )


WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


def today_iso():
    return datetime.now(JST).strftime("%Y-%m-%d")


def fmt_date(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    return f"{d.month}/{d.day}({WEEKDAY_JP[d.weekday()]})"


def upcoming_shifts(article):
    """今日以降の出勤日だけを返す（過去日を載せても意味がないため）"""
    today = today_iso()
    return [d for d in (article.get("shift_dates") or []) if d >= today]


def build_shift_block(article):
    """本文の先頭に置く出勤日ブロック

    タイトルには出勤日を入れない。検索結果に古い日付が残り続けるうえ、
    タイトルを頻繁に変えると評価が安定しないため。日付は本文のここだけを
    毎日 PUT で差し替える。
    """
    upcoming = upcoming_shifts(article)
    if not upcoming:
        return ('<div style="margin:0 0 20px;padding:12px 16px;background:#f6f6f6;'
                'border-left:4px solid #ccc;font-size:14px;">'
                '📅 <b>直近の出勤</b>：現在の予定は未定です'
                '<br><span style="font-size:12px;color:#888;">'
                '最新の出勤日はワクストの記事ページでご確認ください</span></div>')
    labels = " ・ ".join(fmt_date(d) for d in upcoming[:5] if fmt_date(d))
    return ('<div style="margin:0 0 20px;padding:12px 16px;background:#fff7e6;'
            'border-left:4px solid #f59e0b;font-size:14px;">'
            f'📅 <b>直近の出勤</b>：{labels}'
            f'<br><span style="font-size:12px;color:#888;">'
            f'{fmt_date(today_iso())}時点</span></div>')


def build_body(article, cfg):
    """投稿本文を組み立てる（出勤日＋無料部分＋購入導線＋免責）"""
    free = (article.get("free_html") or "").strip()
    if not free:
        return ""
    parts = [build_shift_block(article), free, build_cta(article, cfg),
             f'<p style="color:#888;font-size:12px;">{DISCLAIMER}</p>']
    return "\n".join(p for p in parts if p)


# ============================================================
# 本日出勤まとめ（毎日1本、新規投稿する）
# ============================================================
def build_matome(articles, cfg, state, day=None):
    """その日出勤するセラピストを1本にまとめた記事を作る

    個別記事のタイトルは固定にしているので、出勤日という「消え物」は
    こちらで受け持つ。編集ではなく毎日「新規投稿」なので、livedoor の
    新着エントリーに確実に載る。各記事への内部リンクにもなる。
    """
    day = day or today_iso()
    posted = state.get("_livedoor") or {}
    items = [a for a in articles if day in (a.get("shift_dates") or [])]
    if not items:
        return None

    # 転載済みならブログ内の記事へ、未転載ならワクストへ飛ばす
    def link_for(a):
        rec = posted.get(str(a.get("id"))) or {}
        return rec.get("url") or a.get("source_url") or ""

    by_area = {}
    for a in items:
        by_area.setdefault(a.get("area") or "その他", []).append(a)

    order = ["東京都内", "神奈川", "埼玉", "多摩", "千葉"]
    areas = ([k for k in order if k in by_area]
             + sorted(k for k in by_area if k not in order))

    rows = []
    for area in areas:
        group = sorted(by_area[area], key=lambda a: (a.get("station") or ""))
        rows.append(f'<h3 style="margin:24px 0 8px;">{escape(area)}</h3><ul>')
        for a in group:
            cup = next((t for t in (a.get("tags") or []) if t.endswith("カップ")), "")
            plays = [t for t in (a.get("tags") or [])
                     if not t.endswith("カップ") and t != a.get("station")]
            detail = " / ".join(x for x in [cup] + plays[:2] if x)
            label = f'【{a.get("station") or area}】{detail}'.strip()
            url = link_for(a)
            rows.append(f'<li>{f_link(label, url)}</li>' if url
                        else f'<li>{escape(label)}</li>')
        rows.append("</ul>")

    title = f"【{fmt_date(day)} 本日出勤】体験済みセラピスト{len(items)}名"
    body = (
        f'<p>{fmt_date(day)}に出勤予定の、実際に行ってレポートを書いた'
        f'セラピストをまとめました。</p>'
        + "".join(rows)
        + '<p style="margin-top:24px;font-size:13px;color:#666;">'
        '出勤予定は変更されることがあります。'
        '最新の状況は各記事のリンク先でご確認ください。</p>'
        f'<p style="color:#888;font-size:12px;">{DISCLAIMER}</p>'
    )
    return {"title": title, "body": body, "categories": ["本日出勤"],
            "day": day, "count": len(items)}


def f_link(label, url):
    return (f'<a href="{escape(url)}" target="_blank" rel="noopener">'
            f'{escape(label)}</a>')


# ============================================================
# エリア別まとめ（東京都内・神奈川…の常設ハブ記事）
# ============================================================
AREA_ORDER = ["東京都内", "神奈川", "埼玉", "多摩", "千葉"]
# 記事が少ないうちにハブを作っても中身がスカスカなので、この本数から作る
AREA_MIN_ITEMS = 5


def build_area_matome(area, articles, cfg, state):
    """エリア1つぶんのまとめ記事を組み立てる

    本日出勤まとめが「その日限りの新着枠狙い」なのに対して、
    こちらは貼りっぱなしのハブ。記事が増えるたびに PUT で追記していく。
    駅ごとにまとめて、そのエリアで探している人が選べる形にする。
    """
    posted = state.get("_livedoor") or {}
    items = [a for a in articles if (a.get("area") or "") == area
             and str(a.get("id")) in posted]
    if len(items) < AREA_MIN_ITEMS:
        return None

    by_station = {}
    for a in items:
        by_station.setdefault(a.get("station") or area, []).append(a)

    rows = []
    for st in sorted(by_station, key=lambda k: (-len(by_station[k]), k)):
        group = sorted(by_station[st],
                       key=lambda a: -int(a.get("sales_count") or 0))
        rows.append(f'<h3 style="margin:24px 0 8px;">{escape(st)}'
                    f'（{len(group)}件）</h3><ul>')
        for a in group:
            cup = next((t for t in (a.get("tags") or []) if t.endswith("カップ")), "")
            plays = [t for t in (a.get("tags") or [])
                     if not t.endswith("カップ") and t != a.get("station")]
            detail = " / ".join(x for x in [cup] + plays[:2] if x) or st
            url = (posted.get(str(a["id"])) or {}).get("url") or a.get("source_url")
            rows.append(f'<li>{f_link(detail, url)}</li>' if url
                        else f'<li>{escape(detail)}</li>')
        rows.append("</ul>")

    title = f"【{area}】メンズエステ体験レポートまとめ｜{len(items)}件"
    body = (
        f'<p>{escape(area)}で実際に行ってレポートを書いたセラピストを、'
        f'駅ごとにまとめました。現在{len(items)}件です。</p>'
        '<p style="font-size:13px;color:#666;">'
        '新しいレポートを書くたびに追記しています。</p>'
        + "".join(rows)
        + f'<p style="color:#888;font-size:12px;margin-top:24px;">{DISCLAIMER}</p>'
    )
    return {"title": title, "body": body,
            "categories": [area, "まとめ"], "area": area,
            "ids": sorted(str(a["id"]) for a in items), "count": len(items)}


def run_area_matome(client, articles, cfg, state, draft=False):
    """エリア別まとめを作る／更新する

    まだ無ければ新規投稿、あって中身が変わっていれば PUT で差し替える。
    """
    hubs = state.setdefault("_livedoor_area", {})
    ok = ng = 0
    areas = ([a for a in AREA_ORDER]
             + sorted({(x.get("area") or "") for x in articles} - set(AREA_ORDER)))
    for area in areas:
        if not area:
            continue
        m = build_area_matome(area, articles, cfg, state)
        if not m:
            continue
        rec = hubs.get(area) or {}
        if rec.get("ids") == m["ids"]:
            continue          # 中身が変わっていないので触らない
        try:
            if rec.get("edit_url"):
                url = client.update(rec["edit_url"], m["title"], m["body"],
                                    m["categories"], draft)
                edit_url = rec["edit_url"]
                verb = "更新"
            else:
                url, edit_url = client.post(m["title"], m["body"],
                                            m["categories"], draft)
                verb = "新規"
        except LivedoorError as e:
            print(f"❌ エリアまとめ{verb}失敗 [{area}]: {e}")
            print(f"::error::エリアまとめの投稿に失敗しました [{area}]: {e}")
            ng += 1
            continue
        hubs[area] = {"url": url or rec.get("url", ""),
                      "edit_url": edit_url, "ids": m["ids"],
                      "at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")}
        save_state(state)
        ok += 1
        print(f"✅ エリアまとめ{verb} [{area}] {m['count']}件 "
              f"{hubs[area]['url'] or '(dry-run)'}")
    return ok, ng


# ============================================================
# 出す順番
# ============================================================
def pick_targets(articles, limit, state):
    """まだ転載していない記事から、販売実績が多い順に limit 件返す"""
    done = (state.get("_livedoor") or {})
    todo = [a for a in articles
            if str(a.get("id")) not in done and (a.get("free_html") or "").strip()]
    todo.sort(key=lambda a: -int(a.get("sales_count") or 0))
    return todo[:max(1, limit)]


def run_publish(client, articles, cfg, state, limit, draft=False):
    """未転載の記事を limit 件だけ新規投稿する"""
    targets = pick_targets(articles, limit, state)
    if not targets:
        return 0, 0
    posted = state.setdefault("_livedoor", {})
    ok = ng = 0
    for a in targets:
        title = clean_title(a.get("title"))
        body = build_body(a, cfg)
        if not body:
            continue
        try:
            url, edit_url = client.post(title, body, build_categories(a), draft)
        except LivedoorError as e:
            print(f"❌ 投稿失敗 [{a['id']}]: {e}")
            print(f"::error::livedoorへの投稿に失敗しました [{a['id']}]: {e}")
            ng += 1
            continue
        posted[str(a["id"])] = {
            "at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            "url": url, "edit_url": edit_url,
            # 出勤日を控えておき、変わったときだけ本文を差し替える
            "shifts": list(a.get("shift_dates") or []),
        }
        save_state(state)
        ok += 1
        print(f"✅ 投稿 [{a['id']}] {url or '(dry-run)'}  {title[:38]}")
    return ok, ng


def run_refresh(client, articles, cfg, state, limit, draft=False):
    """投稿済み記事の出勤日ブロックを差し替える

    タイトルは変えない。出勤予定が変わった記事だけを対象にする。
    """
    posted = state.get("_livedoor") or {}
    by_id = {str(a.get("id")): a for a in articles}
    todo = []
    for aid, rec in posted.items():
        a = by_id.get(aid)
        if not a or not rec.get("edit_url"):
            continue
        if list(a.get("shift_dates") or []) != list(rec.get("shifts") or []):
            todo.append((aid, rec, a))
    if not todo:
        return 0, 0
    ok = ng = 0
    for aid, rec, a in todo[:max(1, limit)]:
        body = build_body(a, cfg)
        if not body:
            continue
        try:
            client.update(rec["edit_url"], clean_title(a.get("title")), body,
                          build_categories(a), draft)
        except LivedoorError as e:
            print(f"❌ 更新失敗 [{aid}]: {e}")
            print(f"::error::livedoorの記事更新に失敗しました [{aid}]: {e}")
            ng += 1
            continue
        rec["shifts"] = list(a.get("shift_dates") or [])
        rec["refreshed_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        ok += 1
        print(f"🔄 出勤日を更新 [{aid}] {' '.join(fmt_date(d) for d in upcoming_shifts(a)[:3])}")
    return ok, ng


# 本日出勤まとめを始める記事数。
# 転載記事が少ないうちに出しても、リンク先の大半がブログ外になって
# まとめとして成立しない。まず個別記事で更新実績を積む
MATOME_MIN_POSTED = 30


def run_matome(client, articles, cfg, state, draft=False):
    """本日出勤まとめを1本、新規投稿する（1日1回まで）"""
    day = today_iso()
    posted_n = len(state.get("_livedoor") or {})
    if posted_n < MATOME_MIN_POSTED:
        print(f"⏭️  転載記事が{posted_n}件のため、本日出勤まとめはまだ出しません"
              f"（{MATOME_MIN_POSTED}件から）")
        return 0, 0
    done = state.setdefault("_livedoor_matome", {})
    if day in done:
        print(f"⏭️  本日({day})のまとめは投稿済み")
        return 0, 0
    m = build_matome(articles, cfg, state, day)
    if not m:
        print(f"⏭️  本日({day})出勤の記事がないため、まとめは出しません")
        return 0, 0
    try:
        url, edit_url = client.post(m["title"], m["body"], m["categories"], draft)
    except LivedoorError as e:
        print(f"❌ まとめ投稿失敗: {e}")
        print(f"::error::livedoorへのまとめ投稿に失敗しました: {e}")
        return 0, 1
    done[day] = {"url": url, "edit_url": edit_url, "count": m["count"],
                 "at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")}
    save_state(state)
    print(f"✅ まとめ投稿 {url or '(dry-run)'}  {m['title']}")
    return 1, 0


def main():
    ap = argparse.ArgumentParser(description="livedoor Blog へ記事を転載する")
    ap.add_argument("--limit", type=int, default=1, help="新規投稿する記事数")
    ap.add_argument("--id", help="記事IDを指定して1件だけ表示・投稿する")
    ap.add_argument("--post", action="store_true",
                    help="実際に投稿する（認証情報が無ければdry-run）")
    ap.add_argument("--draft", action="store_true", help="下書きとして投稿する")
    ap.add_argument("--matome", action="store_true",
                    help="本日出勤まとめを投稿する")
    ap.add_argument("--refresh", type=int, metavar="N", default=0,
                    help="投稿済み記事の出勤日ブロックを最大N件更新する")
    ap.add_argument("--area-matome", action="store_true",
                    help="エリア別まとめを作る／更新する")
    args = ap.parse_args()

    cfg = load_config()
    articles = load_articles()
    if not articles:
        print(f"記事データがありません（{ARTICLES_DIR}/ が空）")
        return 0
    state = load_state()

    # --- 表示のみ（--post なし）---
    if not args.post:
        if args.area_matome:
            shown = 0
            for area in AREA_ORDER + sorted(
                    {(a.get("area") or "") for a in articles} - set(AREA_ORDER)):
                m = build_area_matome(area, articles, cfg, state)
                if not m:
                    continue
                print("=" * 60)
                print(f"タイトル : {m['title']}")
                print(f"カテゴリ : {' / '.join(m['categories'])}")
                print("-" * 60)
                print(m["body"][:900])
                print()
                shown += 1
            if not shown:
                posted_n = len(state.get("_livedoor") or {})
                print(f"エリアまとめの対象がありません"
                      f"（転載済み {posted_n}件 / 1エリアあたり "
                      f"{AREA_MIN_ITEMS}件から作成）")
            return 0
        if args.matome:
            m = build_matome(articles, cfg, state)
            if not m:
                print("本日出勤の記事がありません")
                return 0
            print("=" * 60)
            print(f"タイトル : {m['title']}")
            print(f"対象     : {m['count']}名")
            print("-" * 60)
            print(m["body"][:1500])
            return 0
        if args.id:
            targets = [a for a in articles if str(a.get("id")) == str(args.id)]
        else:
            targets = pick_targets(articles, args.limit, state)
        if not targets:
            empty = sum(1 for a in articles if not (a.get("free_html") or "").strip())
            print("転載できる記事がありません。")
            if empty:
                print(f"  無料部分が未取得の記事が {empty}件あります。")
                print("  CODOC_MODE=free_backfill python wakust_auto_update.py "
                      "で取り込んでください。")
            return 0
        for a in targets:
            body = build_body(a, cfg)
            print("=" * 60)
            print(f"タイトル : {clean_title(a.get('title'))}")
            print(f"カテゴリ : {' / '.join(build_categories(a))}")
            print(f"本文     : {len(body)}文字")
            print("-" * 60)
            print(body[:1500])
            print()
        return 0

    # --- 実投稿 ---
    client = LivedoorClient()
    ng = 0
    if args.refresh:
        _, n = run_refresh(client, articles, cfg, state, args.refresh, args.draft)
        ng += n
    if args.area_matome:
        _, n = run_area_matome(client, articles, cfg, state, args.draft)
        ng += n
    if args.matome:
        _, n = run_matome(client, articles, cfg, state, args.draft)
        ng += n
    if args.limit and not args.id:
        _, n = run_publish(client, articles, cfg, state, args.limit, args.draft)
        ng += n
    elif args.id:
        a = next((x for x in articles if str(x.get("id")) == str(args.id)), None)
        if not a:
            print(f"記事 {args.id} が見つかりません")
            return 1
        _, n = run_publish(client, [a], cfg, state, 1, args.draft)
        ng += n
    return 1 if ng else 0


if __name__ == "__main__":
    sys.exit(main())
