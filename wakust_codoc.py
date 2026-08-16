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


def codoc_login(email, password):
    """codocにログインしてsessionを返す。失敗時Noneを返す"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
        "Upgrade-Insecure-Requests": "1",
    })
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
                     token, method=None):
    """codoc entryフォームのデータを構築"""
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
        "limited": "0",
        "affiliate_mode": "0",
        "show_support": "1",
        "binded_url": binded_url or "",
        "seo_paywall": "0",
        "status": "1",  # 公開
        "preview": "",
    }
    if method:
        data["_method"] = method
    return data


def codoc_create_entry(session, title, body_free, body_paywalled, price,
                       binded_url=""):
    """codocに新規記事作成 → entry_id(str)を返す。失敗時None"""
    token = _get_token(session, CODOC_CREATE_FORM_URL)
    if not token:
        return None
    data = _entry_form_data(title, body_free, body_paywalled, price,
                             binded_url, token)
    try:
        r = session.post(CODOC_CREATE_POST_URL, data=data,
                         headers={"Referer": CODOC_CREATE_FORM_URL},
                         timeout=60, allow_redirects=True)
    except requests.RequestException as e:
        log.error(f"❌ codoc: 記事作成リクエスト失敗: {e}")
        return None
    if r.status_code not in (200, 302):
        log.error(f"❌ codoc: 記事作成失敗 (HTTP {r.status_code})")
        log.warning(f"    🔧 body先頭500字: {r.text[:500]!r}")
        return None
    # 応答URLから entry_id を抽出
    # 通常は /me/entries/{id}/edit にリダイレクトされる
    m = re.search(r"/me/entries/(\d+)", r.url or "")
    if m:
        return m.group(1)
    # historyから探す
    for hist in getattr(r, "history", []):
        loc = hist.headers.get("location", "")
        m2 = re.search(r"/me/entries/(\d+)", loc)
        if m2:
            return m2.group(1)
    log.warning(f"    ⚠️ codoc: 応答URLからentry_id抽出失敗 (URL={r.url})")
    return None


def codoc_update_entry(session, entry_id, title, body_free, body_paywalled,
                       price, binded_url=""):
    """codocの既存記事を更新（_method=PUT）"""
    edit_url = f"{CODOC_BASE}/me/entries/{entry_id}/edit"
    submit_url = f"{CODOC_BASE}/me/entries/{entry_id}"
    token = _get_token(session, edit_url)
    if not token:
        return False
    data = _entry_form_data(title, body_free, body_paywalled, price,
                             binded_url, token, method="PUT")
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
