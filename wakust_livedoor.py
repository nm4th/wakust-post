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
import html
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
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _is_gone(e):
    """記事が消えている（ブログ側で削除された）か"""
    return getattr(e, "status", None) in (404, 410)


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

    def _headers(self, basic=False):
        h = {"Content-Type": "application/atom+xml; charset=utf-8"}
        if basic:
            token = base64.b64encode(
                f"{self.user_id}:{self.api_key}".encode("utf-8")).decode("ascii")
            h["Authorization"] = f"Basic {token}"
        else:
            h["X-WSSE"] = _wsse_header(self.user_id, self.api_key)
        return h

    def _send(self, method, url, entry, what):
        """Basicで送り、401ならWSSEで再試行する

        livedoor は WSSE と Basic の両方を受け付けることになっているが、
        実際に試すと WSSE は401で Basic だけ通った。毎回401を踏むのは
        無駄なので Basic を先に出す。将来 Basic が塞がれても動くよう、
        WSSE も残してある。
        """
        data = entry.encode("utf-8") if entry is not None else None
        last = None
        for basic in (True, False):
            try:
                r = requests.request(method, url, data=data, timeout=60,
                                     headers=self._headers(basic))
            except requests.RequestException as e:
                raise LivedoorError(f"{what}のリクエストに失敗: {e}") from e
            if r.status_code == 401:
                last = r
                log.info(f"    🔑 {'Basic' if basic else 'WSSE'} 認証は401")
                continue
            if r.status_code not in (200, 201):
                raise LivedoorError(f"{what} HTTP {r.status_code}: {r.text[:300]}",
                                    status=r.status_code)
            if not basic:
                log.info("    🔑 WSSE認証で通りました（Basicは不可）")
            return r.text
        raise LivedoorError(
            "認証に失敗しました（Basic・WSSEとも401）。次を確認してください:\n"
            "  1. LIVEDOOR_API_KEY は AtomPub用パスワード（英数10文字）。"
            "ログインパスワードではありません。\n"
            "     ※「発行する」を押し直した場合、Secretsの値も入れ直しが必要です\n"
            "  2. LIVEDOOR_USER_ID は livedoor ID。ブログ識別子とは別のことがあります\n"
            "  3. LIVEDOOR_BLOG_NAME はルートエンドポイント末尾の識別子\n"
            f"     （いまの設定: {ATOM_BASE}/{self.blog_name}）\n"
            f"  参考: {last.text[:200] if last is not None else ''}")

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
        alt, edit = _link(text, "alternate"), _link(text, "edit")
        if edit and not alt:
            # POST のレスポンスに rel="alternate"（公開URL）が入らないことが
            # ある。記事末の関連記事リンクに使うので、作った記事を読み直して
            # でも押さえておく。ここで取れなくても投稿自体は成功扱いにする。
            try:
                alt = _link(self.fetch(edit), "alternate")
            except LivedoorError as e:
                log.warning(f"公開URLの取得に失敗しました: {e}")
        return alt, edit

    def update(self, edit_url, title, body_html, categories=None, draft=False):
        """投稿済みの記事を差し替える（POSTだと新規になるので必ずPUT）"""
        entry = _build_atom_entry(title, body_html, categories or [], draft)
        if self.dry_run:
            log.info(f"🧪 [dry-run] 更新: {title}")
            return ""
        text = self._send("PUT", edit_url, entry, "更新")
        return _link(text, "alternate")

    def upload_image(self, data, filename, content_type="image/jpeg"):
        """画像を livedoor にアップロードして、レスポンスをそのまま返す

        「記事の見出し画像」は cover_image_attachment_id という
        アップロード済み画像のIDを持つ。AtomPub からこの項目を
        設定できるかは公開情報が無いので、まずアップロードして
        レスポンスに何が返るかを見る。

        エンドポイントは新旧2つの記述があるため、両方試す。
        """
        if self.dry_run:
            log.info(f"🧪 [dry-run] 画像アップロード: {filename}")
            return ""
        urls = [f"{ATOM_BASE}/{self.blog_name}/image",
                f"https://livedoor.blogcms.jp/atom/blog/{self.blog_name}/image"]
        last = None
        for url in urls:
            for basic in (True, False):
                h = self._headers(basic)
                h["Content-Type"] = content_type
                h["Slug"] = filename
                try:
                    r = requests.post(url, data=data, timeout=60, headers=h)
                except requests.RequestException as e:
                    last = f"{url}: 通信エラー {e}"
                    continue
                log.info(f"    📤 {url} ({'Basic' if basic else 'WSSE'}) "
                         f"→ HTTP {r.status_code}")
                if r.status_code in (200, 201):
                    return r.text
                last = f"{url}: HTTP {r.status_code} {r.text[:200]}"
        raise LivedoorError(f"画像アップロードに失敗: {last}")

    def fetch(self, url):
        """AtomPub のエントリーを取得して、生のXMLを返す"""
        if self.dry_run:
            return ""
        return self._send("GET", url, None, "取得")

    def check(self):
        """投稿せずに認証だけ確認する"""
        url = f"{ATOM_BASE}/{self.blog_name}"
        print(f"エンドポイント : {url}")
        print(f"LIVEDOOR_USER_ID   : {_mask(self.user_id)}")
        print(f"LIVEDOOR_BLOG_NAME : {self.blog_name or '(未設定)'}")
        print(f"LIVEDOOR_API_KEY   : {_mask(self.api_key)}"
              f"  ← AtomPub用パスワードは英数10文字")
        if self.user_id and self.blog_name and self.user_id == self.blog_name:
            print("⚠️  USER_ID と BLOG_NAME が同じ値です。"
                  "livedoor ID とブログ識別子が違う場合は401になります")
        if self.dry_run:
            print("❌ 認証情報が足りません")
            return 1
        for basic in (True, False):
            name = "Basic" if basic else "WSSE"
            try:
                r = requests.get(url, timeout=30, headers=self._headers(basic))
            except requests.RequestException as e:
                print(f"❌ {name}: 通信エラー {e}")
                continue
            print(f"{'✅' if r.status_code == 200 else '❌'} {name}: "
                  f"HTTP {r.status_code}")
            if r.status_code == 200:
                for t in re.findall(r"<title[^>]*>([^<]+)</title>", r.text)[:3]:
                    print(f"     ブログ: {t}")
                return 0
            if r.status_code != 401:
                print(f"     {r.text[:200]}")
        return 1


def _mask(v):
    if not v:
        return "(未設定)"
    return f"{v[0]}{'*' * (len(v) - 2)}{v[-1]}（{len(v)}文字）" if len(v) > 2 else "***"


def parse_image_response(xml_text):
    """画像アップロードのレスポンスから ID と URL を取り出す

    返るXMLの形:
      <link rel="edit" href=".../image/14068837" />
      <content type="image/jpeg" src="https://livedoor.blogimg.jp/.../x.jpg"
               thumbnail="https://livedoor.blogimg.jp/.../x-s.jpg"/>
    """
    out = {"id": "", "url": "", "thumbnail": ""}
    edit = _link(xml_text, "edit")
    m = re.search(r"/image/(\d+)", edit or "")
    if m:
        out["id"] = m.group(1)
    m = re.search(r'<content[^>]+src="([^"]+)"', xml_text)
    if m:
        out["url"] = m.group(1)
    m = re.search(r'<content[^>]+thumbnail="([^"]+)"', xml_text)
    if m:
        out["thumbnail"] = m.group(1)
    return out


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


# ワクスト固有で、転載先では邪魔になるブロック
_STRIP_BLOCKS = [
    # 「8月21日更新 ※販売回数が2回増えるごとに100pt値上げします」
    # ワクストの値上げ告知。転載先では意味がなく、日付も古くなる
    ("<!-- updated_date_start -->", "<!-- updated_date_end -->"),
    # ワクスト内の回遊リンク
    ("<!-- related_posts_start -->", "<!-- related_posts_end -->"),
    ("<!-- related_next_posts_start -->", "<!-- related_next_posts_end -->"),
    # ワクストの有料パート誘導。こちらは独自のCTAを出すので二重になる
    ("<!-- paid_preview_start -->", "<!-- paid_preview_end -->"),
    ('<div id="calendar_block_start" style="display:none"></div>',
     '<div id="calendar_block_end" style="display:none"></div>'),
    ("<!-- calendar_block_start -->", "<!-- calendar_block_end -->"),
]
_SCRIPT_RE = re.compile(r"<\s*(script|iframe|object|embed)\b.*?<\s*/\s*\1\s*>",
                        re.I | re.S)
_SELF_CLOSING_RE = re.compile(r"<\s*(script|iframe|object|embed)\b[^>]*/?>", re.I)
_ON_ATTR_RE = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_JS_HREF_RE = re.compile(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.I)
_STYLE_RE = re.compile(r'style="([^"]*)"')
_WHITE_RE = re.compile(r"color:\s*#(?:fff|ffffff)\s*;?", re.I)
_LEAD_EMPTY_RE = re.compile(r"^(?:\s|<p>\s*(?:&nbsp;|\xa0)?\s*</p>|<br\s*/?>)+", re.I)
_TRAIL_EMPTY_RE = re.compile(
    r"(?:\s|<p>\s*(?:&nbsp;|\xa0)?\s*</p>|<br\s*/?>|<hr\s*/?>)+$", re.I)
_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "section")


def _cut_paid_teaser(text):
    """ワクストの「有料パートでは…」の予告を落とす

    マーカーで囲まれた paid_preview とは別に、手書きの予告が本文末尾に
    残っている記事がある。転載先では独自のCTAを出すので不要。
    実データで確認した限り、この予告より後ろに本文は無い。
    """
    i = text.find("有料パート")
    if i < 0:
        return text
    start = max((text.rfind(f"<{t}", 0, i) for t in _BLOCK_TAGS), default=-1)
    return text[:start] if start > 0 else text[:i]


def _fix_white_text(m):
    """白背景で消える白文字だけを直す

    ワクストは暗い背景なので本文に白文字が多い。ただし大半は見出しなどで
    自前の背景色を持っているため、そのままで読める。背景を持たない要素の
    白文字だけが livedoor の白背景で見えなくなるので、そこだけ色指定を外す。
    """
    style = m.group(1)
    if not _WHITE_RE.search(style):
        return m.group(0)
    if re.search(r"background", style, re.I):
        return m.group(0)          # 自前の背景があるので触らない
    return 'style="%s"' % _WHITE_RE.sub("", style).strip().strip(";")


def clean_for_livedoor(raw):
    """ワクストの無料部分を、転載先でそのまま読める形に整える"""
    if not raw:
        return ""
    text = html.unescape(raw)
    for start, end in _STRIP_BLOCKS:
        while True:
            i = text.find(start)
            if i < 0:
                break
            j = text.find(end, i)
            if j < 0:
                text = text[:i]
                break
            text = text[:i] + text[j + len(end):]
    text = _SCRIPT_RE.sub("", text)
    text = _SELF_CLOSING_RE.sub("", text)
    text = _ON_ATTR_RE.sub("", text)
    text = _JS_HREF_RE.sub(r'\1="#"', text)
    text = _STYLE_RE.sub(_fix_white_text, text)
    # 対になっていない残りのマーカーコメントを落とす
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = _cut_paid_teaser(text)
    text = _LEAD_EMPTY_RE.sub("", text)
    return _TRAIL_EMPTY_RE.sub("", text).strip()


_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)


def extract_thumbnail(raw):
    """記事の見出し画像を取り出す

    ワクストの無料本文に入っている画像は2種類ある。
      - related_posts ブロック … 他の記事のサムネイル（不要）
      - paid_preview ブロック  … その記事自身の見出し画像（これが欲しい）
    どちらも整形時に落としてしまうので、落とす前に後者だけ拾っておく。
    """
    if not raw:
        return ""
    text = html.unescape(raw)
    i = text.find("<!-- paid_preview_start -->")
    if i < 0:
        return ""
    block = text[i:]
    for url in _IMG_RE.findall(block):
        if "s.w.org" in url:      # 絵文字のSVG
            continue
        return url
    return ""


def clean_title(title):
    """ワクストのタイトルから、転載先で意味を持たない部分を落とす

    末尾のハッシュタグ（#8/19,8/20 #本日出勤 #東京都内）と、
    先頭の【8/19.20出勤】は、日が変われば嘘になるので消す。
    """
    t = re.sub(r"\s*#\S+", "", title or "").strip()
    # 先頭の出勤ブロックを落とす。日が変わると嘘になるので転載先には出さない。
    # 元データに【8/23.24.25出勤出勤】のような打ち間違いがあるため、
    # 「数字・記号・出勤」だけで構成された括弧をまとめて対象にする
    while True:
        t2 = re.sub(r"^【(?:[\d./,、\s]|出勤)+】\s*", "", t)
        if t2 == t:
            break
        t = t2
    return t.strip()


def build_title(article):
    """転載先のタイトル

    検索されるのは「恵比寿 メンエス」のような 駅＋ジャンル の組み合わせ。
    元のタイトルには駅名しか入っていないので足す。ただし先頭に置くと
    どの記事も同じ書き出しになって一覧で読みにくいので、末尾にまわす。
    検索側は語がタイトルに含まれていればよく、位置は問わない。
    """
    t = clean_title(article.get("title"))
    station = (article.get("station") or article.get("area") or "").strip()
    if not station:
        return t
    # 元タイトルの【恵比寿】は残す（本文の見出しとして機能するため）
    tag = f"#{station}メンエス"
    return t if tag in t else f"{t} {tag}"


def build_lead(article):
    """本文の最初に置く1文

    livedoor は記事の書き出しから meta description を作る。
    先頭が出勤日ブロックだと日付の羅列が説明文になってしまうので、
    何の記事かが分かる1文を先に置く。
    """
    station = (article.get("station") or article.get("area") or "都内").strip()
    cup = next((t for t in (article.get("tags") or [])
                if t.endswith("カップ")), "")
    who = f"{cup}の" if cup else ""
    return (f'<p>{escape(station)}のメンズエステ体験レポートです。'
            f'{escape(who)}セラピストに実際に行ってきた記録を、'
            f'施術の流れに沿って書いています。</p>')


# livedoor の記事カテゴリは2枠まで。何を入れるかで回遊の効きが変わる
CATEGORY_SLOTS = 2


def play_tags(article):
    """その記事のプレイ系タグ（カップ数と駅名を除いたもの）"""
    station = (article.get("station") or "").strip()
    return [t for t in (article.get("tags") or [])
            if not t.endswith("カップ") and t != station]


def play_frequency(articles):
    """プレイ系タグが全体で何件あるかを数える"""
    freq = {}
    for a in articles:
        for t in play_tags(a):
            freq[t] = freq.get(t, 0) + 1
    return freq


def build_categories(article, freq=None):
    """livedoor 側の記事カテゴリを決める

    livedoor は1記事あたり2つまでなので「駅」と「プレイ内容」を入れる。
    エリア（東京都内など）はエリア別まとめ記事が受け持つので枠を使わない。

    プレイ系タグが複数ある記事（135件中11件）は、全体での件数が多い方を選ぶ。
    OPI や CKB のように1〜2件しかないタグでカテゴリを作っても、
    そのカテゴリを開いた人が他に読むものが無く、回遊に繋がらないため。
    """
    cats = []
    station = (article.get("station") or "").strip()
    if station:
        cats.append(station)
    plays = play_tags(article)
    if plays:
        if freq:
            plays = sorted(plays, key=lambda t: (-freq.get(t, 0), t))
        cats.append(plays[0])
    area = (article.get("area") or "").strip()
    if area and area not in cats:
        cats.append(area)              # 駅もタグも無いときの受け皿
    return cats[:CATEGORY_SLOTS]


def _buy_button(label, url, color):
    """購入ボタン

    ブログテーマの a{color} に負けて文字色が変わるので !important を付ける。
    """
    return (f'<a href="{escape(url)}" target="_blank" rel="noopener" '
            f'style="display:inline-block;margin:6px 8px;padding:12px 26px;'
            f'background:{color};color:#111 !important;font-weight:bold;'
            f'border-radius:6px;text-decoration:none !important;">'
            f'{escape(label)}</a>')


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


def build_ranking_banners(cfg):
    """ブログランキングのバナー

    にほんブログ村などは、自分のブログからバナーを踏まれた回数
    （INポイント）でランキングが決まる。全記事の末尾に置いておかないと
    点が入らないので、CTAの後ろに並べる。

    site_config.json に登録する:
      "livedoor": { "ranking_banners": ["<a href=...><img ...></a>", ...] }
    登録サイトから配布されるHTMLをそのまま貼ればよい。
    """
    banners = ((cfg.get("livedoor") or {}).get("ranking_banners") or [])
    banners = [b for b in banners if (b or "").strip()]
    if not banners:
        return ""
    return ('<div style="margin:24px 0;text-align:center;">'
            + "".join(banners) + '</div>')


def build_affiliate(cfg, article=None):
    """アフィリエイト枠

    2023年10月からのステマ規制（景品表示法）で、広告であることを
    明示しない表示は違反になる。アフィリエイトリンクは広告なので、
    必ず「広告」と分かる見出しを付けて、本文と切り離して出す。

    候補が複数あるときは記事ごとに1つだけ出す。全部並べても読まれないし、
    どの記事でも同じ広告が出続けるより、記事ごとに変わるほうが目に留まる。
    選び方は記事IDから決めるので、同じ記事なら毎回同じものが出る。

    ⚠️ 計測リンク（mfco.link など）をプログラムから取得してはいけない。
       クリックとして記録され、不正クリック扱いになる。画像や名前は
       設定に持たせて、リンクは貼るだけにする。

    site_config.json の書き方（どちらでも可）:
      "affiliate_links": [
        {"url": "https://...", "name": "表示名", "image": "https://..."},
        "<a href=...>生のHTML</a>"
      ]
    """
    lcfg = cfg.get("livedoor") or {}
    # 候補は残したまま、フラグひとつで出し入れできるようにしておく
    if not lcfg.get("affiliate_enabled"):
        return ""
    entries = []
    for e in (lcfg.get("affiliate_links") or []):
        if isinstance(e, str) and e.strip():
            entries.append({"html": e.strip()})
        elif isinstance(e, dict) and (e.get("url") or "").strip():
            entries.append(dict(e))
    if not entries:
        return ""

    # 記事ごとに1つ選ぶ
    if article is not None and len(entries) > 1:
        try:
            idx = int(str(article.get("id"))[-4:] or 0) % len(entries)
        except ValueError:
            idx = 0
        entries = [entries[idx]]

    lead = (lcfg.get("affiliate_lead") or "").strip()
    parts = []
    for e in entries:
        if e.get("html"):
            parts.append(e["html"])
            continue
        url = escape(e["url"])
        name = escape((e.get("name") or "MyFans").strip())
        img = (e.get("image") or "").strip()
        if img:
            parts.append(
                f'<a href="{url}" target="_blank" rel="noopener sponsored" '
                f'style="display:inline-block;text-decoration:none;">'
                f'<img src="{escape(img)}" alt="{name}" '
                f'style="max-width:220px;height:auto;border-radius:8px;" /><br>'
                f'<span style="font-size:14px;">{name}</span></a>')
        else:
            parts.append(
                f'<a href="{url}" target="_blank" rel="noopener sponsored" '
                f'style="display:inline-block;margin:4px 8px;padding:10px 22px;'
                f'border:1px solid #ccc;border-radius:6px;'
                f'text-decoration:none;font-size:14px;">{name}</a>')

    return ('<div style="margin:32px 0 8px;padding:16px;'
            'border-top:1px solid #ddd;text-align:center;">'
            '<p style="margin:0 0 10px;font-size:12px;color:#888;">広告</p>'
            + (f'<p style="margin:0 0 12px;font-size:14px;">{escape(lead)}</p>'
               if lead else "")
            + "".join(parts) + '</div>')


# ============================================================
# 記事末の関連記事（ブログ内の回遊）
# ============================================================
# カテゴリ新着から来た読者に2記事目を読んでもらうための枠。
# 転載がこの本数に届くまでは出さない。候補が数本しか無いうちに出しても、
# どの記事にも同じ数本が並ぶだけで回遊にならないため。
RELATED_MIN_POSTED = 10
RELATED_MAX = 6
# 同じ駅だけで埋めない。残りは別の駅から近い順に混ぜる
RELATED_SAME_STATION_MAX = 4


def thumb_for(article, state):
    """関連記事カードに出す見出し画像

    livedoor に上げ直した画像のサムネイル（-s.jpg）があればそれを使う。
    無ければワクストの元画像。どちらも https なので mixed content にならない。
    """
    src = (extract_thumbnail(article.get("free_html"))
           or article.get("image_url") or "")
    if not src:
        return ""
    rec = (state.get("_livedoor_images") or {}).get(src) or {}
    return rec.get("thumbnail") or rec.get("url") or src


def related_score(a, b):
    """b が a の関連記事としてどれだけ近いか

    同じ駅を最優先にする。「恵比寿の店を探している人」が次に読みたいのは
    同じ恵比寿の別レポートで、これが一番強い。次にプレイ内容、
    最後にカップ数。エリア（東京都内）だけの一致は弱いので点を絞る。
    """
    s = 0
    station = (a.get("station") or "").strip()
    if station and station == (b.get("station") or "").strip():
        s += 6
    elif (a.get("area") or "").strip() and a.get("area") == b.get("area"):
        s += 2
    s += 2 * len(set(play_tags(a)) & set(play_tags(b)))
    cup = next((t for t in (a.get("tags") or []) if t.endswith("カップ")), "")
    if cup and cup in (b.get("tags") or []):
        s += 1
    return s


def related_articles(article, articles, state):
    """記事末に並べる転載済み記事を選ぶ

    リンク先はブログ内の記事だけ。公開URLが取れていない記録は飛ばす
    （ワクストへ飛ばしてしまうと回遊にならず、CTAと役割が重なる）。
    """
    posted = state.get("_livedoor") or {}
    if len(posted) < RELATED_MIN_POSTED:
        return []
    me = str(article.get("id"))
    cands = []
    for a in articles:
        aid = str(a.get("id"))
        if aid == me:
            continue
        rec = posted.get(aid) or {}
        if not (rec.get("url") or "").strip():
            continue
        score = related_score(article, a)
        if score <= 0:
            continue
        cands.append((score, int(a.get("pv_total") or 0), aid, a, rec))
    # 近い順。同点なら読まれている記事を先に、それも同じならID順で毎回同じ並びに
    cands.sort(key=lambda x: (-x[0], -x[1], x[2]))
    # 新宿のように記事の多い駅だと6枚すべて同じ駅で埋まってしまう。
    # 何枚かは別の駅を混ぜて、読者が次に開く選択肢を残す
    same, other, out = [], [], []
    station = (article.get("station") or "").strip()
    for c in cands:
        (same if station and c[3].get("station") == station else other).append(c)
    out = same[:RELATED_SAME_STATION_MAX] + other
    out += [c for c in same[RELATED_SAME_STATION_MAX:] if len(out) < RELATED_MAX]
    return [(a, rec) for _, _, _, a, rec in out[:RELATED_MAX]]


def _related_card(a, rec, state):
    url = rec["url"]
    thumb = thumb_for(a, state)
    label = clean_title(a.get("title"))
    if len(label) > 44:
        label = label[:43] + "…"
    station = (a.get("station") or a.get("area") or "").strip()
    img = (f'<img src="{escape(thumb)}" alt="" loading="lazy" '
           f'style="width:100%;height:112px;object-fit:cover;display:block;'
           f'border-radius:6px;" />' if thumb else
           '<span style="display:block;height:112px;background:#eee;'
           'border-radius:6px;"></span>')
    head = (f'<span style="display:block;font-size:11px;color:#c71585;'
            f'margin:6px 0 2px;">{escape(station)}</span>' if station else "")
    # テーマ側の a{color} に負けるので !important で戻す
    return (f'<a href="{escape(url)}" style="display:block;'
            f'flex:1 1 calc(50% - 6px);min-width:130px;max-width:calc(50% - 6px);'
            f'text-decoration:none !important;color:inherit !important;">'
            + img + head
            + f'<span style="display:block;font-size:13px;line-height:1.45;">'
              f'{escape(label)}</span></a>')


def _hub_links(article, state):
    """エリア別まとめ・人気ランキングへの導線

    関連記事6本で足りなかった読者の受け皿。一覧に落として、
    そこからさらに読んでもらう。
    """
    links = []
    area = (article.get("area") or "").strip()
    hub = ((state.get("_livedoor_area") or {}).get(area) or {})
    if hub.get("url"):
        links.append((f"【{area}】の体験談まとめ", hub["url"]))
    ranks = state.get("_livedoor_ranking") or {}
    plays = play_tags(article)
    for kind in (plays[:1] + ["総合"]):
        rec = ranks.get(kind) or {}
        if not rec.get("url"):
            continue
        label = "人気ランキング TOP20" if kind == "総合" else f"【{kind}】人気ランキング"
        links.append((label, rec["url"]))
    if not links:
        return ""
    body = " ／ ".join(f'<a href="{escape(u)}">{escape(t)}</a>' for t, u in links)
    return f'<p style="margin:14px 0 0;font-size:14px;">▶ {body}</p>'


def build_related(article, articles, state):
    """記事末に置く関連記事ブロック（見出し画像つき）"""
    items = related_articles(article, articles, state)
    if not items:
        return ""
    cards = "".join(_related_card(a, rec, state) for a, rec in items)
    return (
        '<div style="margin:32px 0 12px;padding:0 0 6px;'
        'border-bottom:2px solid #c71585;font-size:16px;font-weight:bold;">'
        'こちらの体験談も読まれています</div>'
        '<div style="display:flex;flex-wrap:wrap;gap:18px 12px;">'
        + cards + '</div>' + _hub_links(article, state))


def related_signature(article, articles, state):
    """関連記事の並びが変わったかを見るための指紋

    転載が増えると各記事の関連記事も入れ替わる。更新すべきかの判定に使う。
    """
    return ",".join(str(a.get("id"))
                    for a, _ in related_articles(article, articles, state))


def ensure_hosted_image(client, article, state):
    """記事の見出し画像を livedoor 側に載せ替える

    ワクストから直リンクすると、リファラで弾かれたり、向こうの記事を
    消したときに画像も消える。一度アップロードして livedoor の URL を
    控えておき、以降はそれを使う。

    戻り値: {"id": 画像ID, "url": livedoorのURL} / 失敗時は None
    """
    src = extract_thumbnail(article.get("free_html"))
    if not src:
        return None
    cache = state.setdefault("_livedoor_images", {})
    if src in cache and (cache[src] or {}).get("url"):
        return cache[src]
    try:
        r = requests.get(src, timeout=60)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"    ⚠️ 元画像の取得に失敗: {e}")
        return None
    try:
        xml = client.upload_image(r.content, src.rsplit("/", 1)[-1],
                                  r.headers.get("Content-Type", "image/jpeg"))
    except LivedoorError as e:
        log.warning(f"    ⚠️ 画像アップロードに失敗: {e}")
        return None
    info = parse_image_response(xml)
    if not info.get("url"):
        return None
    cache[src] = info
    log.info(f"    🖼️  画像をアップロード id={info['id']} {info['url']}")
    return info


def build_body(article, cfg, image=None, related=""):
    """投稿本文を組み立てる（出勤日＋無料部分＋購入導線＋関連記事＋免責）

    related は build_related() の出力。購入導線より後ろに置く。
    先に置くとブログ内を回るだけで購入ページに辿り着かなくなる。
    """
    free = clean_for_livedoor(article.get("free_html"))
    if not free:
        return ""
    thumb = (image or {}).get("url") or extract_thumbnail(article.get("free_html"))
    img = (f'<p><img src="{escape(thumb)}" alt="{escape(clean_title(article.get("title"))[:60])}" '
           f'style="max-width:100%;height:auto;" /></p>' if thumb else "")
    parts = [img, build_lead(article), build_shift_block(article), free,
             build_cta(article, cfg), related, build_ranking_banners(cfg),
             f'<p style="color:#888;font-size:12px;">{DISCLAIMER}</p>',
             build_affiliate(cfg, article)]
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
            if _is_gone(e):
                # 記事が消えているので作り直す
                hubs.pop(area, None)
                save_state(state)
                print(f"🗑️  エリアまとめが見つからないので作り直します")
                ng += 1
                continue
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
# 人気ランキング記事
# ============================================================
# ランキングを作るのに最低限必要な転載済み記事数
RANKING_MIN_ITEMS = 5
RANKING_TOP_N = 20
# プレイ別ランキングを作るタグ（全体で5件以上あるもの）
RANKING_MIN_TAG_ITEMS = 5


def build_ranking(kind, articles, cfg, state):
    """人気ランキング記事を組み立てる

    順位はワクストでの購入数。実数は出さず順番だけ載せる
    （こちらの売上規模をそのまま公開する必要はないため）。
    対象は転載済みの記事だけ。未転載を混ぜるとブログ外へ出てしまう。
    """
    posted = state.get("_livedoor") or {}
    items = [a for a in articles if str(a.get("id")) in posted]
    if kind != "総合":
        items = [a for a in items if kind in play_tags(a)]
    items = [a for a in items if int(a.get("sales_count") or 0) > 0]
    if len(items) < RANKING_MIN_ITEMS:
        return None
    items.sort(key=lambda a: (-int(a.get("sales_count") or 0), str(a.get("id"))))
    items = items[:RANKING_TOP_N]

    rows = []
    for i, a in enumerate(items, 1):
        cup = next((t for t in (a.get("tags") or []) if t.endswith("カップ")), "")
        plays = " / ".join(play_tags(a)[:2])
        label = " / ".join(x for x in [a.get("station") or "", cup, plays] if x)
        url = (posted.get(str(a["id"])) or {}).get("url") or a.get("source_url")
        medal = "🥇🥈🥉"[i - 1] if i <= 3 else f"{i}."
        rows.append(f'<li style="margin-bottom:8px;">{medal} '
                    + (f_link(label, url) if url else escape(label)) + '</li>')

    label = "" if kind == "総合" else f"【{kind}】"
    title = (f"{label}メンズエステ体験レポート 人気ランキング TOP{len(items)}"
             if kind != "総合" else
             f"【保存版】メンズエステ体験レポート 人気ランキング TOP{len(items)}")
    body = (
        f'<p>これまでに書いた体験レポートを、購入数の多い順に並べました。'
        f'{"" if kind == "総合" else escape(kind) + "のレポートに絞っています。"}'
        f'どれを読むか迷ったら、上から順にどうぞ。</p>'
        '<ol style="padding-left:1.2em;">' + "".join(rows) + '</ol>'
        '<p style="margin-top:24px;font-size:13px;color:#666;">'
        '新しいレポートを書くたびに順位を入れ替えています。</p>'
        f'<p style="color:#888;font-size:12px;">{DISCLAIMER}</p>'
    )
    cats = ["ランキング"] + ([] if kind == "総合" else [kind])
    return {"title": title, "body": body, "categories": cats[:CATEGORY_SLOTS],
            "kind": kind, "ids": [str(a["id"]) for a in items]}


def ranking_kinds(articles):
    """作るランキングの種類。総合＋件数の多いプレイ別"""
    freq = play_frequency(articles)
    tags = [t for t, n in sorted(freq.items(), key=lambda kv: -kv[1])
            if n >= RANKING_MIN_TAG_ITEMS]
    return ["総合"] + tags


def run_ranking(client, articles, cfg, state, draft=False):
    """ランキング記事を作る／更新する（順位が変わったときだけ）"""
    store = state.setdefault("_livedoor_ranking", {})
    ok = ng = 0
    for kind in ranking_kinds(articles):
        m = build_ranking(kind, articles, cfg, state)
        if not m:
            continue
        rec = store.get(kind) or {}
        if rec.get("ids") == m["ids"]:
            continue                   # 順位が変わっていないので触らない
        try:
            if rec.get("edit_url"):
                url = client.update(rec["edit_url"], m["title"], m["body"],
                                    m["categories"], draft)
                edit_url, verb = rec["edit_url"], "更新"
            else:
                url, edit_url = client.post(m["title"], m["body"],
                                            m["categories"], draft)
                verb = "新規"
        except LivedoorError as e:
            if _is_gone(e):
                # 記事が消えているので作り直す
                store.pop(kind, None)
                save_state(state)
                print(f"🗑️  ランキングが見つからないので作り直します")
                ng += 1
                continue
            print(f"❌ ランキング{verb}失敗 [{kind}]: {e}")
            print(f"::error::ランキング記事の投稿に失敗しました [{kind}]: {e}")
            ng += 1
            continue
        store[kind] = {"url": url or rec.get("url", ""), "edit_url": edit_url,
                       "ids": m["ids"],
                       "at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")}
        save_state(state)
        ok += 1
        print(f"✅ ランキング{verb} [{kind}] {len(m['ids'])}件 "
              f"{store[kind]['url'] or '(dry-run)'}")
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


def posted_today(state):
    """今日すでに転載した本数（まとめ記事は数えない）"""
    day = today_iso()
    return sum(1 for r in (state.get("_livedoor") or {}).values()
               if (r.get("at") or "").startswith(day))


def run_publish(client, articles, cfg, state, limit, draft=False, upto=None):
    """未転載の記事を新規投稿する

    upto を渡すと「今日の合計がその本数になるまで」出す。
    枠ごとに1本ずつ出しつつ、前の枠が失敗した日でも遅れを取り戻せる。
    upto が無ければ limit 件をそのまま出す。
    """
    if upto is not None:
        done = posted_today(state)
        limit = max(0, upto - done)
        print(f"本日の転載 {done}件 / この枠の目標 {upto}件 → 今回 {limit}件")
        if limit <= 0:
            return 0, 0
    targets = pick_targets(articles, limit, state)
    if not targets:
        return 0, 0
    freq = play_frequency(articles)
    posted = state.setdefault("_livedoor", {})
    ok = ng = 0
    for a in targets:
        title = build_title(a)
        image = ensure_hosted_image(client, a, state)
        rel_sig = related_signature(a, articles, state)
        body = build_body(a, cfg, image, build_related(a, articles, state))
        if not body:
            continue
        try:
            url, edit_url = client.post(title, body, build_categories(a, freq), draft)
        except LivedoorError as e:
            print(f"❌ 投稿失敗 [{a['id']}]: {e}")
            print(f"::error::livedoorへの投稿に失敗しました [{a['id']}]: {e}")
            ng += 1
            continue
        posted[str(a["id"])] = {
            "at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            "url": url, "edit_url": edit_url,
            "image_id": (image or {}).get("id", ""),
            "title": title,
            # 記事末に並べた関連記事。顔ぶれが変わったら refresh で貼り替える
            "rel_sig": rel_sig,
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
    freq = play_frequency(articles)
    by_id = {str(a.get("id")): a for a in articles}
    todo = []
    for aid, rec in posted.items():
        a = by_id.get(aid)
        if not a or not rec.get("edit_url"):
            continue
        # 出勤日が変わったとき、またはタイトルの作り方を変えたときに更新する
        urgent = (list(a.get("shift_dates") or []) != list(rec.get("shifts") or [])
                  or build_title(a) != (rec.get("title") or ""))
        # 転載が増えると関連記事の顔ぶれも変わる。古い記事の記事末が
        # いつまでも初期の6本のままにならないよう、こちらも更新対象にする。
        sig = related_signature(a, articles, state)
        stale = sig != (rec.get("rel_sig") or "")
        if not (urgent or stale):
            continue
        # 出勤日のずれを先に直す。関連記事の入れ替えは後回しでよく、
        # その中では長く放置されているものから順に回す
        todo.append((0 if urgent else 1, rec.get("refreshed_at") or "",
                     aid, rec, a, sig))
    if not todo:
        return 0, 0
    todo.sort(key=lambda x: (x[0], x[1]))
    ok = ng = 0
    for _, _, aid, rec, a, sig in todo[:max(1, limit)]:
        body = build_body(a, cfg, (state.get("_livedoor_images") or {}).get(
            extract_thumbnail(a.get("free_html")) or ""),
            build_related(a, articles, state))
        if not body:
            continue
        try:
            client.update(rec["edit_url"], build_title(a), body,
                          build_categories(a, freq), draft)
        except LivedoorError as e:
            if _is_gone(e):
                # ブログ側で削除された記事。記録を消して、次回また転載する
                print(f"🗑️  [{aid}] 記事が見つからないので記録を削除します")
                posted.pop(aid, None)
                save_state(state)
                continue
            print(f"❌ 更新失敗 [{aid}]: {e}")
            print(f"::error::livedoorの記事更新に失敗しました [{aid}]: {e}")
            ng += 1
            continue
        rec["shifts"] = list(a.get("shift_dates") or [])
        rec["title"] = build_title(a)
        rec["rel_sig"] = sig
        rec["refreshed_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        ok += 1
        print(f"🔄 更新 [{aid}] {' '.join(fmt_date(d) for d in upcoming_shifts(a)[:3])}")
    return ok, ng


# 本日出勤まとめを始める記事数。
# 転載記事が少ないうちに出しても、リンク先の大半がブログ外になって
# まとめとして成立しない。まず個別記事で更新実績を積む
MATOME_MIN_POSTED = 30


def run_fix_images(client, articles, cfg, state, limit=20):
    """投稿済みなのに画像IDを持っていない記事を埋める

    画像アップロード機能より前に投稿した記事や、アップロードに
    失敗した記事は image_id が無く、見出し画像を設定できない。
    後から画像だけ上げてIDを控える。本文の画像URLも差し替えたいので
    記事本体も更新する。
    """
    posted = state.get("_livedoor") or {}
    by_id = {str(a.get("id")): a for a in articles}
    todo = [(aid, rec) for aid, rec in posted.items()
            if not (rec.get("image_id") or "").strip()
            and by_id.get(aid) and rec.get("edit_url")]
    if not todo:
        print("画像IDが未設定の記事はありません")
        return 0, 0
    print(f"画像IDが未設定: {len(todo)}件")
    ok = ng = 0
    for aid, rec in todo[:max(1, limit)]:
        a = by_id[aid]
        image = ensure_hosted_image(client, a, state)
        if not image:
            print(f"  ⏭️  [{aid}] 画像が取得できませんでした")
            ng += 1
            continue
        rec["image_id"] = image.get("id", "")
        # 本文の画像もlivedoor側のURLに差し替える
        try:
            client.update(rec["edit_url"], build_title(a),
                          build_body(a, cfg, image,
                                     build_related(a, articles, state)),
                          build_categories(a), False)
        except LivedoorError as e:
            print(f"  ⚠️ [{aid}] 本文の更新に失敗: {e}")
        rec["title"] = build_title(a)
        save_state(state)
        ok += 1
        print(f"  ✅ [{aid}] image_id={rec['image_id']}")
    return ok, ng


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
    ap.add_argument("--upto", type=int, metavar="N",
                    help="今日の転載が合計N件になるまで出す（枠ごとの遅れを取り戻す）")
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
    ap.add_argument("--ranking", action="store_true",
                    help="人気ランキング記事を作る／更新する")
    ap.add_argument("--fix-images", action="store_true",
                    help="投稿済みで画像IDが無い記事に画像を上げ直す")
    ap.add_argument("--reset", action="store_true",
                    help="転載履歴を消す（ブログ側で記事を削除したとき）")
    ap.add_argument("--check", action="store_true",
                    help="投稿せずに認証だけ確認する")
    ap.add_argument("--upload-test", action="store_true",
                    help="画像を1枚アップロードして、返るXMLをそのまま表示する")
    ap.add_argument("--inspect", metavar="記事ID",
                    help="投稿済み記事のXMLを表示する（見出し画像の項目を探す用）")
    args = ap.parse_args()

    if args.reset:
        # ブログ側で記事を消したときに使う。アップロード済み画像の記録は
        # 残す（画像はブログに残っているので、上げ直す必要がない）
        st = load_state()
        removed = {k: len(st.get(k) or {}) for k in
                   ("_livedoor", "_livedoor_area", "_livedoor_ranking",
                    "_livedoor_matome") if st.get(k)}
        if not removed:
            print("消す記録がありません")
            return 0
        for k in list(removed):
            st.pop(k, None)
        save_state(st)
        print("転載履歴を消しました:")
        for k, n in removed.items():
            print(f"  {k}: {n}件")
        print(f"  （画像の記録 {len(st.get('_livedoor_images') or {})}件 は残しています）")
        return 0

    if args.check:
        return LivedoorClient().check()

    if args.inspect:
        # 見出し画像を管理画面で設定した記事のXMLを見て、
        # AtomPubにその項目が現れるかを確かめる。
        # 現れればその要素名で送れる＝完全自動化できる
        client = LivedoorClient()
        if client.dry_run:
            print("❌ 認証情報が足りません")
            return 1
        target = args.inspect
        if target.isdigit():
            target = f"{ATOM_BASE}/{client.blog_name}/article/{target}"
        print(f"取得: {target}\n")
        try:
            xml = client.fetch(target)
        except LivedoorError as e:
            print(f"❌ {e}")
            return 1
        # 本文は長いので落として、構造だけ見る
        xml = re.sub(r"(<content[^>]*>).*?(</content>)", r"\1…本文省略…\2",
                     xml, flags=re.S)
        print(xml[:3000])
        return 0

    if args.upload_test:
        # 見出し画像を自動設定できるかを判断するための診断。
        # 記事1件ぶんの画像をアップロードして、返ってきたXMLをそのまま出す
        arts = [a for a in load_articles() if extract_thumbnail(a.get("free_html"))]
        if not arts:
            print("画像を持つ記事がありません")
            return 1
        src = extract_thumbnail(arts[0]["free_html"])
        print(f"元画像: {src}")
        try:
            r = requests.get(src, timeout=60)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"❌ 元画像の取得に失敗: {e}")
            return 1
        print(f"取得 {len(r.content)}バイト  {r.headers.get('Content-Type')}")
        client = LivedoorClient()
        if client.dry_run:
            print("❌ 認証情報が足りません")
            return 1
        try:
            xml = client.upload_image(r.content, src.rsplit("/", 1)[-1],
                                      r.headers.get("Content-Type", "image/jpeg"))
        except LivedoorError as e:
            print(f"❌ {e}")
            return 1
        print("\n--- レスポンス（この中に見出し画像に使えるIDがあるか見る）---")
        print(xml[:2000])
        return 0

    cfg = load_config()
    articles = load_articles()
    if not articles:
        print(f"記事データがありません（{ARTICLES_DIR}/ が空）")
        return 0
    state = load_state()

    # --- 表示のみ（--post なし）---
    if not args.post:
        if args.ranking:
            shown = 0
            for kind in ranking_kinds(articles):
                m = build_ranking(kind, articles, cfg, state)
                if not m:
                    continue
                print("=" * 60)
                print(f"タイトル : {m['title']}")
                print(f"カテゴリ : {' / '.join(m['categories'])}")
                print("-" * 60)
                print(re.sub(r"<[^>]+>", "", m["body"])[:600])
                print()
                shown += 1
            if not shown:
                print(f"ランキングの対象がありません"
                      f"（転載済み {len(state.get('_livedoor') or {})}件 / "
                      f"{RANKING_MIN_ITEMS}件から作成）")
            return 0
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
            # --upto は「今日の合計がN件になるまで」なので残り本数を出す
            n = args.limit
            if args.upto is not None:
                n = max(0, args.upto - posted_today(state))
                print(f"本日の転載 {posted_today(state)}件 / 目標 {args.upto}件 "
                      f"→ 今回 {n}件")
                if n <= 0:
                    return 0
            targets = pick_targets(articles, n, state)
        if not targets:
            empty = sum(1 for a in articles if not (a.get("free_html") or "").strip())
            print("転載できる記事がありません。")
            if empty:
                print(f"  無料部分が未取得の記事が {empty}件あります。")
                print("  CODOC_MODE=free_backfill python wakust_auto_update.py "
                      "で取り込んでください。")
            return 0
        for a in targets:
            body = build_body(a, cfg, related=build_related(a, articles, state))
            print("=" * 60)
            print(f"タイトル : {build_title(a)}")
            print(f"カテゴリ : {' / '.join(build_categories(a, play_frequency(articles)))}")
            print(f"本文     : {len(body)}文字")
            print("-" * 60)
            print(body[:1500])
            print()
        return 0

    # --- 実投稿 ---
    client = LivedoorClient()
    ng = 0
    if args.fix_images:
        # 画像の貼り直しだけを行う。ついでに新規投稿してしまわないよう
        # ここで終える（新規投稿は --fix-images を付けずに実行する）
        _, n = run_fix_images(client, articles, cfg, state)
        return 1 if n else 0
    if args.refresh:
        _, n = run_refresh(client, articles, cfg, state, args.refresh, args.draft)
        ng += n
    if args.area_matome:
        _, n = run_area_matome(client, articles, cfg, state, args.draft)
        ng += n
    if args.ranking:
        _, n = run_ranking(client, articles, cfg, state, args.draft)
        ng += n
    if args.matome:
        _, n = run_matome(client, articles, cfg, state, args.draft)
        ng += n
    if (args.limit or args.upto) and not args.id:
        _, n = run_publish(client, articles, cfg, state, args.limit, args.draft,
                           upto=args.upto)
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
