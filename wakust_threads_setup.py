"""Threads API のトークン取得・確認ヘルパー

Meta ダッシュボードの「ユーザートークン生成ツール」は、有効期限60日の
長期トークンを直接発行する。その場合は secrets コマンドでユーザーIDを引くだけでよい。
OAuthフローなどで短期トークン（1時間）しか無い場合は exchange で交換する。

  # ダッシュボードで発行した長期トークンから、Secretsに貼る値を出す（通常はこちら）
  python wakust_threads_setup.py secrets --token "XXXX"

  # 短期トークンしか無い場合は長期トークンに交換する
  python wakust_threads_setup.py exchange --short-token "XXXX" --app-secret "YYYY"

  # 今のトークンの残り日数を確認
  THREADS_ACCESS_TOKEN=... python wakust_threads_setup.py check

  # 期限が切れる前に延長（延長後の値をSecretsに入れ直す）
  THREADS_ACCESS_TOKEN=... python wakust_threads_setup.py refresh
"""

import os
import sys
import json
import argparse
import requests

GRAPH = "https://graph.threads.net"
API = f"{GRAPH}/v1.0"


def _get(url, params):
    try:
        r = requests.get(url, params=params, timeout=30)
    except requests.RequestException as e:
        sys.exit(f"❌ 通信エラー: {e}")
    try:
        data = r.json()
    except ValueError:
        sys.exit(f"❌ 応答がJSONではありません (HTTP {r.status_code}): {r.text[:300]}")
    if r.status_code != 200 or "error" in data:
        err = data.get("error", {})
        sys.exit(f"❌ HTTP {r.status_code}: {err.get('message') or data}")
    return data


def _days(expires_in):
    try:
        return round(int(expires_in) / 86400, 1)
    except (TypeError, ValueError):
        return "?"


def _fetch_me(token):
    return _get(f"{API}/me", {"fields": "id,username", "access_token": token})


def cmd_exchange(args):
    """短期トークン → 長期トークン（60日）"""
    data = _get(f"{GRAPH}/access_token", {
        "grant_type": "th_exchange_token",
        "client_secret": args.app_secret,
        "access_token": args.short_token,
    })
    long_token = data.get("access_token")
    if not long_token:
        sys.exit(f"❌ 長期トークンが取れませんでした: {data}")
    me = _fetch_me(long_token)

    print("\n✅ 取得できました。GitHub の Secrets に以下を登録してください。\n")
    print(f"  THREADS_USER_ID       {me.get('id')}")
    print(f"  THREADS_ACCESS_TOKEN  {long_token}")
    print(f"\n  アカウント : @{me.get('username')}")
    print(f"  有効期限   : 約{_days(data.get('expires_in'))}日")
    print("\n⚠️ 期限が切れると延長できません。切れる前に refresh を実行してください。")
    if args.json:
        print("\n" + json.dumps(
            {"user_id": me.get("id"), "access_token": long_token,
             "username": me.get("username"), "expires_in": data.get("expires_in")},
            ensure_ascii=False, indent=2))


def cmd_secrets(args):
    """既に長期トークンを持っている場合に、Secretsに貼る2つの値を出力する

    Metaダッシュボードの「ユーザートークン生成ツール」は長期トークンを
    直接発行するため、exchange を経由せずこちらを使えばよい。
    """
    token = args.token or os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        sys.exit("❌ トークンを --token で渡すか THREADS_ACCESS_TOKEN に設定してください")
    me = _fetch_me(token)

    print("\n✅ 確認できました。GitHub の Secrets に以下を登録してください。\n")
    print(f"  THREADS_USER_ID       {me.get('id')}")
    print(f"  THREADS_ACCESS_TOKEN  {token}")
    print(f"\n  アカウント : @{me.get('username')}")
    print("\n⚠️ 長期トークンは約60日で失効し、切れると延長できません。")
    print("   期限前に refresh を実行して、Secrets を更新してください。")


def cmd_refresh(args):
    """長期トークンを延長する（有効期限内にのみ可能）"""
    token = args.token or os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        sys.exit("❌ THREADS_ACCESS_TOKEN が未設定です")
    data = _get(f"{GRAPH}/refresh_access_token", {
        "grant_type": "th_refresh_token",
        "access_token": token,
    })
    new_token = data.get("access_token")
    print("\n✅ 延長しました。Secrets の THREADS_ACCESS_TOKEN を更新してください。\n")
    print(f"  THREADS_ACCESS_TOKEN  {new_token}")
    print(f"\n  有効期限: 約{_days(data.get('expires_in'))}日")


def cmd_check(args):
    """トークンが生きているか、投稿枠が残っているかを確認"""
    token = args.token or os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        sys.exit("❌ THREADS_ACCESS_TOKEN が未設定です")
    me = _fetch_me(token)
    print(f"✅ トークン有効  @{me.get('username')}  (user_id={me.get('id')})")

    env_id = os.environ.get("THREADS_USER_ID", "").strip()
    if env_id and env_id != str(me.get("id")):
        print(f"⚠️ THREADS_USER_ID が一致しません: 環境変数={env_id} / 実際={me.get('id')}")

    limit = _get(f"{API}/{me.get('id')}/threads_publishing_limit",
                 {"fields": "quota_usage,config", "access_token": token})
    row = (limit.get("data") or [{}])[0]
    used = int(row.get("quota_usage") or 0)
    total = int((row.get("config") or {}).get("quota_total") or 250)
    print(f"📊 本日の投稿数: {used} / {total}（残り {total - used}）")


def main():
    ap = argparse.ArgumentParser(description="Threads API のトークン取得・確認")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("exchange", help="短期トークンを長期トークンに交換する")
    p.add_argument("--short-token", required=True, help="ダッシュボードで発行した短期トークン")
    p.add_argument("--app-secret", required=True, help="Threads アプリのシークレット")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_exchange)

    p = sub.add_parser(
        "secrets",
        help="長期トークンからSecretsに貼る値を出力する（ダッシュボード発行時はこちら）")
    p.add_argument("--token", help="省略時は THREADS_ACCESS_TOKEN を使う")
    p.set_defaults(func=cmd_secrets)

    p = sub.add_parser("refresh", help="長期トークンを延長する")
    p.add_argument("--token", help="省略時は THREADS_ACCESS_TOKEN を使う")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("check", help="トークンと投稿枠を確認する")
    p.add_argument("--token", help="省略時は THREADS_ACCESS_TOKEN を使う")
    p.set_defaults(func=cmd_check)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
