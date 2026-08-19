"""Threads / X 投稿文の自動生成

site_content/articles/*.json（自社サイトの記事データ）から、
SNSにそのまま貼れる投稿文を組み立てる。投稿APIは叩かず、文面だけを作る。

  python wakust_threads.py                     # 全テンプレートを表示
  python wakust_threads.py --template today    # 1つだけ表示
  python wakust_threads.py --area 新宿 --json  # JSONで出力（API連携用）

参考にした構成（メンエス系アカウントの「保存される投稿」の型）:
  1行目 = タイトル（エリア＋切り口）
  空行
  箇条書き（6〜8件）
  ---- セパレータ
  締めの一言（保存を促すCTA）
本文にはリンクを入れず、リプライにURLを置く運用を想定しているので、
各テンプレートは本文とリプライ文をセットで返す。
"""

import os
import re
import json
import random
import argparse
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from wakust_site import load_config, load_articles, fmt_date

JST = timezone(timedelta(hours=9))
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


# ============================================================
# 補助
# ============================================================
def display_name(title):
    """記事タイトルから表示名を取り出す

    "【8/20,21出勤】ゆい Fカップ" → "ゆい"
    """
    t = re.sub(r"【[^】]*】", "", title or "").strip()
    t = re.sub(r"\s*\d+pt→\d+pt\([^)]*\)\s*$", "", t)
    # 最初の空白までを名前とみなす（属性が続くことが多い）
    name = re.split(r"[\s　/｜|・]", t, 1)[0].strip()
    return name or (t[:12] if t else "（名前不明）")


def label_of(cfg, a):
    """投稿に出す見出し。show_names=False なら名前を伏せてタグで表す

    名前を出さない場合、「誰か」は伏せたまま「どんな子か」だけ伝える形にする。
    """
    if (cfg.get("threads") or {}).get("show_names"):
        return display_name(a["title"])
    tags = a.get("tags") or []
    if tags:
        return " / ".join(tags[:3])
    return a.get("area") or "―"


def today_iso(offset=0):
    return (datetime.now(JST).date() + timedelta(days=offset)).isoformat()


def yen(v):
    try:
        return f"¥{int(v):,}"
    except (TypeError, ValueError):
        return "―"


def _next_shift(a):
    """今日以降で最も近い出勤日を返す。過去しか無ければ None"""
    today = today_iso()
    future = [d for d in (a.get("shift_dates") or []) if d >= today]
    return min(future) if future else None


def _spread(items, limit):
    """並び順を保ったまま、全体に散らして limit 件を選ぶ

    同額が並ぶと「料金順（安→高）」が同じ値の羅列になってしまうため、
    先頭から詰めるのではなく価格帯全体が見えるように間引く。
    """
    n = len(items)
    if n <= limit:
        return items
    if limit == 1:
        return [items[0]]
    idx = sorted({round(i * (n - 1) / (limit - 1)) for i in range(limit)})
    return [items[i] for i in idx]


def _filter(articles, area=None, day=None, tag=None):
    out = []
    for a in articles:
        if area and (a.get("area") or "") != area:
            continue
        if tag and tag not in (a.get("tags") or []):
            continue
        if day and day not in (a.get("shift_dates") or []):
            continue
        out.append(a)
    return out


def _url(cfg, **params):
    """一覧向けの誘導先URLを組み立てる

    link_target="site"   … 自社サイトの絞り込み済み一覧URL
    link_target="wakust" … ワクストの着地URL（wakust_landing_url）
    """
    tcfg = cfg.get("threads") or {}
    if tcfg.get("link_target") == "wakust":
        return (tcfg.get("wakust_landing_url") or "").strip()
    base = cfg["base_url"] + "/"
    q = "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v)
    return base + ("?" + q if q else "")


def _article_url(cfg, a):
    """記事1件の誘導先URL"""
    tcfg = cfg.get("threads") or {}
    if tcfg.get("link_target") == "wakust":
        return a.get("source_url") or (tcfg.get("wakust_landing_url") or "").strip()
    return f'{cfg["base_url"]}/articles/{a["id"]}/'


def _closer(cfg, seed):
    closers = (cfg.get("threads") or {}).get("closers") or ["保存版📌"]
    return closers[seed % len(closers)]


def _compose(cfg, title, lines, closer, reply_url, note=None):
    tcfg = cfg.get("threads") or {}
    sep = tcfg.get("separator", "----")
    body = [title, ""] + lines + ["", sep, ""]
    if note:
        body += [note, ""]
    body.append(closer)
    reply = ""
    if reply_url:
        reply = f'{tcfg.get("reply_lead", "詳細はこちら👇")}\n{reply_url}'
    else:
        # 貼るURLが無い投稿は、プロフィールのリンクへ誘導する
        cta = (tcfg.get("profile_cta") or "").strip()
        if cta:
            body.append(cta)
    return {
        "text": "\n".join(body).strip(),
        "reply": reply,
        "url": reply_url,
    }


# ============================================================
# テンプレート
# ============================================================
POOL_FILE = "threads_pool.json"


def load_pool():
    """手書きの投稿ストックを読む（無ければ空）"""
    if not os.path.exists(POOL_FILE):
        return {}
    try:
        with open(POOL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"⚠️ {POOL_FILE} の読み込みに失敗: {e}")
        return {}
    return {k: v for k, v in data.items()
            if isinstance(v, list) and not k.startswith("_")}


def _pool_key(kind, text):
    import hashlib
    return f'{kind}:{hashlib.md5(text.encode("utf-8")).hexdigest()[:8]}'


def tpl_pool(cfg, articles, area=None, kind="aruaru"):
    """手書きストックから、一番長く使っていないものを1件出す

    データから作れない「あるあるネタ」などを、重複させずに回すための枠。
    """
    entries = [t for t in (load_pool().get(kind) or []) if t and t.strip()]
    if not entries:
        return None
    used = _load_state().get("_threads_pool", {})
    # 未使用（""）が最優先、次に使用日が古い順
    entries.sort(key=lambda t: used.get(_pool_key(kind, t), ""))
    text = entries[0].strip()
    tcfg = cfg.get("threads") or {}
    cta = (tcfg.get("profile_cta") or "").strip()
    if cta and cta not in text:
        text = f"{text}\n\n{cta}"
    return {"text": text, "reply": "", "url": "", "pool_key": _pool_key(kind, entries[0])}


def tpl_today(cfg, articles, area=None):
    """本日出勤を料金順に並べる（参考投稿の「価格（安→高）」型）"""
    day = today_iso()
    items = _filter(articles, area=area, day=day)
    if not items:
        return None
    items.sort(key=lambda a: int(a.get("price") or 0))
    limit = (cfg.get("threads") or {}).get("max_items", 8)
    label = area or "全エリア"
    lines = [f'{yen(a.get("price"))}　{label_of(cfg, a)}（{a.get("area")}）'
             for a in _spread(items, limit)]
    d = datetime.now(JST).date()
    note = f"本日は{len(items)}名が出勤。"
    return _compose(
        cfg,
        f'{label} 本日{d.month}/{d.day}({WEEKDAY_JP[d.weekday()]})出勤 料金順（安→高）',
        lines, _closer(cfg, d.day), _url(cfg, area=area, day="today"), note)


def tpl_cheatsheet(cfg, articles, area=None):
    """目的別チートシート（参考投稿の「→」型）

    価格・人気・出勤の近さなど、記事データから機械的に決まる軸だけで作る。
    """
    items = _filter(articles, area=area)
    if len(items) < 3:
        return None
    limit = (cfg.get("threads") or {}).get("max_items", 8)
    picks = []

    def add(label, sorted_items, key=None):
        for a in sorted_items:
            if a["id"] in [p[1]["id"] for p in picks]:
                continue
            picks.append((label, a, key(a) if key else ""))
            return

    add("まず安く試すなら", sorted(items, key=lambda a: int(a.get("price") or 0)),
        lambda a: yen(a.get("price")))
    add("人気で選ぶなら",
        sorted(items, key=lambda a: -int(a.get("sales_count") or 0)),
        lambda a: f'販売{a.get("sales_count") or 0}回')
    today = today_iso()
    add("今日会えるのは",
        [a for a in items if today in (a.get("shift_dates") or [])])
    add("明日会えるのは",
        [a for a in items if today_iso(1) in (a.get("shift_dates") or [])])
    upcoming = [a for a in items if _next_shift(a)]
    upcoming.sort(key=_next_shift)
    add("直近の出勤が早いのは", upcoming, lambda a: fmt_date(_next_shift(a)))
    add("上を見るなら",
        sorted(items, key=lambda a: -int(a.get("price") or 0)),
        lambda a: yen(a.get("price")))

    if len(picks) < 3:
        return None
    width = max(len(label) for label, _, _ in picks[:limit])
    lines = []
    for label, a, extra in picks[:limit]:
        pad = "　" * (width - len(label))
        tail = f"（{extra}）" if extra else ""
        lines.append(f'・{label}{pad}　→　{label_of(cfg, a)}{tail}')
    return _compose(
        cfg, f'{area or "全エリア"} 目的別チートシート',
        lines, _closer(cfg, len(items)), _url(cfg, area=area))


def tpl_week(cfg, articles, area=None):
    """1週間の出勤カレンダー"""
    items = _filter(articles, area=area)
    if not items:
        return None
    show_names = (cfg.get("threads") or {}).get("show_names")
    lines = []
    for i in range(7):
        d = today_iso(i)
        day_items = [a for a in items if d in (a.get("shift_dates") or [])]
        if not day_items:
            continue
        if show_names:
            names = [display_name(a["title"]) for a in day_items]
            body = "・".join(names[:6])
            if len(names) > 6:
                body += f" 他{len(names) - 6}名"
        else:
            # 名前は出さず、エリアごとの人数だけ載せる
            by_area = {}
            for a in day_items:
                by_area[a.get("area") or "その他"] = \
                    by_area.get(a.get("area") or "その他", 0) + 1
            body = "・".join(f"{k}{v}名" for k, v in
                            sorted(by_area.items(), key=lambda kv: -kv[1]))
        lines.append(f"{fmt_date(d)}　{body}")
    if not lines:
        return None
    return _compose(
        cfg, f'{area or "全エリア"} 今週の出勤まとめ',
        lines, _closer(cfg, len(lines)),
        _url(cfg, area=area, day="week"),
        "日付から探せるようにしてあります。")


def tpl_tag(cfg, articles, tag, area=None):
    """タグ絞り込みのまとめ"""
    items = _filter(articles, area=area, tag=tag)
    if len(items) < 2:
        return None
    items.sort(key=lambda a: int(a.get("price") or 0))
    limit = (cfg.get("threads") or {}).get("max_items", 8)
    lines = [f'{yen(a.get("price"))}　{label_of(cfg, a)}（{a.get("area")}）'
             for a in _spread(items, limit)]
    scope = f"{area} " if area else ""
    return _compose(
        cfg, f'{scope}「{tag}」で絞った一覧 料金順',
        lines, _closer(cfg, len(items)),
        _url(cfg, area=area, tags=tag), f"該当{len(items)}名。")


def tpl_new(cfg, articles, area=None):
    """新着1件の告知（短文）"""
    items = _filter(articles, area=area)
    if not items:
        return None
    a = items[0]
    today = today_iso()
    dates = [d for d in (a.get("shift_dates") or []) if d >= today]
    tcfg = cfg.get("threads") or {}
    lines = [f'{label_of(cfg, a)}（{a.get("area")}）',
             f'出勤　{"・".join(fmt_date(d) for d in dates[:3]) or "調整中"}',
             f'料金　{yen(a.get("price"))}']
    # show_names=False のときは見出しがタグなので、重ねて出さない
    if a.get("tags") and (cfg.get("threads") or {}).get("show_names"):
        lines.append(f'タグ　{" / ".join(a["tags"][:5])}')
    return _compose(cfg, "レポート追加しました", lines,
                    _closer(cfg, int(a["id"][-2:] or 0)),
                    _article_url(cfg, a))


def _title_hook(title):
    """タイトルから【】と末尾ハッシュタグを除いた「引き」の部分を取り出す"""
    t = re.sub(r"【[^】]*】", "", title or "")
    t = re.sub(r"\s*#\S+", "", t)
    return t.strip()


def tpl_price(cfg, articles, area=None):
    """価格帯ごとの在籍数（参考アカウントの価格表型）"""
    items = _filter(articles, area=area)
    if len(items) < 5:
        return None
    dist = {}
    for a in items:
        dist[int(a.get("price") or 0)] = dist.get(int(a.get("price") or 0), 0) + 1
    rows = sorted(dist.items())
    width = max(v for _, v in rows)
    lines = [f'{yen(p)}　{"■" * max(1, round(n / width * 10))} {n}名'
             for p, n in rows if p]
    lo, hi = rows[0][0], rows[-1][0]
    return _compose(
        cfg, f'{area or "全エリア"} 料金帯ごとの在籍数',
        lines, _closer(cfg, len(items)), _url(cfg, area=area),
        f"{yen(lo)}〜{yen(hi)}。迷ったら{yen(rows[0][0])}帯から。")


def tpl_lineup(cfg, articles, area=None):
    """タグ別・カップ別の在籍数"""
    items = _filter(articles, area=area)
    if len(items) < 5:
        return None
    cups, plays = {}, {}
    for a in items:
        for t in (a.get("tags") or []):
            if t.endswith("カップ"):
                cups[t] = cups.get(t, 0) + 1
            elif re.fullmatch(r"[A-Z]{2,5}", t):
                plays[t] = plays.get(t, 0) + 1
    if not cups and not plays:
        return None
    lines = []
    if cups:
        top = sorted(cups.items(), key=lambda kv: -kv[1])[:6]
        lines.append("カップ　" + "・".join(f"{k}{v}名" for k, v in top))
    if plays:
        top = sorted(plays.items(), key=lambda kv: -kv[1])[:6]
        lines.append("タイプ　" + "・".join(f"{k}{v}名" for k, v in top))
    by_area = {}
    for a in items:
        by_area[a.get("area") or "その他"] = by_area.get(a.get("area") or "その他", 0) + 1
    lines.append("エリア　" + "・".join(
        f"{k}{v}名" for k, v in sorted(by_area.items(), key=lambda kv: -kv[1])[:6]))
    return _compose(
        cfg, f'{area or "全エリア"} 在籍{len(items)}名の内訳',
        lines, _closer(cfg, len(items)), _url(cfg, area=area))


def tpl_rank(cfg, articles, area=None):
    """販売実績の多い順（人気ランキング）"""
    items = [a for a in _filter(articles, area=area) if a.get("sales_count")]
    if len(items) < 3:
        return None
    items.sort(key=lambda a: -int(a.get("sales_count") or 0))
    limit = (cfg.get("threads") or {}).get("max_items", 8)
    lines = [f'{i}位　{label_of(cfg, a)}（{a.get("area")} / {yen(a.get("price"))}）'
             for i, a in enumerate(items[:limit], 1)]
    return _compose(
        cfg, f'{area or "全エリア"} よく読まれている順',
        lines, _closer(cfg, len(items)), _url(cfg, area=area),
        "販売数が多い＝満足度が高い、と見ています。")


def tpl_story(cfg, articles, area=None):
    """体験談を1本紹介する（最近出していないものから選ぶ）"""
    items = [a for a in _filter(articles, area=area) if _next_shift(a)]
    if not items:
        items = _filter(articles, area=area)
    if not items:
        return None
    used = _load_state().get("_threads_story", {})
    items.sort(key=lambda a: (used.get(str(a["id"]), ""), -int(a.get("sales_count") or 0)))
    a = items[0]
    hook = _title_hook(a["title"])
    dates = [d for d in (a.get("shift_dates") or []) if d >= today_iso()]
    lines = [" / ".join((a.get("tags") or [])[:3]) or a.get("area", ""), ""]
    if hook:
        lines.append(hook[:120])
        lines.append("")
    lines.append(f'出勤　{"・".join(fmt_date(d) for d in dates[:3]) or "調整中"}')
    lines.append(f'料金　{yen(a.get("price"))}')
    post = _compose(cfg, "体験談を1本", lines, _closer(cfg, int(a["id"][-2:] or 0)),
                    _article_url(cfg, a))
    post["story_id"] = str(a["id"])
    return post


def _pool_tpl(kind):
    def fn(cfg, articles, area=None):
        return tpl_pool(cfg, articles, area, kind=kind)
    return fn


TEMPLATES = {
    # 手書きストック（threads_pool.json）
    "aruaru": ("あるあるネタ（手書き）", _pool_tpl("aruaru")),
    "info": ("情報投稿（手書き）", _pool_tpl("info")),
    # ワクストの記事データから生成
    "today": ("本日出勤・料金順", tpl_today),
    "cheatsheet": ("目的別チートシート", tpl_cheatsheet),
    "week": ("今週の出勤まとめ", tpl_week),
    "price": ("料金帯ごとの在籍数", tpl_price),
    "lineup": ("在籍の内訳", tpl_lineup),
    "rank": ("よく読まれている順", tpl_rank),
    "story": ("体験談を1本", tpl_story),
    "new": ("新着1件", tpl_new),
}

# 手書きストックが尽きたときに代わりに出すテンプレート
POOL_FALLBACK = {"aruaru": "cheatsheet", "info": "price"}


def pick_for_slot(cfg, slot):
    """時間帯（"10"/"13"/"21"）に割り当てられたテンプレートを日替わりで選ぶ

    post_schedule の値をリストにしておくと、日ごとに順番に回る。
    文字列1つならその枠は常に同じテンプレートになる。
    """
    sched = (cfg.get("threads") or {}).get("post_schedule") or {}
    entry = sched.get(str(slot))
    if not entry:
        return None
    if isinstance(entry, str):
        return entry
    if not entry:
        return None
    doy = datetime.now(JST).timetuple().tm_yday
    return entry[doy % len(entry)]


DATA_TEMPLATES = [k for k in ("today", "cheatsheet", "week", "price",
                              "lineup", "rank", "story", "new")]


def build_all(cfg, articles, area=None, tag=None):
    """データ由来のテンプレートをまとめて生成（手書き枠は含めない）"""
    posts = []
    for key in DATA_TEMPLATES:
        label, fn = TEMPLATES[key]
        p = fn(cfg, articles, area)
        if p:
            p["template"] = key
            p["label"] = label
            posts.append(p)
    if tag:
        p = tpl_tag(cfg, articles, tag, area)
        if p:
            p["template"] = f"tag:{tag}"
            p["label"] = f"タグまとめ（{tag}）"
            posts.append(p)
    return posts


def main():
    ap = argparse.ArgumentParser(description="Threads/X 投稿文を生成する")
    ap.add_argument("--template", choices=list(TEMPLATES), help="1つだけ生成")
    ap.add_argument("--slot", help="時間帯から日替わりで選ぶ（例: 10 / 13 / 21）")
    ap.add_argument("--area", help="エリアで絞る（例: 新宿）")
    ap.add_argument("--tag", help="タグまとめも生成する（例: NN）")
    ap.add_argument("--json", action="store_true", help="JSONで出力")
    ap.add_argument("--post", action="store_true",
                    help="Threads APIで実際に投稿する（認証情報が無ければdry-run）")
    args = ap.parse_args()

    cfg = load_config()
    articles = load_articles(cfg)
    if not articles:
        print("記事データがありません（site_content/articles/ が空）")
        return

    template = args.template
    if not template and args.slot:
        template = pick_for_slot(cfg, args.slot)
        if not template:
            print(f"⏭️  スロット {args.slot} に割り当てがありません")
            return
        print(f"🎯 スロット {args.slot} → テンプレート「{template}」")

    if template:
        p = TEMPLATES[template][1](cfg, articles, args.area)
        if not p and template in POOL_FALLBACK:
            # 手書きストックが空なら、データ由来のテンプレートで埋める
            alt = POOL_FALLBACK[template]
            print(f"📭 「{template}」のストックが空 → 「{alt}」に切り替え")
            template = alt
            p = TEMPLATES[template][1](cfg, articles, args.area)
        posts = [dict(p, template=template,
                      label=TEMPLATES[template][0])] if p else []
    else:
        posts = build_all(cfg, articles, args.area, args.tag)

    if args.json:
        print(json.dumps(posts, ensure_ascii=False, indent=2))
        return

    if args.post:
        _publish(posts)
        return

    for p in posts:
        print("=" * 52)
        print(f'【{p.get("label", p["template"])}】  {len(p["text"])}文字')
        print("=" * 52)
        print(p["text"])
        if p["reply"]:
            print("\n--- リプライに置く投稿 ---")
            print(p["reply"])
        print()


STATE_FILE = "wakust_state.json"


def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def _save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _publish(posts):
    """Threads APIで投稿する。同じ日に同じテンプレートを二度投げない"""
    from wakust_threads_api import ThreadsClient, ThreadsError

    client = ThreadsClient()
    left, total = client.remaining_quota()
    if left is not None:
        print(f"本日の残り投稿数: {left} / {total}")
        if left <= 0:
            print("投稿上限に達しています。中止します。")
            return

    state = _load_state()
    posted = state.setdefault("_threads", {})
    today = datetime.now(JST).strftime("%Y-%m-%d")

    for p in posts:
        key = f'{today}:{p["template"]}'
        if key in posted:
            print(f'⏭️  投稿済みなのでスキップ: {p["template"]}')
            continue
        try:
            post_id, reply_id = client.post_with_reply(p["text"], p["reply"])
        except ThreadsError as e:
            print(f'❌ 投稿失敗 [{p["template"]}]: {e}')
            continue
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        posted[key] = {"post_id": post_id, "reply_id": reply_id, "at": now}
        # 同じ手書きネタ・同じ体験談が続かないよう使用日を記録する
        if p.get("pool_key"):
            state.setdefault("_threads_pool", {})[p["pool_key"]] = now
        if p.get("story_id"):
            state.setdefault("_threads_story", {})[p["story_id"]] = now
        _save_state(state)


if __name__ == "__main__":
    main()
