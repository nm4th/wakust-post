"""自社サイト（静的サイト）ジェネレーター

site_content/articles/*.json を読み込み、codoc の貼り付けタグを埋め込んだ
記事ページ一式を site/ に出力する。

  python wakust_site.py

サイトの主目的は「探しやすさ」。一覧ページはキーワード検索・出勤日・エリア・
タグでの絞り込みと並び替えができる。JavaScriptはDOM上のカードを出し分ける
だけなので、JSを切っていても全記事が見える（＝クローラも全記事を読める）。

出力物:
  site/index.html               記事一覧（検索・絞り込み）
  site/articles/{id}/           記事ページ（無料部分 + codocペイウォール）
  site/area/{slug}/             エリア別一覧（SEO用）
  site/tag/{slug}/              タグ別一覧（SEO用）
  site/tokushoho.html           特定商取引法に基づく表記
  site/privacy.html             プライバシーポリシー
  site/assets/style.css
  site/sitemap.xml, robots.txt, .nojekyll

有料部分の本文は「サイト側には出力しない」。codoc の entry に保存され、
購入後に codoc のスクリプトが差し込む。静的HTMLに有料本文を置くと
ソースを見るだけで読めてしまうため、この分離は必須。
"""

import os
import re
import json
import glob
import html
import shutil
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

log = logging.getLogger("wakust.site")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

JST = timezone(timedelta(hours=9))
CONFIG_FILE = "site_config.json"
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

# エリア名 → URLスラッグ（日本語ディレクトリを避けたいので固定表を持つ）
AREA_SLUGS = {
    "東京都内": "tokyo", "東京都": "tokyo", "新宿": "shinjuku", "池袋": "ikebukuro",
    "神奈川": "kanagawa", "神奈川県": "kanagawa", "埼玉": "saitama",
    "埼玉県": "saitama", "千葉": "chiba", "千葉県": "chiba", "多摩": "tama",
    "その他": "other",
}
# 一覧に最初から出すタグの数（残りは「すべてのタグ」で展開）
TAG_CHIP_LIMIT = 24


# ============================================================
# テンプレート補助（CSS/JSの波括弧と衝突しないよう {{key}} 形式）
# ============================================================
def render(tpl, **kw):
    out = tpl
    for k, v in kw.items():
        out = out.replace("{{" + k + "}}", "" if v is None else str(v))
    return out


def esc(s):
    return html.escape(str(s or ""), quote=True)


# ファイル名に使えない文字だけを落とす（日本語はそのまま残す）
_UNSAFE_PATH = re.compile(r'[/\\?%*:|"<>\x00-\x1f]')


def slugify(name, table=None):
    """ディレクトリ名に使うスラッグ。英数字は小文字化、日本語はそのまま残す。

    ディレクトリ名は生のUTF-8にしておく必要がある。パーセントエンコードした
    名前でディレクトリを作ると、サーバーがURLをデコードしてから探すため
    404になる（/tag/%E5%B7%A8%E4%B9%B3/ → 巨乳 を探しに行く）。
    """
    if table and name in table:
        return table[name]
    s = _UNSAFE_PATH.sub("", (name or "").strip())
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]*", s):
        return re.sub(r"[ _]+", "-", s).lower()
    return s or "other"


def url_slug(name, table=None):
    """リンク・canonical用にスラッグをパーセントエンコードする"""
    from urllib.parse import quote
    return quote(slugify(name, table), safe="")


# ============================================================
# 無料部分HTMLのクリーンアップ
# ============================================================
# ワクスト側の回遊リンク（wakust.com へのリンク）は自社サイトには出さない
_STRIP_BLOCKS = [
    ("<!-- related_posts_start -->", "<!-- related_posts_end -->"),
    ("<!-- related_next_posts_start -->", "<!-- related_next_posts_end -->"),
]
_SCRIPT_RE = re.compile(r"<\s*(script|iframe|object|embed)\b.*?<\s*/\s*\1\s*>",
                        re.I | re.S)
_SELF_CLOSING_RE = re.compile(r"<\s*(script|iframe|object|embed)\b[^>]*/?>", re.I)
_ON_ATTR_RE = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_JS_HREF_RE = re.compile(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.I)


def clean_free_html(raw):
    """ワクストの無料部分HTMLを自社サイト用に整える"""
    if not raw:
        return ""
    text = html.unescape(raw)
    for start, end in _STRIP_BLOCKS:
        while True:
            i = text.find(start)
            if i < 0:
                break
            j = text.find(end, i)
            if j < 0:
                text = text[:i]
                break
            text = text[:i] + text[j + len(end):]
    text = _SCRIPT_RE.sub("", text)
    text = _SELF_CLOSING_RE.sub("", text)
    text = _ON_ATTR_RE.sub("", text)
    text = _JS_HREF_RE.sub(r'\1="#"', text)
    return text.strip()


def plain_text(html_str, limit=120):
    """メタディスクリプション用にHTMLからテキストを抜く"""
    txt = re.sub(r"<[^>]+>", " ", html.unescape(html_str or ""))
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:limit]


# ============================================================
# 設定・記事データ
# ============================================================
def load_config(path=CONFIG_FILE):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 環境変数で上書き（GitHub Actions から差し込む用）
    if os.environ.get("SITE_BASE_URL"):
        cfg["base_url"] = os.environ["SITE_BASE_URL"]
    if os.environ.get("CODOC_USERCODE"):
        cfg.setdefault("codoc", {})["usercode"] = os.environ["CODOC_USERCODE"]
    cfg["base_url"] = (cfg.get("base_url") or "").rstrip("/")
    # base_url にパスが含まれる場合（GitHub Pages のプロジェクトサイト）に対応
    m = re.match(r"^https?://[^/]+(/.*)$", cfg["base_url"])
    cfg["base_path"] = (m.group(1).rstrip("/") if m else "")
    return cfg


def load_articles(cfg):
    """site_content/articles/*.json を読み込む（公開日時の新しい順）"""
    articles = []
    for path in sorted(glob.glob(os.path.join(cfg["content_dir"], "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                a = json.load(f)
        except (OSError, ValueError) as e:
            log.warning(f"⚠️ 記事JSON読み込み失敗 {path}: {e}")
            continue
        if not a.get("id") or not a.get("title"):
            log.warning(f"⚠️ id/title のない記事JSONをスキップ: {path}")
            continue
        if a.get("hidden"):
            continue
        a.setdefault("area", a.get("category") or "その他")
        a.setdefault("tags", [])
        a.setdefault("shift_dates", [])
        articles.append(a)
    articles.sort(key=lambda a: (a.get("published_at") or ""), reverse=True)
    return articles


def article_path(cfg, a):
    return f'{cfg["base_path"]}/articles/{a["id"]}/'


def article_url(cfg, a):
    return f'{cfg["base_url"]}/articles/{a["id"]}/'


def fmt_date(iso):
    """2026-08-20 → 8/20(水)"""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return iso
    return f"{d.month}/{d.day}({WEEKDAY_JP[d.weekday()]})"


# ============================================================
# codoc パーツ
# ============================================================
_warned = set()


def codoc_script_tag(cfg):
    c = cfg.get("codoc") or {}
    usercode = (c.get("usercode") or "").strip()
    if not usercode:
        if "usercode" not in _warned:
            _warned.add("usercode")
            log.warning("⚠️ codoc.usercode が未設定です。ペイウォールは表示されません "
                        "(site_config.json か CODOC_USERCODE で設定してください)")
        return "<!-- codoc usercode 未設定 -->"
    return render(c.get("script_tag", ""), usercode=usercode)


def codoc_entry_tag(cfg, a):
    c = cfg.get("codoc") or {}
    code = (a.get("codoc_entry_code") or "").strip()
    if not code:
        return ('<p class="paywall-missing">この記事はただいま販売準備中です。'
                '公開まで少々お待ちください。</p>')
    return render(c.get("entry_tag", ""), entry_code=code)


# ============================================================
# 共通レイアウト
# ============================================================
def nav_html(cfg):
    return render("""
<header class="site-header">
  <div class="wrap">
    <a class="brand" href="{{bp}}/">{{title}}</a>
    <nav>
      <a href="{{bp}}/">記事をさがす</a>
      <a href="{{bp}}/tokushoho.html">特商法表記</a>
      <a href="{{bp}}/privacy.html">プライバシー</a>
    </nav>
  </div>
</header>""", bp=cfg["base_path"], title=esc(cfg["site_title"]))


def footer_html(cfg):
    return render("""
<footer class="site-footer">
  <div class="wrap">
    <p class="links">
      <a href="{{bp}}/">記事をさがす</a>
      <a href="{{bp}}/tokushoho.html">特定商取引法に基づく表記</a>
      <a href="{{bp}}/privacy.html">プライバシーポリシー</a>
    </p>
    <p class="note">決済は codoc（株式会社codoc）を通じて行われます。</p>
    <p class="copy">&copy; {{year}} {{title}}</p>
  </div>
</footer>""", bp=cfg["base_path"], year=datetime.now(JST).year,
        title=esc(cfg["site_title"]))


def adult_gate_html(cfg):
    if not cfg.get("adult_gate"):
        return ""
    return render("""
<div id="age-gate" hidden>
  <div class="age-box">
    <h2>年齢確認</h2>
    <p>{{text}}</p>
    <div class="age-btns">
      <button type="button" id="age-yes">18歳以上です</button>
      <a class="age-no" href="https://www.google.com/">18歳未満です</a>
    </div>
  </div>
</div>
<script>
(function () {
  var KEY = 'wk_age_ok';
  var gate = document.getElementById('age-gate');
  if (!gate) return;
  try { if (localStorage.getItem(KEY) === '1') return; } catch (e) {}
  gate.hidden = false;
  document.documentElement.classList.add('gated');
  document.getElementById('age-yes').addEventListener('click', function () {
    try { localStorage.setItem(KEY, '1'); } catch (e) {}
    gate.hidden = true;
    document.documentElement.classList.remove('gated');
  });
})();
</script>""", text=esc(cfg.get("adult_gate_text", "")))


PAGE_TPL = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{page_title}}</title>
<meta name="description" content="{{description}}">
<link rel="canonical" href="{{canonical}}">
<meta name="robots" content="{{robots}}">
<meta property="og:type" content="{{og_type}}">
<meta property="og:title" content="{{og_title}}">
<meta property="og:description" content="{{description}}">
<meta property="og:url" content="{{canonical}}">
<meta property="og:site_name" content="{{site_title}}">
<meta property="og:locale" content="{{locale}}">
{{og_image}}
<meta name="twitter:card" content="summary_large_image">
<link rel="stylesheet" href="{{base_path}}/assets/style.css">
{{codoc_script}}
{{jsonld}}
</head>
<body>
{{nav}}
<main class="wrap">
{{content}}
</main>
{{footer}}
{{age_gate}}
</body>
</html>
"""


def page(cfg, *, content, page_title, description, canonical,
         og_type="website", og_title=None, image_url=None, jsonld=None,
         robots="index,follow"):
    og_img = ""
    if image_url:
        og_img = f'<meta property="og:image" content="{esc(image_url)}">'
    jsonld_tag = ""
    if jsonld:
        blocks = jsonld if isinstance(jsonld, list) else [jsonld]
        jsonld_tag = "".join(
            '<script type="application/ld+json">'
            + json.dumps(b, ensure_ascii=False) + "</script>" for b in blocks)
    return render(
        PAGE_TPL,
        page_title=esc(page_title),
        description=esc(description),
        canonical=esc(canonical),
        robots=robots,
        og_type=og_type,
        og_title=esc(og_title or page_title),
        og_image=og_img,
        site_title=esc(cfg["site_title"]),
        locale=cfg.get("locale", "ja_JP"),
        base_path=cfg["base_path"],
        codoc_script=codoc_script_tag(cfg),
        jsonld=jsonld_tag,
        nav=nav_html(cfg),
        content=content,
        footer=footer_html(cfg),
        age_gate=adult_gate_html(cfg),
    )


# ============================================================
# 記事カード（絞り込み用の data-* を持たせる）
# ============================================================
def card_html(cfg, a):
    tags = a.get("tags") or []
    dates = a.get("shift_dates") or []
    # 検索用の文字列（タイトル＋タグ＋エリア）を小文字で持たせる
    haystack = " ".join([a.get("title", ""), a.get("area", "")] + tags).lower()
    date_chips = "".join(
        f'<span class="d">{esc(fmt_date(d))}</span>' for d in dates[:3])
    return render("""
<li class="card" data-area="{{area}}" data-tags="{{tags}}" data-dates="{{dates}}"
    data-price="{{price_raw}}" data-sales="{{sales}}" data-pub="{{pub}}"
    data-next="{{next_date}}" data-s="{{hay}}">
  <a href="{{href}}">
    {{thumb}}
    <div class="card-body">
      <h3>{{title}}</h3>
      <p class="card-dates">{{date_chips}}</p>
      <p class="card-meta"><span class="cat">{{area}}</span><span class="price">{{price}}</span></p>
      {{tag_list}}
    </div>
  </a>
</li>""",
        area=esc(a.get("area") or "その他"),
        tags=esc(",".join(tags)),
        dates=esc(",".join(dates)),
        price_raw=int(a.get("price") or 0),
        sales=int(a.get("sales_count") or 0),
        pub=esc((a.get("published_at") or "")[:10]),
        next_date=esc(dates[0] if dates else "9999-12-31"),
        hay=esc(haystack),
        href=article_path(cfg, a),
        thumb=(render('<img class="thumb" src="{{s}}" alt="" loading="lazy">',
                      s=esc(a["image_url"])) if a.get("image_url")
               else '<span class="thumb thumb-none"></span>'),
        title=esc(a["title"]),
        date_chips=date_chips or '<span class="d d-none">出勤日未定</span>',
        price=(esc(f'{int(a["price"]):,}{cfg.get("currency_suffix", "円")}')
               if a.get("price") else ""),
        tag_list=('<p class="card-tags">'
                  + "".join(f"<span>{esc(t)}</span>" for t in tags[:5])
                  + "</p>") if tags else "",
    )


# ============================================================
# 絞り込みUI
# ============================================================
FINDER_JS = """
(function () {
  var root = document.getElementById('finder');
  if (!root) return;
  var cards = Array.prototype.slice.call(
    document.querySelectorAll('#results > .card'));
  var results = document.getElementById('results');
  var countEl = document.getElementById('result-count');
  var moreBtn = document.getElementById('more-btn');
  var emptyEl = document.getElementById('no-result');
  var qInput = document.getElementById('q');
  var sortSel = document.getElementById('sort');
  var PAGE = 30;
  var shown = PAGE;

  var state = { q: '', area: '', tags: [], day: '', sort: 'new' };

  function todayISO(offset) {
    var d = new Date(Date.now() + (offset || 0) * 86400000);
    // JSTのカレンダー日付に合わせる
    var j = new Date(d.getTime() + (d.getTimezoneOffset() + 540) * 60000);
    return j.getFullYear() + '-' + String(j.getMonth() + 1).padStart(2, '0')
      + '-' + String(j.getDate()).padStart(2, '0');
  }

  function dayMatches(card) {
    if (!state.day) return true;
    var dates = (card.dataset.dates || '').split(',').filter(Boolean);
    if (!dates.length) return false;
    if (state.day === 'today') return dates.indexOf(todayISO(0)) >= 0;
    if (state.day === 'tomorrow') return dates.indexOf(todayISO(1)) >= 0;
    if (state.day === 'week') {
      for (var i = 0; i < 7; i++) {
        if (dates.indexOf(todayISO(i)) >= 0) return true;
      }
      return false;
    }
    return dates.indexOf(state.day) >= 0;
  }

  function matches(card) {
    if (state.area && card.dataset.area !== state.area) return false;
    if (state.tags.length) {
      var ct = (card.dataset.tags || '').split(',');
      for (var i = 0; i < state.tags.length; i++) {
        if (ct.indexOf(state.tags[i]) < 0) return false;   // タグはAND条件
      }
    }
    if (!dayMatches(card)) return false;
    if (state.q) {
      var hay = card.dataset.s || '';
      var words = state.q.toLowerCase().split(/[\\s\\u3000]+/).filter(Boolean);
      for (var j = 0; j < words.length; j++) {
        if (hay.indexOf(words[j]) < 0) return false;
      }
    }
    return true;
  }

  var SORTS = {
    new:   function (a, b) { return (b.dataset.pub || '').localeCompare(a.dataset.pub || ''); },
    hot:   function (a, b) { return (+b.dataset.sales) - (+a.dataset.sales); },
    cheap: function (a, b) { return (+a.dataset.price) - (+b.dataset.price); },
    soon:  function (a, b) { return (a.dataset.next || '').localeCompare(b.dataset.next || ''); }
  };

  function apply(resetPage) {
    if (resetPage !== false) shown = PAGE;
    var hits = cards.filter(matches);
    hits.sort(SORTS[state.sort] || SORTS.new);
    cards.forEach(function (c) { c.hidden = true; });
    hits.slice(0, shown).forEach(function (c) {
      c.hidden = false;
      results.appendChild(c);
    });
    countEl.textContent = hits.length + '件';
    emptyEl.hidden = hits.length > 0;
    moreBtn.hidden = hits.length <= shown;
    syncUrl();
  }

  function syncUrl() {
    var p = new URLSearchParams();
    if (state.q) p.set('q', state.q);
    if (state.area) p.set('area', state.area);
    if (state.tags.length) p.set('tags', state.tags.join(','));
    if (state.day) p.set('day', state.day);
    if (state.sort !== 'new') p.set('sort', state.sort);
    var qs = p.toString();
    history.replaceState(null, '', qs ? '?' + qs : location.pathname);
  }

  function readUrl() {
    var p = new URLSearchParams(location.search);
    state.q = p.get('q') || '';
    state.area = p.get('area') || '';
    state.tags = (p.get('tags') || '').split(',').filter(Boolean);
    state.day = p.get('day') || '';
    state.sort = p.get('sort') || 'new';
    qInput.value = state.q;
    sortSel.value = state.sort;
    paintChips();
  }

  function paintChips() {
    root.querySelectorAll('.chip[data-area]').forEach(function (b) {
      b.classList.toggle('is-on', b.dataset.area === state.area);
    });
    root.querySelectorAll('.chip[data-day]').forEach(function (b) {
      b.classList.toggle('is-on', b.dataset.day === state.day);
    });
    root.querySelectorAll('.chip[data-tag]').forEach(function (b) {
      b.classList.toggle('is-on', state.tags.indexOf(b.dataset.tag) >= 0);
    });
    var daySel = document.getElementById('day-select');
    if (daySel) daySel.value = /^\\d{4}-/.test(state.day) ? state.day : '';
    root.classList.toggle('has-filter',
      !!(state.q || state.area || state.tags.length || state.day));
  }

  root.addEventListener('click', function (e) {
    var chip = e.target.closest('.chip');
    if (chip) {
      if ('area' in chip.dataset) {
        state.area = (state.area === chip.dataset.area) ? '' : chip.dataset.area;
      } else if ('day' in chip.dataset) {
        state.day = (state.day === chip.dataset.day) ? '' : chip.dataset.day;
      } else if ('tag' in chip.dataset) {
        var t = chip.dataset.tag, i = state.tags.indexOf(t);
        if (i >= 0) { state.tags.splice(i, 1); } else { state.tags.push(t); }
      }
      paintChips();
      apply();
      return;
    }
    if (e.target.id === 'clear-btn') {
      state.q = ''; state.area = ''; state.tags = []; state.day = '';
      qInput.value = '';
      paintChips();
      apply();
    }
    if (e.target.id === 'tag-more') {
      root.querySelector('.tag-wrap').classList.toggle('open');
      e.target.textContent =
        root.querySelector('.tag-wrap').classList.contains('open')
          ? 'タグを閉じる' : 'すべてのタグ';
    }
  });

  var timer;
  qInput.addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      state.q = qInput.value.trim();
      paintChips();
      apply();
    }, 150);
  });

  sortSel.addEventListener('change', function () {
    state.sort = sortSel.value;
    apply();
  });

  var daySel = document.getElementById('day-select');
  if (daySel) {
    daySel.addEventListener('change', function () {
      state.day = daySel.value;
      paintChips();
      apply();
    });
  }

  moreBtn.addEventListener('click', function () {
    shown += PAGE;
    apply(false);
  });

  readUrl();
  apply();
})();
"""


def finder_html(cfg, articles):
    """検索ボックス＋ファセットUI"""
    areas = Counter(a.get("area") or "その他" for a in articles)
    tags = Counter(t for a in articles for t in (a.get("tags") or []))
    today = datetime.now(JST).date()
    upcoming = Counter(d for a in articles for d in (a.get("shift_dates") or [])
                       if d >= today.isoformat())

    area_chips = "".join(
        render('<button type="button" class="chip" data-area="{{a}}">{{a}}'
               '<em>{{n}}</em></button>', a=esc(k), n=v)
        for k, v in areas.most_common())

    tag_items = tags.most_common()
    tag_chips = "".join(
        render('<button type="button" class="chip{{extra}}" data-tag="{{t}}">'
               '{{t}}<em>{{n}}</em></button>',
               t=esc(k), n=v, extra="" if i < TAG_CHIP_LIMIT else " chip-extra")
        for i, (k, v) in enumerate(tag_items))
    tag_more = ('<button type="button" class="link-btn" id="tag-more">すべてのタグ</button>'
                if len(tag_items) > TAG_CHIP_LIMIT else "")

    day_options = "".join(
        render('<option value="{{d}}">{{label}}（{{n}}件）</option>',
               d=esc(d), label=esc(fmt_date(d)), n=n)
        for d, n in sorted(upcoming.items())[:30])

    return render("""
<section id="finder" class="finder">
  <div class="search-row">
    <input type="search" id="q" placeholder="名前・タグ・エリアで検索"
           autocomplete="off" aria-label="キーワード検索">
    <button type="button" class="link-btn" id="clear-btn">条件をクリア</button>
  </div>

  <div class="facet">
    <span class="facet-label">出勤日</span>
    <div class="chips">
      <button type="button" class="chip" data-day="today">今日</button>
      <button type="button" class="chip" data-day="tomorrow">明日</button>
      <button type="button" class="chip" data-day="week">1週間以内</button>
      <select id="day-select" aria-label="日付で絞り込む">
        <option value="">日付を選ぶ</option>
        {{day_options}}
      </select>
    </div>
  </div>

  <div class="facet">
    <span class="facet-label">エリア</span>
    <div class="chips">{{area_chips}}</div>
  </div>

  <div class="facet">
    <span class="facet-label">タグ</span>
    <div class="chips tag-wrap">{{tag_chips}}</div>
    {{tag_more}}
  </div>

  <div class="result-row">
    <span id="result-count">{{count}}件</span>
    <label class="sort-label">並び替え
      <select id="sort">
        <option value="new">新着順</option>
        <option value="hot">人気順（販売数）</option>
        <option value="soon">出勤日が近い順</option>
        <option value="cheap">価格が安い順</option>
      </select>
    </label>
  </div>
</section>
""", area_chips=area_chips, tag_chips=tag_chips, tag_more=tag_more,
        day_options=day_options, count=len(articles))


def listing_html(cfg, articles, heading, lead=""):
    """ファセットUI + カード一覧（共通）"""
    cards = "".join(card_html(cfg, a) for a in articles)
    return render("""
<section class="hero-copy">
  <h1>{{heading}}</h1>
  {{lead}}
</section>
{{finder}}
<ul id="results" class="cards">{{cards}}</ul>
<p id="no-result" class="empty" hidden>条件に合う記事がありません。条件をゆるめてお試しください。</p>
{{empty}}
<div class="more-row"><button type="button" id="more-btn" class="more-btn" hidden>
  もっと見る</button></div>
<script>{{js}}</script>
""", heading=esc(heading),
        lead=f'<p>{esc(lead)}</p>' if lead else "",
        finder=finder_html(cfg, articles),
        cards=cards,
        empty=("" if articles else
               '<p class="empty">公開中の記事はまだありません。</p>'),
        js=FINDER_JS)


# ============================================================
# 各ページ
# ============================================================
def render_index(cfg, articles):
    content = listing_html(cfg, articles, cfg["site_title"],
                           cfg.get("site_tagline", ""))
    return page(cfg, content=content,
                page_title=f'{cfg["site_title"]} | {cfg.get("site_tagline", "")}',
                description=cfg["site_description"],
                canonical=cfg["base_url"] + "/")


def render_area(cfg, area, articles):
    heading = f"{area}の記事一覧"
    desc = (f"{area}エリアの出勤情報・体験レポート{len(articles)}件。"
            f"出勤日・タグで絞り込めます。")
    content = listing_html(cfg, articles, heading, desc)
    return page(cfg, content=content,
                page_title=f'{heading} | {cfg["site_title"]}',
                description=desc,
                canonical=f'{cfg["base_url"]}/area/{url_slug(area, AREA_SLUGS)}/')


def render_tag(cfg, tag, articles):
    heading = f"「{tag}」の記事一覧"
    desc = (f"{tag}の出勤情報・体験レポート{len(articles)}件。"
            f"出勤日・エリアで絞り込めます。")
    content = listing_html(cfg, articles, heading, desc)
    return page(cfg, content=content,
                page_title=f'{heading} | {cfg["site_title"]}',
                description=desc,
                canonical=f'{cfg["base_url"]}/tag/{url_slug(tag)}/')


def render_article(cfg, a, related):
    free = clean_free_html(a.get("free_html"))
    desc = plain_text(free) or cfg["site_description"]
    url = article_url(cfg, a)
    price = a.get("price")
    tags = a.get("tags") or []
    dates = a.get("shift_dates") or []

    date_html = ""
    if dates:
        chips = "".join(f'<span class="d">{esc(fmt_date(d))}</span>' for d in dates)
        date_html = f'<div class="shift-dates"><span class="lbl">出勤予定</span>{chips}</div>'

    meta_bits = []
    if a.get("area"):
        meta_bits.append(
            render('<a class="cat" href="{{bp}}/area/{{slug}}/">{{a}}</a>',
                   bp=cfg["base_path"], slug=url_slug(a["area"], AREA_SLUGS),
                   a=esc(a["area"])))
    if a.get("content_updated_at"):
        meta_bits.append(f'<time>{esc(a["content_updated_at"][:10])} 更新</time>')
    if price:
        meta_bits.append('<span class="price">'
                         + esc(f"{int(price):,}{cfg.get('currency_suffix', '円')}")
                         + "</span>")

    tags_html = ""
    if tags:
        tags_html = ('<ul class="tags">' + "".join(
            render('<li><a href="{{bp}}/tag/{{slug}}/">{{t}}</a></li>',
                   bp=cfg["base_path"], slug=url_slug(t), t=esc(t))
            for t in tags) + "</ul>")

    related_html = ""
    if related:
        items = "".join(
            render('<li><a href="{{href}}">{{t}}</a></li>',
                   href=article_path(cfg, r), t=esc(r["title"]))
            for r in related)
        related_html = ('<section class="related"><h2>同じエリアの記事</h2>'
                        f'<ul>{items}</ul></section>')

    hero = ""
    if a.get("image_url"):
        hero = render('<figure class="hero"><img src="{{src}}" alt="{{alt}}" '
                      'loading="lazy"></figure>',
                      src=esc(a["image_url"]), alt=esc(a["title"]))

    content = render("""
<nav class="crumbs"><a href="{{bp}}/">記事をさがす</a> ›
  <a href="{{bp}}/area/{{aslug}}/">{{area}}</a></nav>
<article class="post">
  <h1>{{title}}</h1>
  <div class="post-meta">{{meta}}</div>
  {{dates}}
  {{tags}}
  {{hero}}
  <div class="post-body">
{{free}}
  </div>
  <div class="paywall">
    <p class="paywall-lead">続きは有料パートです。ご購入いただくと、この下に本文がすべて表示されます。</p>
    {{codoc}}
  </div>
</article>
{{related}}
""", bp=cfg["base_path"], aslug=url_slug(a.get("area") or "その他", AREA_SLUGS),
        area=esc(a.get("area") or "その他"),
        title=esc(a["title"]), meta="".join(meta_bits), dates=date_html,
        tags=tags_html, hero=hero, free=free, codoc=codoc_entry_tag(cfg, a),
        related=related_html)

    jsonld = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": a["title"],
        "description": desc,
        "url": url,
        "isAccessibleForFree": False,
        "hasPart": {
            "@type": "WebPageElement",
            "isAccessibleForFree": False,
            "cssSelector": ".paywall",
        },
    }
    if a.get("image_url"):
        jsonld["image"] = a["image_url"]
    if a.get("published_at"):
        jsonld["datePublished"] = a["published_at"].replace(" ", "T") + "+09:00"
    if a.get("content_updated_at"):
        jsonld["dateModified"] = a["content_updated_at"].replace(" ", "T") + "+09:00"
    if tags:
        jsonld["keywords"] = ", ".join(tags)
    if price:
        jsonld["offers"] = {
            "@type": "Offer", "price": int(price), "priceCurrency": "JPY",
            "url": url, "availability": "https://schema.org/InStock",
        }

    # パンくずも構造化データにしておくと検索結果に階層が出る
    area = a.get("area") or "その他"
    breadcrumb = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": cfg["site_title"],
             "item": cfg["base_url"] + "/"},
            {"@type": "ListItem", "position": 2, "name": area,
             "item": f'{cfg["base_url"]}/area/{url_slug(area, AREA_SLUGS)}/'},
            {"@type": "ListItem", "position": 3, "name": a["title"], "item": url},
        ],
    }

    return page(cfg, content=content,
                page_title=f'{a["title"]} | {cfg["site_title"]}',
                description=desc, canonical=url, og_type="article",
                og_title=a["title"], image_url=a.get("image_url"),
                jsonld=[jsonld, breadcrumb])


def render_tokushoho(cfg):
    t = cfg.get("tokushoho") or {}
    rows = [
        ("販売事業者", t.get("seller")),
        ("運営責任者", t.get("manager")),
        ("所在地", t.get("address")),
        ("お問い合わせ", t.get("contact")),
        ("電話番号", t.get("phone")),
        ("販売価格", "各記事ページに表示された金額（税込）"),
        ("商品代金以外の必要料金", t.get("extra_fee")),
        ("お支払い方法", t.get("payment_method")),
        ("お支払い時期", t.get("payment_timing")),
        ("商品の引渡し時期", t.get("delivery_timing")),
        ("返品・キャンセル", t.get("refund_policy")),
        ("備考", t.get("note")),
    ]
    body = "".join(
        render("<tr><th>{{k}}</th><td>{{v}}</td></tr>", k=esc(k), v=esc(v or "―"))
        for k, v in rows)
    content = render("""
<article class="page">
  <h1>特定商取引法に基づく表記</h1>
  <table class="legal">{{rows}}</table>
</article>""", rows=body)
    return page(cfg, content=content,
                page_title=f'特定商取引法に基づく表記 | {cfg["site_title"]}',
                description="特定商取引法に基づく表記",
                canonical=cfg["base_url"] + "/tokushoho.html",
                robots="noindex,follow")


def render_privacy(cfg):
    t = cfg.get("tokushoho") or {}
    content = render("""
<article class="page">
  <h1>プライバシーポリシー</h1>
  <h2>取得する情報</h2>
  <p>当サイトは、記事の閲覧のみを行う場合に個人情報を取得することはありません。
  有料記事のご購入時は、決済事業者である codoc（株式会社codoc）および
  その決済代行事業者がメールアドレス・決済情報を取得します。
  当サイト運営者がクレジットカード番号を保持することはありません。</p>
  <h2>Cookie・ローカルストレージ</h2>
  <p>年齢確認の表示状態を記録するためにブラウザのローカルストレージを使用します。
  また、購入状態の判定のために codoc が Cookie を使用します。</p>
  <h2>第三者提供</h2>
  <p>法令に基づく場合を除き、取得した情報を第三者に提供することはありません。</p>
  <h2>お問い合わせ</h2>
  <p>{{contact}}</p>
</article>""", contact=esc(t.get("contact") or "―"))
    return page(cfg, content=content,
                page_title=f'プライバシーポリシー | {cfg["site_title"]}',
                description="プライバシーポリシー",
                canonical=cfg["base_url"] + "/privacy.html",
                robots="noindex,follow")


# ============================================================
# CSS
# ============================================================
STYLE_CSS = """
:root {
  --bg: #ffffff; --fg: #1a1a1c; --muted: #6b6b73; --line: #e6e6ea;
  --card: #ffffff; --chip: #f4f4f7; --accent: #c2185b; --accent-fg: #ffffff;
  --shadow: 0 1px 3px rgba(0,0,0,.08);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #131316; --fg: #ececf1; --muted: #a0a0ab; --line: #2c2c33;
    --card: #1c1c21; --chip: #22222a; --accent: #ff6f9c; --accent-fg: #1a1a1c;
    --shadow: none;
  }
}
* { box-sizing: border-box; }
html.gated { overflow: hidden; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP",
    "Yu Gothic", Meiryo, sans-serif;
  line-height: 1.8; font-size: 16px;
}
img { max-width: 100%; height: auto; }
a { color: inherit; }
.wrap { width: min(1000px, 92vw); margin: 0 auto; }
.site-header { border-bottom: 1px solid var(--line); position: sticky; top: 0;
  background: var(--bg); z-index: 10; }
.site-header .wrap { display: flex; flex-wrap: wrap; gap: .5rem 1.2rem;
  align-items: center; justify-content: space-between; padding: .9rem 0; }
.brand { font-weight: 700; text-decoration: none; font-size: 1.05rem; }
.site-header nav { display: flex; gap: 1rem; font-size: .85rem; }
.site-header nav a { color: var(--muted); text-decoration: none; }
.site-header nav a:hover { color: var(--accent); }
main.wrap { padding: 1.6rem 0 3rem; }
.hero-copy h1 { font-size: 1.45rem; margin: 0 0 .3rem; }
.hero-copy p { color: var(--muted); margin: 0 0 1.2rem; font-size: .9rem; }

/* 絞り込み */
.finder { background: var(--card); border: 1px solid var(--line);
  border-radius: 14px; padding: 1rem 1.1rem .9rem; margin-bottom: 1.4rem; }
.search-row { display: flex; gap: .6rem; align-items: center; margin-bottom: .9rem; }
#q { flex: 1; border: 1px solid var(--line); background: var(--bg); color: var(--fg);
  border-radius: 10px; padding: .65rem .9rem; font-size: 1rem; min-width: 0; }
#q:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.link-btn { background: none; border: 0; color: var(--muted); cursor: pointer;
  font-size: .82rem; text-decoration: underline; padding: .2rem; white-space: nowrap; }
.finder.has-filter .link-btn#clear-btn { color: var(--accent); font-weight: 600; }
.facet { display: flex; gap: .7rem; align-items: flex-start;
  padding: .45rem 0; border-top: 1px dashed var(--line); }
.facet-label { flex: 0 0 3.8rem; font-size: .78rem; color: var(--muted);
  padding-top: .35rem; }
.chips { display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; }
.chip { border: 1px solid var(--line); background: var(--chip); color: var(--fg);
  border-radius: 999px; padding: .28rem .75rem; font-size: .82rem; cursor: pointer;
  display: inline-flex; gap: .3rem; align-items: baseline; }
.chip em { font-style: normal; font-size: .68rem; color: var(--muted); }
.chip.is-on { background: var(--accent); color: var(--accent-fg);
  border-color: var(--accent); }
.chip.is-on em { color: var(--accent-fg); opacity: .8; }
.tag-wrap .chip-extra { display: none; }
.tag-wrap.open .chip-extra { display: inline-flex; }
#day-select, #sort { border: 1px solid var(--line); background: var(--bg);
  color: var(--fg); border-radius: 999px; padding: .28rem .6rem; font-size: .82rem; }
.result-row { display: flex; justify-content: space-between; align-items: center;
  border-top: 1px dashed var(--line); margin-top: .5rem; padding-top: .6rem;
  font-size: .85rem; }
#result-count { font-weight: 700; }
.sort-label { color: var(--muted); font-size: .8rem; display: flex; gap: .4rem;
  align-items: center; }

/* カード */
.cards { list-style: none; padding: 0; margin: 0; display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 1rem; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  overflow: hidden; box-shadow: var(--shadow); }
.card[hidden] { display: none; }
.card > a { text-decoration: none; display: block; height: 100%; }
.card .thumb { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }
.thumb-none { background: var(--chip); }
.card-body { padding: .8rem .9rem 1rem; }
.card-body h3 { font-size: .95rem; margin: 0 0 .45rem; line-height: 1.55;
  font-weight: 600; }
.card-dates { margin: 0 0 .4rem; display: flex; flex-wrap: wrap; gap: .25rem; }
.card-dates .d { font-size: .74rem; background: var(--chip); border-radius: 4px;
  padding: .05rem .4rem; color: var(--fg); }
.card-dates .d-none { color: var(--muted); }
.card-meta { margin: 0 0 .35rem; font-size: .78rem; display: flex; gap: .6rem;
  justify-content: space-between; }
.card-tags { margin: 0; display: flex; flex-wrap: wrap; gap: .25rem; }
.card-tags span { font-size: .7rem; color: var(--muted);
  border: 1px solid var(--line); border-radius: 4px; padding: 0 .35rem; }
.cat { color: var(--muted); text-decoration: none; }
.price { color: var(--accent); font-weight: 700; }
.empty { color: var(--muted); padding: 2rem 0; text-align: center; }
.more-row { text-align: center; margin-top: 1.6rem; }
.more-btn { border: 1px solid var(--line); background: var(--card); color: var(--fg);
  border-radius: 999px; padding: .6rem 2rem; font-size: .9rem; cursor: pointer; }

/* 記事 */
.crumbs { font-size: .78rem; color: var(--muted); margin-bottom: .7rem; }
.crumbs a { color: var(--muted); text-decoration: none; }
.post { max-width: 720px; }
.post h1 { font-size: 1.45rem; line-height: 1.55; margin: 0 0 .6rem; }
.post-meta { display: flex; flex-wrap: wrap; gap: .8rem; font-size: .82rem;
  color: var(--muted); margin-bottom: .8rem; }
.shift-dates { display: flex; flex-wrap: wrap; gap: .35rem; align-items: center;
  background: var(--chip); border-radius: 10px; padding: .5rem .7rem;
  margin-bottom: 1rem; }
.shift-dates .lbl { font-size: .75rem; color: var(--muted); margin-right: .2rem; }
.shift-dates .d { font-size: .85rem; font-weight: 600; background: var(--bg);
  border-radius: 6px; padding: .1rem .5rem; }
.tags { list-style: none; display: flex; flex-wrap: wrap; gap: .35rem;
  padding: 0; margin: 0 0 1.2rem; }
.tags a { font-size: .74rem; border: 1px solid var(--line); border-radius: 4px;
  padding: .1rem .45rem; color: var(--muted); text-decoration: none;
  display: inline-block; }
.tags a:hover { color: var(--accent); border-color: var(--accent); }
.hero { margin: 0 0 1.4rem; }
.hero img { border-radius: 12px; width: 100%; }
.post-body { overflow-wrap: anywhere; }
.post-body img { border-radius: 8px; }
.post-body table { display: block; overflow-x: auto; border-collapse: collapse; }
.paywall { margin-top: 2rem; padding-top: 1.4rem; border-top: 2px dashed var(--line); }
.paywall-lead { font-size: .88rem; color: var(--muted); margin: 0 0 1rem; }
.paywall-missing { color: var(--muted); font-size: .9rem; }
.related { margin-top: 3rem; border-top: 1px solid var(--line); padding-top: 1.4rem;
  max-width: 720px; }
.related h2 { font-size: 1rem; margin: 0 0 .6rem; }
.related ul { padding-left: 1.1rem; margin: 0; }
.related li { margin-bottom: .35rem; font-size: .9rem; }
.page h1 { font-size: 1.35rem; }
.legal { width: 100%; border-collapse: collapse; font-size: .9rem; }
.legal th, .legal td { border: 1px solid var(--line); padding: .6rem .8rem;
  text-align: left; vertical-align: top; }
.legal th { width: 34%; background: var(--card); font-weight: 600; }
.site-footer { border-top: 1px solid var(--line); padding: 1.6rem 0 2.4rem;
  font-size: .8rem; color: var(--muted); }
.site-footer .links { display: flex; flex-wrap: wrap; gap: 1rem; margin: 0 0 .6rem; }
.site-footer a { color: var(--muted); }
.site-footer .note, .site-footer .copy { margin: .2rem 0; }
#age-gate { position: fixed; inset: 0; background: rgba(0,0,0,.86); z-index: 100;
  display: flex; align-items: center; justify-content: center; padding: 1.2rem; }
#age-gate[hidden] { display: none; }
.age-box { background: var(--card); color: var(--fg); border-radius: 14px;
  padding: 1.8rem 1.6rem; max-width: 420px; text-align: center; }
.age-box h2 { margin: 0 0 .6rem; font-size: 1.15rem; }
.age-box p { font-size: .9rem; color: var(--muted); }
.age-btns { display: flex; flex-direction: column; gap: .6rem; margin-top: 1.2rem; }
#age-yes { background: var(--accent); color: var(--accent-fg); border: 0;
  border-radius: 8px; padding: .7rem 1rem; font-size: .95rem; cursor: pointer; }
.age-no { color: var(--muted); font-size: .85rem; }
@media (max-width: 560px) {
  body { font-size: 15px; }
  .cards { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: .7rem; }
  .card-body { padding: .6rem .65rem .75rem; }
  .card-body h3 { font-size: .85rem; }
  .facet { flex-direction: column; gap: .35rem; }
  .facet-label { flex: none; padding-top: 0; }
}
"""


# ============================================================
# ビルド
# ============================================================
def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def build(cfg=None):
    cfg = cfg or load_config()
    out = cfg["output_dir"]
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out, exist_ok=True)

    articles = load_articles(cfg)
    log.info(f"📄 記事 {len(articles)}件を書き出します → {out}/")

    _write(os.path.join(out, "assets", "style.css"), STYLE_CSS.strip() + "\n")
    _write(os.path.join(out, "index.html"), render_index(cfg, articles))
    _write(os.path.join(out, "tokushoho.html"), render_tokushoho(cfg))
    _write(os.path.join(out, "privacy.html"), render_privacy(cfg))

    by_area = {}
    by_tag = {}
    for a in articles:
        by_area.setdefault(a.get("area") or "その他", []).append(a)
        for t in (a.get("tags") or []):
            by_tag.setdefault(t, []).append(a)

    urls = [cfg["base_url"] + "/"]

    for a in articles:
        related = [r for r in by_area.get(a.get("area") or "その他", [])
                   if r["id"] != a["id"]][:6]
        _write(os.path.join(out, "articles", a["id"], "index.html"),
               render_article(cfg, a, related))
        urls.append(article_url(cfg, a))
        sold = "販売中" if a.get("codoc_entry_code") else "⚠️ codoc未紐付け"
        log.info(f"  ✅ articles/{a['id']}/  {a['title'][:34]}  [{sold}]")

    for area, items in sorted(by_area.items()):
        slug = slugify(area, AREA_SLUGS)
        _write(os.path.join(out, "area", slug, "index.html"),
               render_area(cfg, area, items))
        urls.append(f'{cfg["base_url"]}/area/{url_slug(area, AREA_SLUGS)}/')
    log.info(f"  📁 エリア別ページ {len(by_area)}件")

    for tag, items in sorted(by_tag.items()):
        slug = slugify(tag)
        _write(os.path.join(out, "tag", slug, "index.html"),
               render_tag(cfg, tag, items))
        urls.append(f'{cfg["base_url"]}/tag/{url_slug(tag)}/')
    log.info(f"  🏷️  タグ別ページ {len(by_tag)}件")

    today = datetime.now(JST).strftime("%Y-%m-%d")
    entries = "".join(
        f"<url><loc>{esc(u)}</loc><lastmod>{today}</lastmod></url>" for u in urls)
    _write(os.path.join(out, "sitemap.xml"),
           '<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + entries + "</urlset>")
    _write(os.path.join(out, "robots.txt"),
           f"User-agent: *\nAllow: /\nSitemap: {cfg['base_url']}/sitemap.xml\n")
    _write(os.path.join(out, ".nojekyll"), "")

    missing = [a["id"] for a in articles if not a.get("codoc_entry_code")]
    if missing:
        log.warning(f"⚠️ codocエントリーコード未設定の記事: {missing}")
    log.info(f"✅ サイト生成完了: {out}/ "
             f"（記事{len(articles)} / URL{len(urls)}）")
    return len(articles)


if __name__ == "__main__":
    build()
