"""Threads API クライアント（投稿の作成・公開・リプライ）

Meta の Threads Graph API を叩いて投稿する。
  1. コンテナ作成   POST /v1.0/{user_id}/threads?media_type=TEXT&text=...
  2. 公開           POST /v1.0/{user_id}/threads_publish?creation_id=...
  3. リプライ       1と同じだが reply_to_id を付ける

必要な環境変数:
  THREADS_USER_ID       Threads のユーザーID（数値）
  THREADS_ACCESS_TOKEN  長期アクセストークン（有効期限60日）

トークンが未設定のときは dry-run になり、投稿せずに内容を表示するだけ。
"""

import os
import time
import json
import logging
import requests

log = logging.getLogger("wakust.threads")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

API_BASE = "https://graph.threads.net/v1.0"
REFRESH_URL = "https://graph.threads.net/refresh_access_token"
TEXT_LIMIT = 500          # Threadsの本文上限
CONTAINER_WAIT = 5        # コンテナ作成→公開の待機秒数


class ThreadsError(RuntimeError):
    pass


class ThreadsClient:
    def __init__(self, user_id=None, token=None, dry_run=None):
        self.user_id = user_id or os.environ.get("THREADS_USER_ID", "").strip()
        self.token = token or os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
        # 明示指定がなければ、認証情報の有無で自動判定
        self.dry_run = (not (self.user_id and self.token)) if dry_run is None else dry_run
        if self.dry_run:
            log.info("🧪 Threads: dry-run モード（実際には投稿しません）")

    # ------------------------------------------------------------
    def _post(self, path, params):
        url = f"{API_BASE}/{self.user_id}/{path}"
        payload = dict(params, access_token=self.token)
        try:
            r = requests.post(url, data=payload, timeout=30)
        except requests.RequestException as e:
            raise ThreadsError(f"リクエスト失敗 {path}: {e}") from e
        if r.status_code != 200:
            raise ThreadsError(f"{path} HTTP {r.status_code}: {r.text[:400]}")
        try:
            return r.json()
        except ValueError as e:
            raise ThreadsError(f"{path} のJSON解析に失敗: {r.text[:200]}") from e

    def _get(self, path, params=None):
        url = f"{API_BASE}/{self.user_id}/{path}"
        payload = dict(params or {}, access_token=self.token)
        try:
            r = requests.get(url, params=payload, timeout=30)
        except requests.RequestException as e:
            raise ThreadsError(f"リクエスト失敗 {path}: {e}") from e
        if r.status_code != 200:
            raise ThreadsError(f"{path} HTTP {r.status_code}: {r.text[:400]}")
        return r.json()

    # ------------------------------------------------------------
    def publishing_limit(self):
        """24時間あたりの投稿数の使用状況を返す（上限250件）"""
        if self.dry_run:
            return {"quota_usage": 0, "config": {"quota_total": 250}}
        data = self._get("threads_publishing_limit",
                         {"fields": "quota_usage,config"})
        return (data.get("data") or [{}])[0]

    def remaining_quota(self):
        try:
            d = self.publishing_limit()
            used = int(d.get("quota_usage") or 0)
            total = int((d.get("config") or {}).get("quota_total") or 250)
            return max(0, total - used), total
        except ThreadsError as e:
            log.warning(f"    ⚠️ Threads: 投稿上限の取得に失敗: {e}")
            return None, None

    # ------------------------------------------------------------
    def post(self, text, reply_to_id=None, wait=CONTAINER_WAIT):
        """テキスト投稿を1件公開して、投稿IDを返す"""
        text = (text or "").strip()
        if not text:
            raise ThreadsError("本文が空です")
        if len(text) > TEXT_LIMIT:
            log.warning(f"    ⚠️ Threads: 本文が{len(text)}文字。"
                        f"{TEXT_LIMIT}文字に切り詰めます")
            text = text[:TEXT_LIMIT]

        kind = "リプライ" if reply_to_id else "投稿"
        if self.dry_run:
            log.info(f"🧪 [dry-run] {kind} ({len(text)}文字)"
                     + (f" → reply_to={reply_to_id}" if reply_to_id else ""))
            for line in text.splitlines():
                log.info(f"     │ {line}")
            return f"dryrun-{abs(hash(text)) % 10**10}"

        params = {"media_type": "TEXT", "text": text}
        if reply_to_id:
            params["reply_to_id"] = reply_to_id
        container = self._post("threads", params)
        creation_id = container.get("id")
        if not creation_id:
            raise ThreadsError(f"コンテナIDが取れません: {container}")
        # コンテナの処理待ち（Metaの推奨に従い数秒あける）
        if wait:
            time.sleep(wait)
        published = self._post("threads_publish", {"creation_id": creation_id})
        post_id = published.get("id")
        if not post_id:
            raise ThreadsError(f"公開に失敗しました: {published}")
        log.info(f"    ✅ Threads{kind}成功 id={post_id}")
        return post_id

    def post_with_reply(self, text, reply_text=None, wait=CONTAINER_WAIT):
        """本文を投稿し、続けてリプライにリンクを置く

        本文にリンクを入れないための運用。Threads API ではリプライは
        1日250件の投稿上限にカウントされない。
        """
        post_id = self.post(text, wait=wait)
        reply_id = None
        if reply_text and reply_text.strip():
            try:
                reply_id = self.post(reply_text, reply_to_id=post_id, wait=wait)
            except ThreadsError as e:
                # 本体は投稿済みなので、リプライ失敗だけで全体を落とさない
                log.error(f"❌ Threads: リプライ投稿に失敗: {e}")
        return post_id, reply_id

    # ------------------------------------------------------------
    def refresh_token(self):
        """長期トークンを延長する（有効期限が切れる前に実行すること）"""
        if self.dry_run:
            log.info("🧪 [dry-run] トークン更新をスキップ")
            return None
        try:
            r = requests.get(REFRESH_URL, params={
                "grant_type": "th_refresh_token",
                "access_token": self.token,
            }, timeout=30)
        except requests.RequestException as e:
            raise ThreadsError(f"トークン更新リクエスト失敗: {e}") from e
        if r.status_code != 200:
            raise ThreadsError(f"トークン更新 HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        log.info(f"    🔑 トークン更新成功（残り{data.get('expires_in')}秒）")
        return data.get("access_token")


if __name__ == "__main__":
    client = ThreadsClient()
    left, total = client.remaining_quota()
    if left is not None:
        print(f"本日の残り投稿数: {left} / {total}")
    else:
        print("投稿上限を取得できませんでした（認証情報を確認してください）")
