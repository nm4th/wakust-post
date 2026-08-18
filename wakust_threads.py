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


def today_iso(offset=0):
    return (datetime.now(JST).date() + timedelta(days=offset)).isoformat()


def yen(v):
    try:
        return f"¥{int(v):,}"
    except (TypeError, ValueError):
        return "―"


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
    return {
        "text": "\n".join(body).strip(),
        "reply": reply,
        "url": reply_url,
    }


# ============================================================
# テンプレート
# ============================================================
def tpl_today(cfg, articles, area=None):
    """本日出勤を料金順に並べる（参考投稿の「価格（安→高）」型）"""
    day = today_iso()
    items = _filter(articles, area=area, day=day)
    if not items:
        return None
    items.sort(key=lambda a: int(a.get("price") or 0))
    limit = (cfg.get("threads") or {}).get("max_items", 8)
    label = area or "全エリア"
    lines = [f'{yen(a.get("price"))}　{display_name(a["title"])}（{a.get("area")}）'
             for a in items[:limit]]
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
    add("直近の出勤が早いのは",
        sorted([a for a in items if a.get("shift_dates")],
               key=lambda a: a["shift_dates"][0]),
        lambda a: fmt_date(a["shift_dates"][0]))
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
        lines.append(f'・{label}{pad}　→　{display_name(a["title"])}{tail}')
    return _compose(
        cfg, f'{area or "全エリア"} 目的別チートシート',
        lines, _closer(cfg, len(items)), _url(cfg, area=area))


def tpl_week(cfg, articles, area=None):
    """1週間の出勤カレンダー"""
    items = _filter(articles, area=area)
    if not items:
        return None
    lines = []
    for i in range(7):
        d = today_iso(i)
        names = [display_name(a["title"]) for a in items
                 if d in (a.get("shift_dates") or [])]
        if not names:
            continue
        shown = "・".join(names[:6])
        more = f" 他{len(names) - 6}名" if len(names) > 6 else ""
        lines.append(f"{fmt_date(d)}　{shown}{more}")
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
    lines = [f'{yen(a.get("price"))}　{display_name(a["title"])}（{a.get("area")}）'
             for a in items[:limit]]
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
    dates = a.get("shift_dates") or []
    tcfg = cfg.get("threads") or {}
    lines = [f'{display_name(a["title"])}（{a.get("area")}）',
             f'出勤　{"・".join(fmt_date(d) for d in dates[:3]) or "調整中"}',
             f'料金　{yen(a.get("price"))}']
    if a.get("tags"):
        lines.append(f'タグ　{" / ".join(a["tags"][:5])}')
    return _compose(cfg, "レポート追加しました", lines,
                    _closer(cfg, int(a["id"][-2:] or 0)),
                    _article_url(cfg, a))


TEMPLATES = {
    "today": ("本日出勤・料金順", tpl_today),
    "cheatsheet": ("目的別チートシート", tpl_cheatsheet),
    "week": ("今週の出勤まとめ", tpl_week),
    "new": ("新着1件", tpl_new),
}


def build_all(cfg, articles, area=None, tag=None):
    posts = []
    for key, (label, fn) in TEMPLATES.items():
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

    if args.template:
        fn = TEMPLATES[args.template][1]
        p = fn(cfg, articles, args.area)
        posts = [dict(p, template=args.template)] if p else []
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
        posted[key] = {"post_id": post_id, "reply_id": reply_id,
                       "at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")}
        _save_state(state)


if __name__ == "__main__":
    main()
