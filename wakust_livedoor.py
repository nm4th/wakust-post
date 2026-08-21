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

    def post(self, title, body_html, categories=None, draft=False):
        """記事を1件投稿して、公開URLを返す"""
        entry = _build_atom_entry(title, body_html, categories or [], draft)
        if self.dry_run:
            log.info(f"🧪 [dry-run] 投稿: {title}")
            return None
        url = f"{ATOM_BASE}/{self.blog_name}/article"
        try:
            r = requests.post(
                url, data=entry.encode("utf-8"), timeout=60,
                headers={"X-WSSE": _wsse_header(self.user_id, self.api_key),
                         "Content-Type": "application/atom+xml; charset=utf-8"})
        except requests.RequestException as e:
            raise LivedoorError(f"リクエスト失敗: {e}") from e
        if r.status_code == 401:
            raise LivedoorError(
                "認証に失敗しました。LIVEDOOR_USER_ID（livedoor ID）と "
                "LIVEDOOR_API_KEY（AtomPub用パスワード。ログインパスワードとは別）"
                "を確認してください")
        if r.status_code not in (200, 201):
            raise LivedoorError(f"HTTP {r.status_code}: {r.text[:300]}")
        m = re.search(r'<link[^>]+rel="alternate"[^>]+href="([^"]+)"', r.text)
        return m.group(1) if m else ""


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


def build_body(article, cfg):
    """投稿本文を組み立てる（無料部分＋出勤日＋購入導線＋免責）"""
    free = (article.get("free_html") or "").strip()
    if not free:
        return ""

    parts = [free]

    dates = article.get("shift_dates") or []
    if dates:
        # 出勤日は「記事を書いた時点の予定」でしかないので、そう明示する
        labels = []
        for d in dates:
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
                labels.append(f"{dt.month}/{dt.day}")
            except ValueError:
                continue
        if labels:
            parts.append(
                f'<p style="margin-top:24px;color:#666;font-size:13px;">'
                f'記事作成時点の出勤日: {" ・ ".join(labels)}</p>')

    parts.append(build_cta(article, cfg))
    parts.append(f'<p style="color:#888;font-size:12px;">{DISCLAIMER}</p>')
    return "\n".join(p for p in parts if p)


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


def main():
    ap = argparse.ArgumentParser(description="livedoor Blog へ記事を転載する")
    ap.add_argument("--limit", type=int, default=1, help="処理する記事数")
    ap.add_argument("--id", help="記事IDを指定して1件だけ処理する")
    ap.add_argument("--post", action="store_true",
                    help="実際に投稿する（認証情報が無ければdry-run）")
    ap.add_argument("--draft", action="store_true", help="下書きとして投稿する")
    args = ap.parse_args()

    cfg = load_config()
    articles = load_articles()
    if not articles:
        print(f"記事データがありません（{ARTICLES_DIR}/ が空）")
        return 0

    state = load_state()
    if args.id:
        targets = [a for a in articles if str(a.get("id")) == str(args.id)]
        if not targets:
            print(f"記事 {args.id} が見つかりません")
            return 1
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

    client = LivedoorClient()
    posted = state.setdefault("_livedoor", {})
    failed = 0

    for a in targets:
        title = clean_title(a.get("title"))
        body = build_body(a, cfg)
        cats = build_categories(a)
        if not body:
            print(f"⏭️  [{a['id']}] 無料部分が空のためスキップ")
            continue

        if not args.post:
            print("=" * 60)
            print(f"タイトル : {title}")
            print(f"カテゴリ : {' / '.join(cats)}")
            print(f"本文     : {len(body)}文字")
            print("-" * 60)
            print(body[:1200])
            print()
            continue

        try:
            url = client.post(title, body, cats, draft=args.draft)
        except LivedoorError as e:
            print(f"❌ 投稿失敗 [{a['id']}]: {e}")
            print(f"::error::livedoorへの投稿に失敗しました [{a['id']}]: {e}")
            failed += 1
            continue
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        posted[str(a["id"])] = {"at": now, "url": url or ""}
        save_state(state)
        print(f"✅ 投稿しました [{a['id']}] {url or '(dry-run)'}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
