"""自社サイト（静的サイト）ジェネレーター

site_content/articles/*.json を読み込み、codoc の貼り付けタグを埋め込んだ
記事ページ一式を site/ に出力する。

  python wakust_site.py

出力物:
  site/index.html               記事一覧
  site/articles/{id}.html       記事ページ（無料部分 + codocペイウォール）
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
from datetime import datetime, timedelta, timezone

log = logging.getLogger("wakust.site")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

JST = timezone(timedelta(hours=9))
CONFIG_FILE = "site_config.json"

# ============================================================
# テンプレート補助（CSSの波括弧と衝突しないよう {{key}} 形式）
# ============================================================
def render(tpl, **kw):
    out = tpl
    for k, v in kw.items():
        out = out.replace("{{" + k + "}}", "" if v is None else str(v))
    return out


def esc(s):
    return html.escape(s or "", quote=True)


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
        articles.append(a)
    articles.sort(key=lambda a: (a.get("published_at") or ""), reverse=True)
    return articles


def article_url(cfg, a):
    return f"{cfg['base_url']}/articles/{a['id']}.html"


# ============================================================
# パーツ
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


def nav_html(cfg):
    bp = cfg["base_path"]
    return render("""
<header class="site-header">
  <div class="wrap">
    <a class="brand" href="{{bp}}/">{{title}}</a>
    <nav>
      <a href="{{bp}}/">記事一覧</a>
      <a href="{{bp}}/tokushoho.html">特商法表記</a>
      <a href="{{bp}}/privacy.html">プライバシー</a>
    </nav>
  </div>
</header>""", bp=bp, title=esc(cfg["site_title"]))


def footer_html(cfg):
    bp = cfg["base_path"]
    year = datetime.now(JST).year
    return render("""
<footer class="site-footer">
  <div class="wrap">
    <p class="links">
      <a href="{{bp}}/">記事一覧</a>
      <a href="{{bp}}/tokushoho.html">特定商取引法に基づく表記</a>
      <a href="{{bp}}/privacy.html">プライバシーポリシー</a>
    </p>
    <p class="note">決済は codoc（株式会社codoc）を通じて行われます。</p>
    <p class="copy">&copy; {{year}} {{title}}</p>
  </div>
</footer>""", bp=bp, year=year, title=esc(cfg["site_title"]))


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
        jsonld_tag = ('<script type="application/ld+json">'
                      + json.dumps(jsonld, ensure_ascii=False) + "</script>")
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
# 記事ページ
# ============================================================
def render_article(cfg, a, related):
    free = clean_free_html(a.get("free_html"))
    desc = plain_text(free) or cfg["site_description"]
    url = article_url(cfg, a)
    price = a.get("price")
    tags = a.get("tags") or []

    meta_bits = []
    if a.get("category"):
        meta_bits.append(f'<span class="cat">{esc(a["category"])}</span>')
    if a.get("published_at"):
        meta_bits.append(f'<time>{esc(a["published_at"][:10])} 公開</time>')
    if a.get("content_updated_at"):
        meta_bits.append(f'<time>{esc(a["content_updated_at"][:10])} 更新</time>')
    if price:
        meta_bits.append('<span class="price">'
                         + esc(f"{int(price):,}{cfg.get('currency_suffix', '円')}")
                         + "</span>")
    tags_html = ""
    if tags:
        tags_html = ('<ul class="tags">'
                     + "".join(f"<li>{esc(t)}</li>" for t in tags) + "</ul>")

    related_html = ""
    if related:
        items = "".join(
            render('<li><a href="{{href}}">{{t}}</a></li>',
                   href=f'{cfg["base_path"]}/articles/{r["id"]}.html',
                   t=esc(r["title"]))
            for r in related)
        related_html = f'<section class="related"><h2>関連記事</h2><ul>{items}</ul></section>'

    hero = ""
    if a.get("image_url"):
        hero = render('<figure class="hero"><img src="{{src}}" alt="{{alt}}" '
                      'loading="lazy"></figure>',
                      src=esc(a["image_url"]), alt=esc(a["title"]))

    content = render("""
<article class="post">
  <h1>{{title}}</h1>
  <div class="post-meta">{{meta}}</div>
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
""", title=esc(a["title"]), meta=" ".join(meta_bits), tags=tags_html,
        hero=hero, free=free, codoc=codoc_entry_tag(cfg, a),
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
    if price:
        jsonld["offers"] = {
            "@type": "Offer",
            "price": int(price),
            "priceCurrency": "JPY",
            "url": url,
            "availability": "https://schema.org/InStock",
        }

    return page(cfg, content=content,
                page_title=f'{a["title"]} | {cfg["site_title"]}',
                description=desc, canonical=url, og_type="article",
                og_title=a["title"], image_url=a.get("image_url"),
                jsonld=jsonld)


# ============================================================
# 一覧ページ
# ============================================================
def render_index(cfg, articles):
    cats = sorted({a.get("category") or "その他" for a in articles})
    filters = '<button type="button" class="f-btn is-on" data-cat="">すべて</button>'
    filters += "".join(
        render('<button type="button" class="f-btn" data-cat="{{c}}">{{c}}</button>',
               c=esc(c)) for c in cats)

    cards = []
    for a in articles:
        cards.append(render("""
<li class="card" data-cat="{{cat}}">
  <a href="{{href}}">
    {{thumb}}
    <div class="card-body">
      <h2>{{title}}</h2>
      <p class="card-meta"><span class="cat">{{cat}}</span> <span class="price">{{price}}</span></p>
      <p class="card-excerpt">{{excerpt}}</p>
    </div>
  </a>
</li>""",
            cat=esc(a.get("category") or "その他"),
            href=f'{cfg["base_path"]}/articles/{a["id"]}.html',
            thumb=(render('<img class="thumb" src="{{s}}" alt="" loading="lazy">',
                          s=esc(a["image_url"])) if a.get("image_url") else ""),
            title=esc(a["title"]),
            price=(esc(f'{int(a["price"]):,}{cfg.get("currency_suffix", "円")}')
                   if a.get("price") else ""),
            excerpt=esc(plain_text(clean_free_html(a.get("free_html")), 90)),
        ))

    empty = "" if cards else '<p class="empty">公開中の記事はまだありません。</p>'

    content = render("""
<section class="hero-copy">
  <h1>{{title}}</h1>
  <p>{{tagline}}</p>
</section>
<div class="filters">{{filters}}</div>
<ul class="cards">{{cards}}</ul>
{{empty}}
<script>
(function () {
  var btns = document.querySelectorAll('.f-btn');
  var cards = document.querySelectorAll('.cards .card');
  btns.forEach(function (b) {
    b.addEventListener('click', function () {
      var cat = b.dataset.cat;
      btns.forEach(function (x) { x.classList.toggle('is-on', x === b); });
      cards.forEach(function (c) {
        c.hidden = !!cat && c.dataset.cat !== cat;
      });
    });
  });
})();
</script>
""", title=esc(cfg["site_title"]), tagline=esc(cfg.get("site_tagline", "")),
        filters=filters, cards="".join(cards), empty=empty)

    return page(cfg, content=content,
                page_title=f'{cfg["site_title"]} | {cfg.get("site_tagline", "")}',
                description=cfg["site_description"],
                canonical=cfg["base_url"] + "/")


# ============================================================
# 固定ページ
# ============================================================
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
  --card: #ffffff; --accent: #c2185b; --accent-fg: #ffffff; --shadow: 0 1px 3px rgba(0,0,0,.08);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #131316; --fg: #ececf1; --muted: #a0a0ab; --line: #2c2c33;
    --card: #1c1c21; --accent: #ff6f9c; --accent-fg: #1a1a1c; --shadow: none;
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
.wrap { width: min(880px, 92vw); margin: 0 auto; }
.site-header { border-bottom: 1px solid var(--line); position: sticky; top: 0;
  background: var(--bg); z-index: 10; }
.site-header .wrap { display: flex; flex-wrap: wrap; gap: .5rem 1.2rem;
  align-items: center; justify-content: space-between; padding: .9rem 0; }
.brand { font-weight: 700; text-decoration: none; font-size: 1.05rem; }
.site-header nav { display: flex; gap: 1rem; font-size: .85rem; }
.site-header nav a { color: var(--muted); text-decoration: none; }
.site-header nav a:hover { color: var(--accent); }
main.wrap { padding: 2rem 0 3rem; }
.hero-copy h1 { font-size: 1.6rem; margin: 0 0 .4rem; }
.hero-copy p { color: var(--muted); margin: 0 0 1.5rem; }
.filters { display: flex; flex-wrap: wrap; gap: .4rem; margin-bottom: 1.4rem; }
.f-btn { border: 1px solid var(--line); background: transparent; color: var(--muted);
  border-radius: 999px; padding: .3rem .9rem; font-size: .82rem; cursor: pointer; }
.f-btn.is-on { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
.cards { list-style: none; padding: 0; margin: 0; display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 1rem; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  overflow: hidden; box-shadow: var(--shadow); }
.card > a { text-decoration: none; display: block; height: 100%; }
.card .thumb { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }
.card-body { padding: .85rem 1rem 1.1rem; }
.card-body h2 { font-size: .98rem; margin: 0 0 .4rem; line-height: 1.5; }
.card-meta { margin: 0 0 .4rem; font-size: .78rem; color: var(--muted);
  display: flex; gap: .6rem; }
.card-excerpt { margin: 0; font-size: .8rem; color: var(--muted); }
.cat { color: var(--muted); }
.price { color: var(--accent); font-weight: 700; }
.empty { color: var(--muted); }
.post h1 { font-size: 1.5rem; line-height: 1.5; margin: 0 0 .6rem; }
.post-meta { display: flex; flex-wrap: wrap; gap: .8rem; font-size: .82rem;
  color: var(--muted); margin-bottom: .8rem; }
.tags { list-style: none; display: flex; flex-wrap: wrap; gap: .35rem;
  padding: 0; margin: 0 0 1.2rem; }
.tags li { font-size: .72rem; border: 1px solid var(--line); border-radius: 4px;
  padding: .1rem .45rem; color: var(--muted); }
.hero { margin: 0 0 1.4rem; }
.hero img { border-radius: 12px; width: 100%; }
.post-body { overflow-wrap: anywhere; }
.post-body img { border-radius: 8px; }
.post-body table { display: block; overflow-x: auto; border-collapse: collapse; }
.paywall { margin-top: 2rem; padding-top: 1.4rem; border-top: 2px dashed var(--line); }
.paywall-lead { font-size: .88rem; color: var(--muted); margin: 0 0 1rem; }
.paywall-missing { color: var(--muted); font-size: .9rem; }
.related { margin-top: 3rem; border-top: 1px solid var(--line); padding-top: 1.4rem; }
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
@media (max-width: 480px) {
  body { font-size: 15px; }
  .cards { grid-template-columns: 1fr; }
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

    by_cat = {}
    for a in articles:
        by_cat.setdefault(a.get("category") or "その他", []).append(a)

    for a in articles:
        related = [r for r in by_cat.get(a.get("category") or "その他", [])
                   if r["id"] != a["id"]][:6]
        _write(os.path.join(out, "articles", f'{a["id"]}.html'),
               render_article(cfg, a, related))
        sold = "販売中" if a.get("codoc_entry_code") else "⚠️ codoc未紐付け"
        log.info(f"  ✅ articles/{a['id']}.html  {a['title'][:36]}  [{sold}]")

    # sitemap / robots
    urls = [cfg["base_url"] + "/"] + [article_url(cfg, a) for a in articles]
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
    log.info(f"✅ サイト生成完了: {out}/ ({len(articles)}記事)")
    return len(articles)


if __name__ == "__main__":
    build()
