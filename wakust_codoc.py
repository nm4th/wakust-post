"""codoc.jp 連携モジュール
- ログイン、記事作成、記事更新
"""

import re
import logging
import requests
from bs4 import BeautifulSoup

log = logging.getLogger("wakust")

CODOC_BASE = "https://codoc.jp"
CODOC_LOGIN_URL = f"{CODOC_BASE}/login"
CODOC_CREATE_FORM_URL = f"{CODOC_BASE}/me/entries/create"
CODOC_CREATE_POST_URL = f"{CODOC_BASE}/me/entries"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _get_token(session, url):
    """指定ページから _token (CSRF) を取得"""
    try:
        r = session.get(url, timeout=30)
        if r.status_code != 200:
            log.warning(f"    ⚠️ codoc: token取得失敗 (HTTP {r.status_code}) URL={url}")
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        tok_input = soup.find("input", {"name": "_token"})
        if tok_input and tok_input.get("value"):
            return tok_input["value"]
        log.warning(f"    ⚠️ codoc: _tokenが見つからない URL={url}")
        return None
    except requests.RequestException as e:
        log.warning(f"    ⚠️ codoc: token取得例外: {e}")
        return None


def _build_session():
    """codoc用のrequests sessionを構築（ヘッダー設定済み）"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
        "Upgrade-Insecure-Requests": "1",
    })
    return session


def _verify_session(session):
    """/me/entries/create に GET して認証済みか判定。認証済みならTrue"""
    try:
        chk = session.get(CODOC_CREATE_FORM_URL, timeout=30, allow_redirects=False)
    except requests.RequestException as e:
        log.warning(f"    ⚠️ codoc: 認証確認エラー: {e}")
        return False
    if chk.status_code == 200:
        return True
    if chk.status_code in (301, 302):
        loc = chk.headers.get("location", "")
        # loginやtwo_factorにリダイレクトされたら未認証
        if "login" in loc.lower() or "two_factor" in loc.lower():
            return False
        return True
    return False


def codoc_login_via_cookie(cookie_str):
    """Cookie文字列からsessionを構築。認証OKならsession返却、失敗時None
    cookie_str形式: "XSRF-TOKEN=xxx; codoc_session=yyy; ..."
    """
    session = _build_session()
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue
        try:
            session.cookies.set(name, value, domain="codoc.jp")
        except Exception as e:
            log.warning(f"    ⚠️ codoc: Cookie設定失敗 [{name}]: {e}")
    log.info(f"    🔧 codoc: 注入Cookie={list(session.cookies.keys())}")
    if _verify_session(session):
        log.info("✅ codocログイン成功 (Cookie注入)")
        return session
    log.warning("⚠️ codoc: Cookie無効/期限切れの可能性")
    session.close()
    return None


def codoc_login(email, password):
    """codocにログインしてsessionを返す。失敗時Noneを返す
    2FA有効の場合は失敗する。その場合はcodoc_login_via_cookieを使うこと。
    """
    session = _build_session()
    # 1. GET /login でCSRFトークン+セッションCookie取得
    token = _get_token(session, CODOC_LOGIN_URL)
    if not token:
        log.error("❌ codoc: ログインフォームトークン取得失敗")
        return None
    log.info(f"    🔧 codoc: token取得 cookies={list(session.cookies.keys())}")
    # 2. POST /login (リダイレクト無しで結果を確認)
    try:
        r = session.post(CODOC_LOGIN_URL, data={
            "_token": token,
            "email": email,
            "password": password,
            "remember": "1",
        }, headers={
            "Referer": CODOC_LOGIN_URL,
            "Origin": CODOC_BASE,
        }, timeout=30, allow_redirects=False)
    except requests.RequestException as e:
        log.error(f"❌ codoc: ログインリクエスト失敗: {e}")
        return None
    log.info(f"    🔧 codoc: login POST → HTTP {r.status_code} "
             f"location={r.headers.get('location', '')!r} "
             f"cookies={list(session.cookies.keys())}")
    # 302で /login 以外にリダイレクト → 成功
    if r.status_code in (301, 302):
        loc = r.headers.get("location", "")
        # 2FAページへのリダイレクトは「成功」ではない。ここを通すと
        # 未認証セッションのまま処理が進み、後段で不可解に失敗する
        if "two_factor" in loc.lower():
            log.error(f"❌ codoc: 2要素認証が要求されました ({loc})。"
                      f"CODOC_COOKIE を再取得してください")
            return None
        if loc and "/login" not in loc:
            log.info(f"✅ codocログイン成功 (login POSTのredirect先: {loc})")
            return session
        # /login にリダイレクト = 認証失敗
        log.error(f"❌ codoc: ログイン失敗（/loginにリダイレクト back） location={loc}")
        # レスポンス本文からエラーメッセージを取得
        try:
            follow = session.get(loc if loc.startswith("http") else CODOC_BASE + loc,
                                 timeout=15)
            body = follow.text
            m = re.search(r'<span[^>]*(?:invalid-feedback|error)[^>]*>([^<]+)</span>',
                          body, re.I)
            if m:
                log.error(f"    🔧 エラーメッセージ: {m.group(1).strip()!r}")
        except Exception:
            pass
        return None
    # 200が返る場合、フォームが再描画されている可能性 → エラー
    if r.status_code == 200:
        snippet = r.text[:800].replace("\n", " ")
        log.error(f"❌ codoc: ログイン失敗 (200再描画) body先頭800字: {snippet!r}")
        return None
    log.error(f"❌ codoc: 予期しないHTTPコード {r.status_code}")
    return None


def _entry_form_data(title, body_free, body_paywalled, price, binded_url,
                     token, method=None, limited="0", seo_paywall="0"):
    """codoc entryフォームのデータを構築

    limited="1" にすると codoc 上では限定公開（一覧に出さない）になる。
    自社サイトのみで販売し、codoc.jp を決済レイヤーとしてだけ使う場合に指定する。
    """
    data = {
        "_token": token,
        "title": title,
        "image_id": "",
        "image_url": "",
        "body_free": body_free or "",
        "body_paywalled": body_paywalled or "",
        "show_price": "1",
        "price": str(price),
        "show_paywalled_support": "0",
        "limited": str(limited),
        "affiliate_mode": "0",
        "show_support": "1",
        "binded_url": binded_url or "",
        "seo_paywall": str(seo_paywall),
        "status": "1",  # 公開（ペイウォールを動作させるには公開が必要）
        "preview": "",
    }
    if method:
        data["_method"] = method
    return data


def codoc_create_entry(session, title, body_free, body_paywalled, price,
                       binded_url="", limited="0"):
    """codocに新規記事作成 → entry_id(str)を返す。失敗時None"""
    token = _get_token(session, CODOC_CREATE_FORM_URL)
    if not token:
        return None
    # XSRF-TOKEN cookieがあればX-XSRF-TOKENヘッダーにセット(Laravel対応)
    extra_headers = {
        "Referer": CODOC_CREATE_FORM_URL,
        "Origin": CODOC_BASE,
    }
    xsrf = session.cookies.get("XSRF-TOKEN", domain="codoc.jp")
    if xsrf:
        from urllib.parse import unquote
        extra_headers["X-XSRF-TOKEN"] = unquote(xsrf)
    data = _entry_form_data(title, body_free, body_paywalled, price,
                             binded_url, token, limited=limited)
    try:
        r = session.post(CODOC_CREATE_POST_URL, data=data,
                         headers=extra_headers,
                         timeout=60, allow_redirects=True)
    except requests.RequestException as e:
        log.error(f"❌ codoc: 記事作成リクエスト失敗: {e}")
        return None
    if r.status_code not in (200, 302):
        log.error(f"❌ codoc: 記事作成失敗 (HTTP {r.status_code})")
        log.warning(f"    🔧 body先頭800字: {r.text[:800]!r}")
        return None
    # 応答URLから entry_id を抽出
    # 通常は /me/entries/{id}/edit にリダイレクトされる
    m = re.search(r"/me/entries/(\d+)", r.url or "")
    if m and m.group(1).isdigit():
        return m.group(1)
    # historyから探す
    for hist in getattr(r, "history", []):
        loc = hist.headers.get("location", "")
        m2 = re.search(r"/me/entries/(\d+)", loc)
        if m2 and m2.group(1).isdigit():
            return m2.group(1)
    # URLが/createに戻ってる = バリデーションエラー、本文からエラーメッセージ抽出
    log.warning(f"    ⚠️ codoc: 応答URLからentry_id抽出失敗 (URL={r.url}, "
                f"HTTP {r.status_code}, history={[h.status_code for h in r.history]})")
    # エラーメッセージ抽出
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        # Laravelのバリデーションエラー
        errors = []
        for cls in ("invalid-feedback", "error", "alert-danger", "error-message-body"):
            for el in soup.find_all(class_=re.compile(cls, re.I)):
                txt = el.get_text(" ", strip=True)
                if txt and len(txt) < 500:
                    errors.append(f"[{cls}] {txt}")
        if errors:
            log.error("    🔧 エラーメッセージ:")
            for e in errors[:10]:
                log.error(f"      {e}")
        # bodyの先頭も出力
        log.warning(f"    🔧 body先頭1500字: {r.text[:1500]!r}")
    except Exception as e:
        log.warning(f"    ⚠️ エラー解析失敗: {e}")
    return None


def codoc_update_entry(session, entry_id, title, body_free, body_paywalled,
                       price, binded_url="", limited="0"):
    """codocの既存記事を更新（_method=PUT）"""
    edit_url = f"{CODOC_BASE}/me/entries/{entry_id}/edit"
    submit_url = f"{CODOC_BASE}/me/entries/{entry_id}"
    token = _get_token(session, edit_url)
    if not token:
        return False
    data = _entry_form_data(title, body_free, body_paywalled, price,
                             binded_url, token, method="PUT", limited=limited)
    try:
        r = session.post(submit_url, data=data,
                         headers={"Referer": edit_url},
                         timeout=60, allow_redirects=True)
    except requests.RequestException as e:
        log.error(f"❌ codoc: 記事更新リクエスト失敗 [id={entry_id}]: {e}")
        return False
    if r.status_code not in (200, 302):
        log.error(f"❌ codoc: 記事更新失敗 [id={entry_id}] (HTTP {r.status_code})")
        log.warning(f"    🔧 body先頭500字: {r.text[:500]!r}")
        return False
    return True


# ============================================================
# 自社サイト埋め込み用: エントリーコード / ユーザーコードの取得
# ============================================================
# codocの貼り付けタグは2種類:
#   1) ページの<head>に1つ  <script src="https://codoc.jp/js/cms.js"
#                              data-usercode="xxxxxxxx" charset="UTF-8" defer></script>
#   2) ペイウォールを出す位置に
#      <div class="codoc-entries" id="codoc-entry-{エントリーコード}"></div>
# entry_id（/me/entries/{id} の数字）とエントリーコード（英数字の公開ID）は別物なので、
# 作成後に編集画面から公開IDを回収する必要がある。

# 数字だけのIDを公開コードと誤認しないよう、英字を1文字以上含むものだけ拾う
_CODE_CHARS = r"[A-Za-z0-9_\-]{6,32}"
_ENTRY_CODE_PATTERNS = [
    re.compile(r'codoc-entry-(' + _CODE_CHARS + r')'),
    re.compile(r'data-entry-code=["\'](' + _CODE_CHARS + r')["\']'),
    re.compile(r'/entries/(' + _CODE_CHARS + r')'),
]
_USERCODE_RE = re.compile(r'data-usercode=["\']([A-Za-z0-9_\-]{4,32})["\']')
# 公開URLは https://codoc.jp/sites/{サイトコード}/entries/{エントリーコード}
_SITECODE_RE = re.compile(r'/sites/([A-Za-z0-9_\-]{6,32})/')


def _pick_entry_code(text):
    """HTMLからエントリーコード（英字を含む英数字ID）を抜き出す"""
    for pat in _ENTRY_CODE_PATTERNS:
        for m in pat.finditer(text or ""):
            code = m.group(1)
            if code.isdigit():
                continue  # /entries/123 は内部ID、公開コードではない
            if code.lower() in ("create", "preview", "edit"):
                continue
            return code
    return None


def codoc_fetch_entry_code(session, entry_id):
    """entry_id から自社サイト埋め込み用のエントリーコードを取得。失敗時None"""
    for url in (f"{CODOC_BASE}/me/entries/{entry_id}/edit",
                f"{CODOC_BASE}/me/entries/{entry_id}"):
        try:
            r = session.get(url, timeout=30)
        except requests.RequestException as e:
            log.warning(f"    ⚠️ codoc: エントリーコード取得エラー {url}: {e}")
            continue
        if r.status_code != 200:
            log.warning(f"    ⚠️ codoc: エントリーコード取得 HTTP {r.status_code} "
                        f"URL={url}")
            continue
        code = _pick_entry_code(r.text)
        if code:
            log.info(f"    🔖 codoc: entry_code={code} (entry_id={entry_id})")
            return code
    log.error(f"❌ codoc: エントリーコードが見つかりません (entry_id={entry_id})。"
              f"codoc管理画面の「タグを貼り付け」から手動で確認してください")
    return None


def codoc_fetch_usercode(session):
    """codocのユーザーコード（scriptタグの data-usercode）を取得。失敗時None"""
    for url in (CODOC_CREATE_FORM_URL, f"{CODOC_BASE}/me/sites", f"{CODOC_BASE}/me"):
        try:
            r = session.get(url, timeout=30)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        m = _USERCODE_RE.search(r.text)
        if m:
            log.info(f"    🔖 codoc: usercode={m.group(1)}")
            return m.group(1)
        # 貼り付けタグが見つからない画面でも、公開URLからサイトコードを拾える
        m = _SITECODE_RE.search(r.text)
        if m:
            log.info(f"    🔖 codoc: usercode={m.group(1)} (公開URLから推定)")
            return m.group(1)
    log.warning("    ⚠️ codoc: usercodeを自動取得できませんでした。"
                "site_config.json の codoc.usercode に手動設定してください")
    return None
