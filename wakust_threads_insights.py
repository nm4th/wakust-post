"""Threads投稿の反応を記録して、テンプレートごとに集計する

投稿履歴（wakust_state.json の _threads）にある投稿の表示数・いいね数を取得し、
logs/threads_insights.csv に追記する。同じ投稿を何日か追うことで
「伸びたテンプレートはどれか」を数字で判断できるようにする。

  python wakust_threads_insights.py            # 収集して集計を表示
  python wakust_threads_insights.py --report   # 収集せず、CSVから集計だけ
"""

import os
import csv
import json
import argparse
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
STATE_FILE = "wakust_state.json"
CSV_PATH = "logs/threads_insights.csv"
COLUMNS = ["記録日時", "投稿日時", "テンプレート", "post_id",
           "表示数", "いいね", "返信", "リポスト", "引用"]
# これより古い投稿はもう伸びないので取得しない
TRACK_DAYS = 14


def _load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def collect():
    """投稿履歴をたどってインサイトを取り、CSVに追記する"""
    from wakust_threads_api import ThreadsClient, ThreadsError

    posted = _load_state().get("_threads") or {}
    if not posted:
        print("投稿履歴がありません（まだ投稿していないか、state未保存）")
        return []

    cutoff = (datetime.now(JST) - timedelta(days=TRACK_DAYS)).strftime("%Y-%m-%d")
    client = ThreadsClient()
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for key, rec in sorted(posted.items()):
        # key は "YYYY-MM-DD:template"
        date, _, template = key.partition(":")
        if date < cutoff:
            continue
        post_id = (rec or {}).get("post_id") or ""
        if not post_id or post_id.startswith("dryrun-"):
            continue
        try:
            m = client.insights(post_id)
        except ThreadsError as e:
            print(f"  ⚠️ {key}: {e}")
            continue
        rows.append({
            "記録日時": now,
            "投稿日時": (rec or {}).get("at") or date,
            "テンプレート": template,
            "post_id": post_id,
            "表示数": m.get("views", 0),
            "いいね": m.get("likes", 0),
            "返信": m.get("replies", 0),
            "リポスト": m.get("reposts", 0),
            "引用": m.get("quotes", 0),
        })
        print(f"  📊 {template:10} 表示{m.get('views', 0):5}  "
              f"いいね{m.get('likes', 0):4}  返信{m.get('replies', 0):3}")

    if rows:
        os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
        exists = os.path.exists(CSV_PATH)
        with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            if not exists:
                w.writeheader()
            w.writerows(rows)
        print(f"\n💾 {len(rows)}件を {CSV_PATH} に記録しました")
    return rows


def report():
    """CSVから、テンプレートごとの平均反応を出す"""
    if not os.path.exists(CSV_PATH):
        print("まだ記録がありません")
        return
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    # 同じ投稿は最後の記録（＝一番伸びた時点）だけを使う
    latest = {}
    for r in rows:
        latest[r["post_id"]] = r

    agg = {}
    for r in latest.values():
        t = r["テンプレート"]
        a = agg.setdefault(t, {"n": 0, "views": 0, "likes": 0, "replies": 0})
        a["n"] += 1
        for k, col in (("views", "表示数"), ("likes", "いいね"), ("replies", "返信")):
            try:
                a[k] += int(r[col] or 0)
            except ValueError:
                pass

    if not agg:
        print("集計できる記録がありません")
        return
    print(f"\n{'テンプレート':<14}{'投稿数':>5}{'平均表示':>9}{'平均いいね':>10}{'平均返信':>8}")
    print("-" * 48)
    for t, a in sorted(agg.items(), key=lambda kv: -kv[1]["views"] / max(1, kv[1]["n"])):
        n = a["n"]
        print(f"{t:<14}{n:>5}{a['views'] // n:>9}{a['likes'] / n:>10.1f}"
              f"{a['replies'] / n:>8.1f}")
    print(f"\n※ 平均表示が高い順。投稿数が少ないうちは参考程度に。")


def main():
    ap = argparse.ArgumentParser(description="Threads投稿の反応を記録・集計する")
    ap.add_argument("--report", action="store_true", help="収集せず集計だけ表示")
    args = ap.parse_args()
    if not args.report:
        collect()
    report()


if __name__ == "__main__":
    main()
