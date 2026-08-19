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
import hashlib
import argparse
from datetime import datetime, timezone, timedelta
import requests

from wakust_threads_api import is_auth_error

GRAPH = "https://graph.threads.net"
API = f"{GRAPH}/v1.0"

JST = timezone(timedelta(hours=9))
STATE_FILE = "wakust_state.json"
TOKEN_LIFE_DAYS = 60      # 長期トークンの寿命
WARN_DAYS = 45            # ここを過ぎたら警告（まだ延長できる）
FAIL_DAYS = 50            # ここを過ぎたら失敗させて通知する（残り約10日）


class NetworkError(RuntimeError):
    """一時的な通信障害。トークンの生死とは無関係"""


class AuthError(RuntimeError):
    """トークンが無効・失効している"""


def _get(url, params):
    """Graph API を叩く。失敗は通信エラーとトークンエラーに投げ分ける

    通信エラーでワークフローを落とすと、GitHub側の一時障害のたびに
    「トークンが切れた」という誤報メールが飛ぶ。両者は必ず区別する。
    """
    try:
        r = requests.get(url, params=params, timeout=30)
    except requests.RequestException as e:
        raise NetworkError(f"通信エラー: {e}") from e
    try:
        data = r.json()
    except ValueError:
        if r.status_code >= 500:
            raise NetworkError(f"サーバーエラー HTTP {r.status_code}") from None
        raise RuntimeError(
            f"応答がJSONではありません (HTTP {r.status_code}): {r.text[:300]}")
    if r.status_code == 200 and "error" not in data:
        return data

    err = data.get("error") or {}
    msg = err.get("message") or data
    if is_auth_error(r.status_code, err):
        raise AuthError(f"アクセストークンが無効です: {msg}")
    if r.status_code >= 500:
        raise NetworkError(f"サーバーエラー HTTP {r.status_code}: {msg}")
    raise RuntimeError(f"HTTP {r.status_code}: {msg}")


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
    print(f"  THREADS_ACCESS_TOKEN  {long_token}")
    print(f"  THREADS_USER_ID       {me.get('id')}"
          "   ※省略可（未設定なら自動で自分を指します）")
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
    print(f"  THREADS_ACCESS_TOKEN  {token}")
    print(f"  THREADS_USER_ID       {me.get('id')}"
          "   ※省略可（未設定なら自動で自分を指します）")
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


def _load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _token_age_days(token, issued=None):
    """このトークンを使い始めてから何日経ったかを返す（初回は0日）

    Graph API は残り有効期限を教えてくれないので、初めて見た日を
    wakust_state.json に控えて経過日数から逆算する。トークンを差し替えれば
    指紋が変わるので、自動的にカウントがリセットされる。
    issued を渡すと、その日を発行日として上書きする（記録を始める前から
    使っているトークンの残り日数を実態に合わせるため）。
    """
    fp = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    state = _load_state()
    rec = state.get("_threads_token") or {}
    today = datetime.now(JST).strftime("%Y-%m-%d")

    if issued:
        updated = {"fingerprint": fp, "first_seen": issued}
    elif rec.get("fingerprint") != fp:
        updated = {"fingerprint": fp, "first_seen": today}
    else:
        updated = None

    if updated and updated != rec:
        rec = updated
        state["_threads_token"] = rec
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"⚠️ トークンの使用開始日を保存できませんでした: {e}")

    try:
        first = datetime.strptime(rec["first_seen"], "%Y-%m-%d").replace(tzinfo=JST)
    except (KeyError, ValueError):
        return 0
    return max(0, (datetime.now(JST) - first).days)


def cmd_check(args):
    """トークンが生きているか、投稿枠が残っているかを確認

    戻り値が終了コードになる。0=正常、1=要対応。
    通信障害は「要対応」にしない（誤報メールになるため）。
    """
    token = args.token or os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        print("::error::THREADS_ACCESS_TOKEN が未設定です。Threads投稿は止まっています")
        return 1
    try:
        me = _fetch_me(token)
    except AuthError as e:
        print(f"❌ {e}")
        print("::error::Threadsのアクセストークンが失効しました。投稿は止まっています。"
              "Metaダッシュボードでトークンを再発行し、Secrets の "
              "THREADS_ACCESS_TOKEN を更新してください"
              "（長期トークンは60日で失効し、切れた後は延長できません）")
        return 1
    except NetworkError as e:
        # 一時的な障害でメールを飛ばすと、本当の失効に気づけなくなる
        print(f"::warning::確認できませんでした（{e}）。次回の実行で再確認します")
        return 0
    print(f"✅ トークン有効  @{me.get('username')}  (user_id={me.get('id')})")

    env_id = os.environ.get("THREADS_USER_ID", "").strip()
    if env_id and env_id != str(me.get("id")):
        print(f"⚠️ THREADS_USER_ID が一致しません: 環境変数={env_id} / 実際={me.get('id')}")

    try:
        limit = _get(f"{API}/{me.get('id')}/threads_publishing_limit",
                     {"fields": "quota_usage,config", "access_token": token})
        row = (limit.get("data") or [{}])[0]
        used = int(row.get("quota_usage") or 0)
        total = int((row.get("config") or {}).get("quota_total") or 250)
        print(f"📊 本日の投稿数: {used} / {total}（残り {total - used}）")
    except (NetworkError, RuntimeError) as e:
        print(f"⚠️ 投稿枠の取得に失敗: {e}")

    # 今はまだ生きていても、60日で必ず切れる。切れる前に知らせる
    age = _token_age_days(token, getattr(args, "issued", None))
    left = TOKEN_LIFE_DAYS - age
    print(f"🗓  このトークンを使い始めて{age}日（推定残り{left}日）")
    if age >= FAIL_DAYS:
        print(f"::error::Threadsのアクセストークンが期限切れ間近です"
              f"（使用開始から{age}日、推定残り{left}日）。"
              f"いま python wakust_threads_setup.py refresh を実行して"
              f"Secrets の THREADS_ACCESS_TOKEN を更新してください。"
              f"完全に切れると延長できず、再発行が必要になります")
        return 1
    if age >= WARN_DAYS:
        print(f"::warning::トークンの使用開始から{age}日です。"
              f"そろそろ refresh して Secrets を更新してください")
    return 0


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
    p.add_argument("--issued", metavar="YYYY-MM-DD",
                   help="トークンを発行した日。記録を始める前から使っている"
                        "トークンの残り日数を合わせるときに一度だけ指定する")
    p.set_defaults(func=cmd_check)

    args = ap.parse_args()
    try:
        sys.exit(args.func(args) or 0)
    except AuthError as e:
        sys.exit(f"❌ {e}")
    except NetworkError as e:
        sys.exit(f"❌ {e}")


if __name__ == "__main__":
    main()
