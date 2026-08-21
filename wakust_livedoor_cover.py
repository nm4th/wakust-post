"""livedoor Blog の「記事の見出し画像」を設定する

AtomPub には見出し画像の項目が無い（設定済みの記事を GET しても現れない）。
管理画面の編集フォームだけが持っている cover_image_attachment_id を
埋めて POST するしかないので、ここだけブラウザと同じことをする。

画像ID は AtomPub のアップロード時に取れていて、投稿履歴の
_livedoor[記事ID].image_id に控えてある。

必要な環境変数:
  LIVEDOOR_USER_ID   livedoor ID
  LIVEDOOR_PASSWORD  livedoor のログインパスワード（AtomPub用とは別）
  LIVEDOOR_BLOG_NAME ブログ識別子

⚠️ このやり方はフォームの全項目を送り直す。項目を取りこぼすと記事が
   壊れるので、既存の値はページから読んだものをそのまま返す。
   変更するのは cover_image_attachment_id だけ。

  python wakust_livedoor_cover.py --id 16796667            # 何を送るか見るだけ
  python wakust_livedoor_cover.py --id 16796667 --apply    # 実際に反映する
  python wakust_livedoor_cover.py --all --apply            # 未設定の記事をまとめて
"""

import os
import re
import sys
import json
import argparse
import logging

import requests
from html.parser import HTMLParser
from urllib.parse import urljoin

log = logging.getLogger("wakust.livedoor.cover")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

LOGIN_URL = "https://member.livedoor.com/login/index"
BLOGCMS = "https://livedoor.blogcms.jp"
STATE_FILE = "wakust_state.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# 送ってはいけない項目（ファイル入力や送信ボタンの重複）
SKIP_FIELDS = {"upfile", "cropped_image"}


class CoverError(RuntimeError):
    pass


class FormParser(HTMLParser):
    """HTMLからフォームの入力項目を読み取る

    管理画面のフォームは項目が多く、1つでも落とすと保存時に消える。
    bs4に頼らず標準ライブラリだけで、input / textarea / select を拾う。
    want に項目名を渡すと、その項目を持つフォームだけを対象にする。
    """

    def __init__(self, want=None):
        super().__init__(convert_charrefs=True)
        self.want = want
        self.forms = []          # [{"action":…, "data":{…}}]
        self._cur = None
        self._ta = None          # 収集中の textarea 名
        self._sel = None         # 収集中の select 名
        self._sel_first = None
        self._sel_done = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self._cur = {"action": a.get("action", ""), "data": {},
                         "buttons": [], "method": (a.get("method") or "get").lower()}
            return
        if self._cur is None:
            return
        if tag == "input":
            name = a.get("name")
            if not name:
                return
            t = (a.get("type") or "text").lower()
            if t == "file":
                return
            if t in ("checkbox", "radio"):
                if "checked" in a:
                    self._cur["data"][name] = a.get("value", "on")
            else:
                self._cur["data"][name] = a.get("value", "")
        elif tag == "button":
            # 保存ボタンが <button name="..."> のことがある。
            # これを送らないとサーバー側でアクションを判別できない
            name = a.get("name")
            if name:
                self._cur.setdefault("buttons", []).append(
                    (name, a.get("value", ""), (a.get("type") or "submit").lower()))
        elif tag == "textarea":
            self._ta = a.get("name")
            if self._ta:
                self._cur["data"][self._ta] = ""
        elif tag == "select":
            self._sel = a.get("name")
            self._sel_first = None
            self._sel_done = False
        elif tag == "option" and self._sel:
            v = a.get("value", "")
            if self._sel_first is None:
                self._sel_first = v
            if "selected" in a and not self._sel_done:
                self._cur["data"][self._sel] = v
                self._sel_done = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._cur is not None and self._ta:
            self._cur["data"][self._ta] += data

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._ta = None
        elif tag == "select":
            if self._sel and not self._sel_done:
                self._cur["data"][self._sel] = self._sel_first or ""
            self._sel = None
        elif tag == "form" and self._cur is not None:
            for k in SKIP_FIELDS:
                self._cur["data"].pop(k, None)
            self.forms.append(self._cur)
            self._cur = None

    def pick(self):
        """目的のフォームを1つ返す"""
        if self.want:
            for f in self.forms:
                if self.want in f["data"]:
                    return f
        return self.forms[0] if self.forms else None


def parse_form(html_text, want=None):
    p = FormParser(want)
    p.feed(html_text)
    p.close()
    return p.pick()


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def login():
    """livedoor にログインしたセッションを返す"""
    user = os.environ.get("LIVEDOOR_USER_ID", "").strip()
    pw = os.environ.get("LIVEDOOR_PASSWORD", "").strip()
    if not (user and pw):
        raise CoverError(
            "LIVEDOOR_USER_ID と LIVEDOOR_PASSWORD を設定してください"
            "（PASSWORD はログイン用。AtomPub用パスワードとは別です）")

    s = requests.Session()
    s.headers.update({"User-Agent": UA,
                      "Accept-Language": "ja,en-US;q=0.7,en;q=0.3"})
    r = s.get(LOGIN_URL, timeout=30)
    form = parse_form(r.text, want="livedoor_id") or parse_form(r.text)
    if not form:
        raise CoverError(f"ログインフォームが見つかりません (HTTP {r.status_code})")

    data = dict(form["data"])
    data["livedoor_id"] = user
    data["password"] = pw
    # action は "./edit" のような相対URLのこともあるので、
    # 取得したページのURLを基準に解決する
    action = urljoin(r.url, form["action"] or "")

    r = s.post(action, data=data, timeout=30, headers={"Referer": LOGIN_URL})
    log.info(f"    🔑 ログイン POST → HTTP {r.status_code}")

    chk = s.get(f"{BLOGCMS}/member/", timeout=30, allow_redirects=True)
    if "login" in chk.url:
        raise CoverError("ログインに失敗しました。IDとパスワードを確認してください")
    log.info("    ✅ ログイン成功")
    return s


def edit_url(blog, article_id):
    return f"{BLOGCMS}/blog/{blog}/article/edit?id={article_id}"


def read_form(session, blog, article_id):
    """編集ページのフォームを読み、(action, 送信データ) を返す"""
    url = edit_url(blog, article_id)
    r = session.get(url, timeout=60)
    if r.status_code != 200:
        raise CoverError(f"編集ページを開けません HTTP {r.status_code}: {url}")
    form = parse_form(r.text, want="cover_image_attachment_id")
    if not form:
        raise CoverError("記事編集フォームが見つかりません。"
                         "管理画面のHTMLが変わった可能性があります")
    # action は "./edit" のような相対URLのこともある
    action = urljoin(r.url, form["action"] or "")
    return action, form["data"], form.get("buttons") or []


def set_cover(session, blog, article_id, image_id, apply=False):
    """1記事の見出し画像を設定する"""
    action, data, buttons = read_form(session, blog, article_id)
    # 保存ボタンが <button name="..."> の場合、それも送らないと
    # サーバー側でどの操作か判別できない
    for name, value, btype in buttons:
        if btype == "submit":
            data.setdefault(name, value)
    current = data.get("cover_image_attachment_id", "")
    body_len = len(data.get("body", "") or data.get("article_body", "") or "")
    log.info(f"  📄 [{article_id}] フォーム項目 {len(data)}個 / 本文 {body_len}文字")
    log.info(f"     見出し画像: '{current}' → '{image_id}'")

    if "cover_image_attachment_id" not in data:
        raise CoverError("cover_image_attachment_id がフォームにありません")
    if current == str(image_id):
        log.info("     すでに設定済み。何もしません")
        return False
    if body_len < 200:
        # 本文が読めていないのに送ると記事を空にしてしまう
        raise CoverError(f"本文が{body_len}文字しか読めていません。"
                         "送信すると記事を壊すので中止します")

    data["cover_image_attachment_id"] = str(image_id)
    if not apply:
        log.info("     🧪 --apply が無いので送信しません")
        log.info(f"     送信先: {action}")
        log.info(f"     ボタン: {buttons or '（なし）'}")
        log.info(f"     項目名: {sorted(data)}")
        return False

    r = session.post(action, data=data, timeout=60,
                     headers={"Referer": edit_url(blog, article_id),
                              "Origin": BLOGCMS,
                              "X-Requested-With": "XMLHttpRequest"})
    log.info(f"     POST {action} → HTTP {r.status_code}  最終URL {r.url}")
    if r.status_code not in (200, 302):
        raise CoverError(f"保存に失敗 HTTP {r.status_code}: {r.text[:300]}")

    # 反映されたか読み直して確かめる
    _, after, _ = read_form(session, blog, article_id)
    ok = after.get("cover_image_attachment_id", "") == str(image_id)
    if ok:
        log.info("     ✅ 反映されました")
        return True
    log.info(f"     ⚠️ 反映されていません（現在: '{after.get('cover_image_attachment_id')}'）")
    # 何が返ってきたのか手がかりを出す
    body = re.sub(r"<(script|style)\b.*?</\1>", "", r.text, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    log.info(f"     応答の先頭: {body[:300]}")
    return False


def main():
    ap = argparse.ArgumentParser(description="livedoorの見出し画像を設定する")
    ap.add_argument("--id", help="記事ID（livedoor側）")
    ap.add_argument("--all", action="store_true",
                    help="投稿履歴にある記事をまとめて処理する")
    ap.add_argument("--limit", type=int, default=20, help="--all のときの件数")
    ap.add_argument("--apply", action="store_true",
                    help="実際に保存する（付けないと内容の確認だけ）")
    args = ap.parse_args()

    blog = os.environ.get("LIVEDOOR_BLOG_NAME", "").strip()
    if not blog:
        print("❌ LIVEDOOR_BLOG_NAME が未設定です")
        return 1

    state = load_state()
    posted = state.get("_livedoor") or {}

    # 対象を組み立てる: (livedoor記事ID, 画像ID, ワクスト記事ID)
    targets = []
    if args.id:
        rec = next((v for v in posted.values()
                    if str(args.id) in (v.get("edit_url") or "")), None)
        image_id = (rec or {}).get("image_id", "")
        if not image_id:
            print(f"❌ 記事 {args.id} の画像IDが投稿履歴にありません")
            return 1
        targets.append((args.id, image_id, ""))
    elif args.all:
        for wid, rec in posted.items():
            if rec.get("cover_set") or not rec.get("image_id"):
                continue
            m = re.search(r"/article/(\d+)", rec.get("edit_url") or "")
            if m:
                targets.append((m.group(1), rec["image_id"], wid))
        targets = targets[:max(1, args.limit)]
    else:
        print("--id か --all を指定してください")
        return 1

    if not targets:
        print("対象がありません")
        return 0

    try:
        session = login()
    except CoverError as e:
        print(f"❌ {e}")
        return 1

    ok = ng = 0
    for aid, image_id, wid in targets:
        try:
            if set_cover(session, blog, aid, image_id, args.apply):
                ok += 1
                if wid:
                    posted[wid]["cover_set"] = True
                    save_state(state)
        except CoverError as e:
            print(f"  ❌ [{aid}] {e}")
            ng += 1
    print(f"\n設定 {ok}件 / 失敗 {ng}件")
    return 1 if ng else 0


if __name__ == "__main__":
    sys.exit(main())
