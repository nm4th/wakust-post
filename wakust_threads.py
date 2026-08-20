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

import sys
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
    link_target="none"   … リプライにURLを貼らない（プロフィール誘導のみ）

    "none" は、リンク先ごと判定されて投稿が削除される場合の対応。
    別ドメインを噛ませて遷移先を隠すのは規約回避であり、見つかれば
    アカウント停止になるので取らない。貼らないことで対応する。
    """
    tcfg = cfg.get("threads") or {}
    if tcfg.get("link_target") == "none":
        return ""
    if tcfg.get("link_target") == "wakust":
        return (tcfg.get("wakust_landing_url") or "").strip()
    base = cfg["base_url"] + "/"
    q = "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v)
    return base + ("?" + q if q else "")


def _list_link(cfg, name, **params):
    """一覧系テンプレートの誘導先。cta_only_templates に入れたものは
    リプライにURLを貼らず、本文末尾のプロフィール誘導だけで引く"""
    if name in ((cfg.get("threads") or {}).get("cta_only_templates") or []):
        return ""
    return _url(cfg, **params)


def _article_url(cfg, a):
    """記事1件の誘導先URL"""
    tcfg = cfg.get("threads") or {}
    if tcfg.get("link_target") == "none":
        return ""
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
    entries = []
    for e in (load_pool().get(kind) or []):
        # 文字列そのままか、{"text": ..., "cta": false} の形を受け付ける
        if isinstance(e, str):
            entries.append({"text": e, "cta": True})
        elif isinstance(e, dict) and (e.get("text") or "").strip():
            entries.append({"text": e["text"], "cta": e.get("cta", True)})
    entries = [e for e in entries if e["text"].strip()]
    if not entries:
        return None
    used = _load_state().get("_threads_pool", {})
    # 未使用（""）が最優先、次に使用日が古い順
    entries.sort(key=lambda e: used.get(_pool_key(kind, e["text"]), ""))
    entry = entries[0]
    text = entry["text"].strip()
    tcfg = cfg.get("threads") or {}
    cta = (tcfg.get("profile_cta") or "").strip()
    # 独自の締め（「いいねだけ押してくれ」等）を持つ投稿には付け足さない
    if entry["cta"] and cta and cta not in text:
        text = f"{text}\n\n{cta}"
    return {"text": text, "reply": "", "url": "",
            "pool_key": _pool_key(kind, entry["text"]),
            "variant": _pool_key(kind, entry["text"]).split(":", 1)[1]}


def tpl_today(cfg, articles, area=None):
    """本日出勤を並べる。料金は出さない"""
    day = today_iso()
    items = _filter(articles, area=area, day=day)
    if not items:
        return None
    limit = (cfg.get("threads") or {}).get("max_items", 8)
    label = area or "全エリア"
    # 料金は出さないが、選ぶときは価格帯全体から散らす（同じ層ばかりにしない）。
    # あわせて1駅あたりの件数に上限を設けて新宿だけにならないようにする
    items.sort(key=lambda a: int(a.get("price") or 0))
    spread = _spread(items, limit * 3)
    per_station = 2
    picks, used_station = [], {}
    for a in spread:
        st = a.get("station") or a.get("area") or ""
        if used_station.get(st, 0) >= per_station:
            continue
        used_station[st] = used_station.get(st, 0) + 1
        picks.append(a)
        if len(picks) >= limit:
            break
    for a in spread:
        if len(picks) >= limit:
            break
        if a not in picks:
            picks.append(a)

    seen, lines = set(), []
    for a in picks:
        tags = [t for t in (a.get("tags") or []) if not t.endswith("カップ")
                and t != a.get("station")]
        cup = next((t for t in (a.get("tags") or []) if t.endswith("カップ")), "")
        detail = " / ".join(x for x in [cup] + tags[:2] if x) or a.get("area", "")
        row = f'【{a.get("station") or a.get("area")}】{detail}'
        if row in seen:
            continue
        seen.add(row)
        lines.append(row)

    d = datetime.now(JST).date()
    note = f"本日は{len(items)}名が出勤。"
    return _compose(
        cfg,
        f'{label} 本日{d.month}/{d.day}({WEEKDAY_JP[d.weekday()]})出勤',
        lines, _closer(cfg, d.day),
        _list_link(cfg, "today", area=area, day="today"), note)


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
             f'出勤　{"・".join(fmt_date(d) for d in dates[:3]) or "調整中"}']
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


_QUOTE_RE = re.compile(r"「([^」]{4,40})」")
_PUNCH_RE = re.compile(r"^([^。！!]{4,24}[！!])")


def _title_lead(title):
    """タイトルを「引き」と「残り」に分ける

    タイトルは自分で書いた宣伝文なので、そのまま使える素材が入っている。
      「このGカップ、本物だ…」  ← 会話の引用があればそれが一番強い
      神乳PZで絶頂！             ← 無ければ冒頭の惹句を使う
    戻り値: (引き, 残りの説明)
    """
    body = _title_hook(title)
    m = _QUOTE_RE.search(body)
    if m:
        rest = (body[:m.start()] + " " + body[m.end():]).strip(" 　、。")
        return f"「{m.group(1)}」", re.sub(r"\s+", " ", rest)
    m = _PUNCH_RE.match(body)
    if m:
        return m.group(1), body[m.end():].strip(" 　、。")
    return "", body


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
    lines = [f'{i}位　{label_of(cfg, a)}（{a.get("area")}）'
             for i, a in enumerate(items[:limit], 1)]
    return _compose(
        cfg, f'{area or "全エリア"} よく読まれている順',
        lines, _closer(cfg, len(items)), _url(cfg, area=area),
        "販売数が多い＝満足度が高い、と見ています。")


def tpl_spec(cfg, articles, area=None):
    """本日ワクストに公開された記事を、スペック表の形で紹介する

    今日公開された記事が無ければ None を返すので、その日は別のテンプレートが出る。
    """
    today = today_iso()
    items = [a for a in _filter(articles, area=area)
             if (a.get("published_at") or "")[:10] == today]
    if not items:
        return None
    items.sort(key=lambda a: -int(a.get("sales_count") or 0))
    a = items[0]

    tags = a.get("tags") or []
    cup = next((t for t in tags if t.endswith("カップ")), "")
    plays = [t for t in tags if re.fullmatch(r"[A-Z]{2,5}", t)]
    dates = [d for d in (a.get("shift_dates") or []) if d >= today]

    state = _load_state()
    no = int(state.get("_threads_spec_no", 0)) + 1

    place = a.get("station") or a.get("area") or ""
    if a.get("station") and a.get("area") and a["station"] != a["area"]:
        place = f'{a["station"]}（{a["area"]}）'

    lines = [f"No.{no}", ""]
    lead, rest = _title_lead(a["title"])
    for x in (lead, rest[:70] if rest else ""):
        if x:
            lines += [x, ""]
    lines.append(f"エリア: {place}")
    prof = "、".join(x for x in [cup, " / ".join(plays[:3])] if x)
    if prof:
        lines.append(f"セラピスト: {prof}")
    if dates:
        lines.append(f'出勤: {"・".join(fmt_date(d) for d in dates[:3])}')

    tcfg = cfg.get("threads") or {}
    closer = (tcfg.get("spec_closer") or "本日公開しました。続きは記事で。").strip()
    post = _compose(cfg, "", lines, closer, _article_url(cfg, a))
    post["text"] = post["text"].lstrip("\n")
    post["spec_no"] = no
    post["story_id"] = str(a["id"])
    return post


def tpl_pinned(cfg, articles, area=None):
    """固定ポストの本体

    貼りっぱなしにするので、**時間が経っても古くならない内容だけ**を書く。
    在籍数や本命リストのような日々変わるものは入れない（毎回変えると
    投稿ごとに約束の中身がズレて、固定ポストが何の場所か分からなくなる）。
    変わるものは、着地先のワクストのプロフィールが毎日更新してくれる。

    リンク先はワクストの体験談一覧なので、「レポートが読める場所」だと
    分かる書き方にしておく。他の投稿の締め（profile_cta / pickup_tails）も
    同じ約束に揃えてある。
    """
    tcfg = cfg.get("threads") or {}
    url = (tcfg.get("pinned_link") or tcfg.get("wakust_landing_url") or "").strip()
    areas = [a.get("area") for a in articles if a.get("area")]
    # エリア名の一覧だけ使う。人数は日々変わるので出さない
    order = ["東京都内", "神奈川", "埼玉", "多摩", "千葉"]
    known = [a for a in order if a in areas] or sorted(set(areas))

    lines = ["【保存版】まずここだけ読んでください📌", "",
             "行ってきた人の体験レポートを毎日書いてます。",
             "写真と紹介文だけじゃ分からないところを、",
             "実際どうだったかで残してます。", "",
             "読み方はこの順番で。", "",
             "① エリアを1つに絞る",
             f"　{' / '.join(known)}",
             "② 気になった子のレポートを2〜3本まとめて読む",
             "　1本だけだと、相性の当たり外れか実力か分からない",
             "③ 出勤日から逆算して予約する",
             "　当日枠が埋まってる子＝人気の裏付け",
             "", "----", "",
             "レポートは毎日増えていきます。"]
    if url:
        lines += ["ぜんぶここから読めます →", url]
    return {"text": "\n".join(lines).strip(), "reply": "", "url": url}


def pickup_targets(articles, min_items=4):
    """厳選投稿の対象一覧を作る

    記事が min_items 以上ある駅は駅単体で、エリアはエリアとして対象にする。
    東京都内のように広いエリアでも、新宿・池袋のような駅単位の投稿が回る。
    戻り値: [("station", "新宿"), ("area", "神奈川"), ...]
    """
    st_counts, area_counts = {}, {}
    for a in articles:
        st = a.get("station") or ""
        ar = a.get("area") or "その他"
        if st:
            st_counts[st] = st_counts.get(st, 0) + 1
        area_counts[ar] = area_counts.get(ar, 0) + 1
    targets = [("station", k) for k, v in sorted(st_counts.items()) if v >= min_items]
    targets += [("area", k) for k, v in sorted(area_counts.items())
                if v >= min_items and k != "その他"]
    return targets


def tpl_pickup(cfg, articles, area=None, station=None, rotate=0):
    """エリア厳選型（【駅名】+ 特徴 のリスト＋出し惜しみで締める）

    参考アカウントで最も反応が取れている型。店名の代わりに記事を並べる。
    「一番の本命はここには書いてない」でプロフィールへ引くのが肝なので、
    リプライにURLは付けず、締めの一文だけで誘導する。
    """
    tcfg = cfg.get("threads") or {}
    min_items = int(tcfg.get("pickup_min_items") or 4)

    # 対象は日替わりで回す。東京都内のような広いエリアは駅ごとに分けたいので、
    # 記事が十分ある駅は駅単体を、エリアはエリアとして、それぞれ対象に入れる。
    if station:
        items = [a for a in articles if (a.get("station") or "") == station]
        label = station
    elif area:
        items = _filter(articles, area=area)
        label = area
    else:
        targets = pickup_targets(articles, min_items)
        if not targets:
            return None
        # rotate は「同じ日に2回出すときに別の駅を選ぶ」ためのずらし幅。
        # 1日1回だけなら0のままで従来どおり日替わりで回る
        idx = (datetime.now(JST).timetuple().tm_yday + int(rotate)) % len(targets)
        kind, label = targets[idx]
        if kind == "station":
            items = [a for a in articles if (a.get("station") or "") == label]
            station = label
        else:
            items = _filter(articles, area=label)
            area = label

    if len(items) < min_items:
        return None

    # 「外しにくい」= 販売実績が多いもの。実績順に取りつつ、
    # 同じ駅ばかりにならないよう1駅あたりの上限を設ける
    limit = min(10, max(5, (tcfg.get("max_items") or 8) + 2))
    ranked = sorted(items, key=lambda a: -int(a.get("sales_count") or 0))
    n_st = len({a.get("station") for a in items})
    per_station = limit if station else max(2, -(-limit // max(1, n_st)))
    picks, used_station = [], {}
    for a in ranked:
        st = a.get("station") or label
        if used_station.get(st, 0) >= per_station:
            continue
        used_station[st] = used_station.get(st, 0) + 1
        picks.append(a)
        if len(picks) >= limit:
            break
    # 上限で弾かれて件数が足りない場合は実績順で埋める
    for a in ranked:
        if len(picks) >= limit:
            break
        if a not in picks:
            picks.append(a)
    # 駅単体は出勤日順に並べる。同じ「Fカップ / GHR」が並んでも
    # 日付が昇順なら重複ではなくスケジュールとして読める
    if station:
        picks.sort(key=lambda a: _next_shift(a) or "9999-12-31")

    # 駅が散っていれば駅ごとにまとめる（参考投稿の《エリア》グルーピング）
    by_station = {}
    for a in picks:
        by_station.setdefault(a.get("station") or area, []).append(a)

    def row(a):
        tags = [t for t in (a.get("tags") or []) if not t.endswith("カップ")
                and t != a.get("station")]
        cup = next((t for t in (a.get("tags") or []) if t.endswith("カップ")), "")
        detail = " / ".join(x for x in [cup] + tags[:2] if x) or label
        if station:
            # 駅単体では【駅名】が全行同じになるので付けない。代わりに
            # 「Gカップ / HR」が並んで見分けが付かなくなるため出勤日を添える
            nxt = _next_shift(a)
            return f"{detail}　{fmt_date(nxt)}" if nxt else detail
        return f'【{a.get("station") or label}】{detail}'

    # 見出しは2件以上ある駅だけに立てる。1件だけの駅は最後にまとめて並べる
    # （1行の《駅》が並ぶと、かえって読みにくくなるため）
    multi = {} if station else {st: g for st, g in by_station.items() if len(g) >= 2}
    singles = [a for st, g in by_station.items() if len(g) < 2 for a in g]
    seen_rows = set()

    def rows(group):
        out = []
        for a in group:
            r = row(a)
            if r in seen_rows:
                continue
            seen_rows.add(r)
            out.append(r)
        return out

    lines = []
    if len(multi) >= 2:
        for st, group in sorted(multi.items(), key=lambda kv: -len(kv[1])):
            block = rows(group)
            if not block:
                continue
            lines.append(f"《{st}》")
            lines.extend(block)
            lines.append("")
        block = rows(singles)
        if block:
            lines.append("《その他のエリア》")
            lines.extend(block)
            lines.append("")
        if lines and not lines[-1]:
            lines.pop()
    else:
        lines = rows(picks)

    heads = tcfg.get("pickup_heads") or ["【初心者必見🔰】\n{area}で迷ってるなら、この{n}人から選べば外しにくい"]
    tails = tcfg.get("pickup_tails") or ["ちなみに{area}の一番の本命は、あえてここには書いてない。"]
    seed = datetime.now(JST).timetuple().tm_yday
    head = render_head(heads[seed % len(heads)], area=label, n=len(picks))
    tail = render_head(tails[seed % len(tails)], area=label, n=len(picks))
    cta = (tcfg.get("profile_cta") or "詳細は固定ポストに置いてます📌").strip()
    # 締めの一文が既に固定ポストへ誘導しているなら、共通CTAは足さない
    # （「固定ポストからまとめて読めます」が2行続いてしまうため）
    if "固定ポスト" in tail:
        cta = ""

    sep = tcfg.get("separator", "----")
    body = [head, ""] + lines + ["", sep, "",
                                 "正直、初回はこの中から選ぶだけで失敗率がグッと下がります。",
                                 "", tail, cta]
    # variant は「同じテンプレートでも中身が別」を表す印。
    # これが違えば同じ日に2回投稿できる（_publish の重複判定で使う）
    return {"text": "\n".join(body).strip(), "reply": "", "url": "",
            "variant": label}


def tpl_flash(cfg, articles, area=None):
    """体験速報型（短文＋反応を煽る）

    実際に行っていない体験を自動生成すると、コメントで具体的に聞かれた時に
    破綻する。そこで「レポートを追加した」という事実だけを速報の形にし、
    煽りの構造（結果を先に出して続きを引く）だけを借りている。
    """
    items = [a for a in _filter(articles, area=area) if _next_shift(a)]
    if not items:
        return None
    used = _load_state().get("_threads_story", {})
    # 販売実績が高いものを優先しつつ、最近出したものは後回し
    items.sort(key=lambda a: (used.get(str(a["id"]), ""),
                              -int(a.get("sales_count") or 0)))
    a = items[0]
    tags = a.get("tags") or []
    play = next((t for t in tags if re.fullmatch(r"[A-Z]{2,5}", t)), "")
    dates = [d for d in (a.get("shift_dates") or []) if d >= today_iso()]

    tcfg = cfg.get("threads") or {}
    heads = tcfg.get("flash_heads") or ["レポート上げました"]
    hooks = tcfg.get("flash_hooks") or ["いいね多かったら次も出します"]
    seed = int(a["id"][-3:] or 0)

    head = render_head(heads[seed % len(heads)],
                       area=a.get("area", ""), play=play or "当たり")
    lines = [head, ""]
    lines.append(" / ".join(tags[:3]) or a.get("area", ""))
    lines.append("出勤 " + ("・".join(fmt_date(d) for d in dates[:2]) or "調整中"))
    post = _compose(cfg, "", lines, hooks[seed % len(hooks)],
                    _article_url(cfg, a))
    # 1行目のタイトル枠は使わないので、先頭の空行を落とす
    post["text"] = post["text"].lstrip("\n")
    post["story_id"] = str(a["id"])
    post["variant"] = str(a["id"])
    return post


def render_head(tpl, **kw):
    """速報の1行目テンプレートに値を差し込む"""
    out = tpl
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


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
    lead, rest = _title_lead(a["title"])
    dates = [d for d in (a.get("shift_dates") or []) if d >= today_iso()]
    tcfg = cfg.get("threads") or {}
    seed = int(a["id"][-3:] or 0)

    # 引きを1行目に置く。引用が取れなければ煽り文で始める
    heads = tcfg.get("story_heads") or ["これは書いておきたい。"]
    head = lead or render_head(heads[seed % len(heads)],
                               area=a.get("station") or a.get("area", ""),
                               play=next((t for t in (a.get("tags") or [])
                                          if re.fullmatch(r"[A-Z]{2,5}", t)), "当たり"))

    lines = [head, ""]
    lines.append(" / ".join((a.get("tags") or [])[:3]) or a.get("area", ""))
    lines.append(f'出勤　{"・".join(fmt_date(d) for d in dates[:3]) or "調整中"}')
    if rest:
        lines += ["", rest[:110]]

    closers = tcfg.get("story_closers") or ["続きは記事に全部書きました。"]
    post = _compose(cfg, "", lines, closers[seed % len(closers)],
                    _article_url(cfg, a))
    post["text"] = post["text"].lstrip("\n")
    post["story_id"] = str(a["id"])
    post["variant"] = str(a["id"])
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
    "today": ("本日出勤", tpl_today),
    "cheatsheet": ("目的別チートシート", tpl_cheatsheet),
    "week": ("今週の出勤まとめ", tpl_week),
    "price": ("料金帯ごとの在籍数", tpl_price),
    "lineup": ("在籍の内訳", tpl_lineup),
    "rank": ("よく読まれている順", tpl_rank),
    "pinned": ("固定ポスト（貼りっぱなし）", tpl_pinned),
    "spec": ("本日公開のスペック表", tpl_spec),
    "pickup": ("エリア厳選（出し惜しみ型）", tpl_pickup),
    "flash": ("体験速報（反応を煽る）", tpl_flash),
    "story": ("体験談を1本", tpl_story),
    "new": ("新着1件", tpl_new),
}

# 手書きストックが尽きたときに代わりに出すテンプレート
# 手書きストックが尽きたときの代役。
# cheatsheet / price は料金を出すので使わない（投稿に料金は載せない方針）
POOL_FALLBACK = {"aruaru": "lineup", "info": "lineup"}


def _pick_priority(cfg, articles, area=None):
    """ローテーションより先に出したいテンプレートを選ぶ

    spec（本日公開の記事）のように「該当がある日は必ず出したい」ものを
    ここで拾う。該当が無ければ None を返してローテーションに任せる。
    その日すでに投稿済みのものは対象外。
    """
    names = (cfg.get("threads") or {}).get("priority_templates") or []
    today = datetime.now(JST).strftime("%Y-%m-%d")
    posted = _load_state().get("_threads", {})
    for name in names:
        if name not in TEMPLATES or f"{today}:{name}" in posted:
            continue
        if TEMPLATES[name][1](cfg, articles, area):
            return name
    return None


def pick_for_slot(cfg, slot):
    """時間帯（"10"/"13"/"21"）に割り当てられたテンプレートを日替わりで選ぶ

    post_schedule の値の書き方は3通り。

      "10": "today"                        … その枠は常に today
      "21": ["info", "aruaru"]             … 日ごとに順番に回る
      "15": {"template": "pickup",         … オプション付き
             "rotate": 7}                     （同じ日の別枠と対象をずらす）

    戻り値は (テンプレート名, オプション辞書)。割り当てが無ければ (None, {})。
    """
    sched = (cfg.get("threads") or {}).get("post_schedule") or {}
    entry = sched.get(str(slot))
    if isinstance(entry, list):
        if not entry:
            return None, {}
        doy = datetime.now(JST).timetuple().tm_yday
        entry = entry[doy % len(entry)]
    if isinstance(entry, dict):
        opts = dict(entry)
        name = opts.pop("template", None)
        return (name or None), opts
    if isinstance(entry, str) and entry:
        return entry, {}
    return None, {}


DATA_TEMPLATES = [k for k in ("today", "cheatsheet", "week", "price",
                              "lineup", "rank", "pickup", "spec", "flash",
                              "story", "new")]


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
    ap.add_argument("--area", help="エリアで絞る（例: 神奈川）")
    ap.add_argument("--station", help="駅で絞る（例: 池袋）。pickupのみ")
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
    opts = {}
    if not template and args.slot:
        template = _pick_priority(cfg, articles, args.area)
        if template:
            print(f"⭐ 優先テンプレート「{template}」を採用（本日分の該当あり）")
        else:
            template, opts = pick_for_slot(cfg, args.slot)
            if not template:
                print(f"⏭️  スロット {args.slot} に割り当てがありません")
                return
            detail = f"（{opts}）" if opts else ""
            print(f"🎯 スロット {args.slot} → テンプレート「{template}」{detail}")

    if template:
        if template == "pickup":
            p = tpl_pickup(cfg, articles,
                           args.area or opts.get("area"),
                           args.station or opts.get("station"),
                           rotate=opts.get("rotate", 0))
        else:
            p = TEMPLATES[template][1](cfg, articles, args.area)
        if not p and template in POOL_FALLBACK:
            # 手書きストックが空なら、データ由来のテンプレートで埋める
            alt = POOL_FALLBACK[template]
            print(f"📭 「{template}」のストックが空 → 「{alt}」に切り替え")
            template = alt
            p = TEMPLATES[template][1](cfg, articles, args.area)
        # スロット側で代役を指定できる。spec のように「該当がある日だけ出す」
        # テンプレートで、枠を空振りさせないために使う
        alt = opts.get("fallback")
        if not p and alt and alt in TEMPLATES:
            print(f"📭 「{template}」は該当なし → 「{alt}」に切り替え")
            template = alt
            p = TEMPLATES[template][1](cfg, articles, args.area)
        if not p:
            print(f"⏭️  「{template}」は今回該当なし（投稿するものがありません）")
            return
        posts = [dict(p, template=template, label=TEMPLATES[template][0])]
    else:
        posts = build_all(cfg, articles, args.area, args.tag)

    if args.json:
        print(json.dumps(posts, ensure_ascii=False, indent=2))
        return

    if args.post:
        # 失敗をここで握りつぶすと、トークン失効に誰も気づけないまま
        # 投稿が止まり続ける。異常終了させてワークフローの通知に乗せる
        sys.exit(_publish(posts))

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
    """Threads APIで投稿する。同じ日に同じテンプレートを二度投げない

    戻り値は終了コード。0=成功、1=投稿失敗あり、2=トークンが無効。
    トークン失効は「切れたら延長できない」ので、ログに出すだけでは足りない。
    必ず非ゼロで終わらせて GitHub Actions の失敗通知に乗せる。
    """
    from wakust_threads_api import ThreadsClient, ThreadsError, ThreadsAuthError

    client = ThreadsClient()
    try:
        left, total = client.remaining_quota()
    except ThreadsAuthError as e:
        return _token_dead(e)
    if left is not None:
        print(f"本日の残り投稿数: {left} / {total}")
        if left <= 0:
            print("投稿上限に達しています。中止します。")
            return 0

    state = _load_state()
    posted = state.setdefault("_threads", {})
    today = datetime.now(JST).strftime("%Y-%m-%d")
    failed = 0

    for p in posts:
        # 同じテンプレートでも中身（駅・記事・ネタ）が違えば別物として投稿する。
        # variant が無いものは従来どおり1日1回まで
        variant = str(p.get("variant") or "")
        key = f'{today}:{p["template"]}' + (f":{variant}" if variant else "")
        if key in posted:
            print(f'⏭️  投稿済みなのでスキップ: {p["template"]}')
            continue
        try:
            post_id, reply_id = client.post_with_reply(p["text"], p["reply"])
        except ThreadsAuthError as e:
            return _token_dead(e)
        except ThreadsError as e:
            print(f'❌ 投稿失敗 [{p["template"]}]: {e}')
            _annotate(f'Threads投稿に失敗しました [{p["template"]}]: {e}')
            failed += 1
            continue
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        posted[key] = {"post_id": post_id, "reply_id": reply_id, "at": now}
        # 同じ手書きネタ・同じ体験談が続かないよう使用日を記録する
        if p.get("pool_key"):
            state.setdefault("_threads_pool", {})[p["pool_key"]] = now
        if p.get("story_id"):
            state.setdefault("_threads_story", {})[p["story_id"]] = now
        if p.get("spec_no"):
            state["_threads_spec_no"] = p["spec_no"]
        if p["template"] == "pinned":
            # あとで返信を貼り替えられるよう、固定ポストのIDを覚えておく
            state.setdefault("_threads_pinned", {})["post_id"] = post_id
            print(f"📌 固定ポストのIDを記録しました: {post_id}\n"
                  f"   Threadsアプリでこの投稿をピン留めしてください")
        _save_state(state)
    return 1 if failed else 0


def _annotate(message, level="error"):
    """GitHub Actions のログに注釈として出す（メール本文にも載る）"""
    print(f"::{level}::{message}")


def _token_dead(err):
    """トークン失効時の共通処理。ワークフローを必ず失敗させる"""
    print(f"❌ {err}")
    _annotate(
        "Threadsのアクセストークンが失効しました。投稿は止まっています。"
        "Metaダッシュボードでトークンを再発行し、"
        "Secrets の THREADS_ACCESS_TOKEN を更新してください "
        "（長期トークンは60日で失効し、切れた後は延長できません）")
    return 2


if __name__ == "__main__":
    main()
