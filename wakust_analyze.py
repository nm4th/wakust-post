# -*- coding: utf-8 -*-
"""記事の成績を、公開からの経過日数で正規化して分析する

累計PVをそのまま比べると「古い記事ほど多い」だけの話になるので、
公開からの日数で割った PV/日 で見る。あわせて経過日数を揃えた
コホート（既定 91〜180日）でも比較し、時間の効果を除いた差を出す。

  python wakust_analyze.py                # 全体＋コホート比較
  python wakust_analyze.py --cohort 8 30  # コホートの範囲を変える
"""
import json, glob, collections, statistics as st, argparse
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).replace(tzinfo=None)
arts = []
for f in glob.glob("site_content/articles/*.json"):
    a = json.load(open(f, encoding="utf-8"))
    try:
        pub = datetime.strptime(a["published_at"], "%Y-%m-%d %H:%M")
    except (KeyError, ValueError, TypeError):
        continue
    age = max(1, (TODAY - pub).days)
    a["_age"] = age
    a["_pv"] = int(a.get("pv_total") or 0)
    a["_sales"] = int(a.get("sales_count") or 0)
    a["_price"] = int(a.get("price") or 0)
    a["_pvpd"] = a["_pv"] / age
    a["_pub"] = pub
    arts.append(a)

ap = argparse.ArgumentParser()
ap.add_argument("--cohort", nargs=2, type=int, default=[91, 180],
                metavar=("最小日数", "最大日数"),
                help="経過日数を揃えて比較する範囲（既定 91 180）")
args = ap.parse_args()

print(f"分析対象 {len(arts)}件  公開日 {min(a['_pub'] for a in arts):%Y-%m-%d} 〜 {max(a['_pub'] for a in arts):%Y-%m-%d}")
print(f"経過日数 中央値 {st.median([a['_age'] for a in arts]):.0f}日  最小 {min(a['_age'] for a in arts)}  最大 {max(a['_age'] for a in arts)}")

def block(title, groups, keyfmt="{}"):
    print(f"\n■ {title}")
    print(f"  {'':<14}{'件数':>4}{'経過日数':>8}{'累計PV':>8}{'PV/日':>8}{'販売':>7}{'転換率':>8}")
    for k, v in groups:
        if not v: continue
        pv = sum(x["_pv"] for x in v); sa = sum(x["_sales"] for x in v)
        print(f"  {keyfmt.format(k):<14}{len(v):>4}{st.median([x['_age'] for x in v]):>8.0f}"
              f"{st.mean([x['_pv'] for x in v]):>8.0f}{st.mean([x['_pvpd'] for x in v]):>8.2f}"
              f"{st.mean([x['_sales'] for x in v]):>7.1f}{sa/pv*100 if pv else 0:>7.1f}%")

# 1. 経過日数で分けて、そもそも時間効果がどれだけあるか
buckets = [("0-7日", lambda a: a["_age"] <= 7), ("8-30日", lambda a: 8 <= a["_age"] <= 30),
           ("31-90日", lambda a: 31 <= a["_age"] <= 90), ("91-180日", lambda a: 91 <= a["_age"] <= 180),
           ("181日以上", lambda a: a["_age"] > 180)]
block("公開からの経過日数別", [(n, [a for a in arts if f(a)]) for n, f in buckets])

# 2. 価格帯別（時間で正規化）
by = collections.defaultdict(list)
for a in arts: by[a["_price"]].append(a)
block("価格帯別（PV/日で見る）", sorted(by.items()), "¥{}")

# 3. エリア別
by = collections.defaultdict(list)
for a in arts: by[a.get("area") or "?"].append(a)
block("エリア別", sorted(by.items(), key=lambda kv: -len(kv[1])))

# 4. 駅別（5件以上）
by = collections.defaultdict(list)
for a in arts: by[a.get("station") or "?"].append(a)
block("駅別（5件以上）", sorted([(k, v) for k, v in by.items() if len(v) >= 5],
                                key=lambda kv: -st.mean([x["_pvpd"] for x in kv[1]])))

# 5. タグ別（8件以上・カップ除く）
by = collections.defaultdict(list)
for a in arts:
    for t in (a.get("tags") or []):
        if not t.endswith("カップ") and t != a.get("station"):
            by[t].append(a)
block("タグ別（8件以上）", sorted([(k, v) for k, v in by.items() if len(v) >= 8],
                                  key=lambda kv: -st.mean([x["_pvpd"] for x in kv[1]])))

# 6. カップ別
by = collections.defaultdict(list)
for a in arts:
    cup = next((t for t in (a.get("tags") or []) if t.endswith("カップ")), "なし")
    by[cup].append(a)
block("カップ別", sorted([(k, v) for k, v in by.items() if len(v) >= 5],
                        key=lambda kv: -st.mean([x["_pvpd"] for x in kv[1]])))

# 7. 出勤日を持っているか
has = [a for a in arts if a.get("shift_dates")]
non = [a for a in arts if not a.get("shift_dates")]
block("出勤日の有無", [("あり", has), ("なし", non)])

# 8. PV/日 の上位・下位
print("\n■ PV/日 上位10")
for a in sorted(arts, key=lambda a: -a["_pvpd"])[:10]:
    print(f"  {a['_pvpd']:>6.2f}/日  {a['_age']:>3}日  PV{a['_pv']:>5}  販売{a['_sales']:>3}  "
          f"¥{a['_price']}  {a.get('station')}  {a['title'][:26]}")
print("\n■ PV/日 下位10")
for a in sorted(arts, key=lambda a: a["_pvpd"])[:10]:
    print(f"  {a['_pvpd']:>6.2f}/日  {a['_age']:>3}日  PV{a['_pv']:>5}  販売{a['_sales']:>3}  "
          f"¥{a['_price']}  {a.get('station')}  {a['title'][:26]}")


# ============================================================
# 経過日数を揃えたコホート比較（時間の効果を除く）
# ============================================================
lo, hi = args.cohort
coh = [a for a in arts if lo <= a["_age"] <= hi]
print(f"\n{'='*60}")
print(f"■ 経過日数を {lo}〜{hi}日 に揃えた比較（n={len(coh)}）")
print("  累計PVは古い記事ほど大きくなるため、日数を揃えないと比較にならない")
print("=" * 60)
if len(coh) < 10:
    print(f"  件数が少なすぎます（{len(coh)}件）。--cohort で範囲を広げてください")
else:
    def show(title, groups, minn):
        print(f"\n  【{title}】")
        print(f"    {'':<10}{'件数':>4}{'経過日':>7}{'PV/日':>8}{'販売':>7}{'転換率':>8}")
        rows = []
        for k, v in groups:
            if len(v) < minn:
                continue
            pv = sum(x["_pv"] for x in v)
            sa = sum(x["_sales"] for x in v)
            rows.append((st.mean([x["_pvpd"] for x in v]), k, v, pv, sa))
        for pvpd, k, v, pv, sa in sorted(rows, reverse=True):
            print(f"    {k:<10}{len(v):>4}{st.median([x['_age'] for x in v]):>7.0f}"
                  f"{pvpd:>8.2f}{st.mean([x['_sales'] for x in v]):>7.1f}"
                  f"{sa / pv * 100 if pv else 0:>7.1f}%")

    by = collections.defaultdict(list)
    for a in coh:
        by[a.get("station") or "?"].append(a)
    show("駅別（3件以上）", by.items(), 3)

    by = collections.defaultdict(list)
    for a in coh:
        by[next((t for t in (a.get("tags") or []) if t.endswith("カップ")), "なし")].append(a)
    show("カップ別（4件以上）", by.items(), 4)

    by = collections.defaultdict(list)
    for a in coh:
        for t in (a.get("tags") or []):
            if not t.endswith("カップ") and t != a.get("station"):
                by[t].append(a)
    show("タグ別（4件以上）", by.items(), 4)

# ============================================================
# 投入量と見返り
# ============================================================
print(f"\n{'='*60}")
print("■ どこに何本書いたか と その見返り")
print("=" * 60)
by = collections.defaultdict(list)
for a in arts:
    by[a.get("station") or "?"].append(a)
print(f"  {'駅':<10}{'本数':>4}{'全体比':>7}{'総PV':>8}{'総販売':>7}{'PV/日':>8}")
for k, v in sorted(by.items(), key=lambda kv: -len(kv[1]))[:12]:
    print(f"  {k:<10}{len(v):>4}{len(v) / len(arts) * 100:>6.0f}%"
          f"{sum(x['_pv'] for x in v):>8}{sum(x['_sales'] for x in v):>7}"
          f"{st.mean([x['_pvpd'] for x in v]):>8.2f}")
