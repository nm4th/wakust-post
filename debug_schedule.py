"""
omiya-mens-este.net 出勤日取得デバッグスクリプト
Playwrightでページを取得し、スケジュール関連のHTML構造を詳細にダンプする

使い方:
  python debug_schedule.py [URL]
  デフォルト: https://omiya-mens-este.net/profile.html?sid=285
"""
import sys
import re

def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "https://omiya-mens-este.net/profile.html?sid=285"
    print(f"=== デバッグ対象URL: {url} ===\n")

    # --- Playwright取得 ---
    from playwright.sync_api import sync_playwright
    from bs4 import BeautifulSoup
    import time

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        page = context.new_page()
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        print(f"HTTP Status: {response.status}")
        if response.status == 403:
            print("403 → Cloudflareチャレンジ待機中...")
            time.sleep(5)
            page.wait_for_load_state("networkidle", timeout=15000)
        else:
            page.wait_for_load_state("networkidle", timeout=15000)
        try:
            page.wait_for_selector(".sch-date, .sch-work, .sch-tbl, .weekSchedule, table", timeout=5000)
            print("スケジュール要素検出: OK")
        except Exception:
            print("スケジュール要素検出: タイムアウト（要素なし or ロード遅延）")
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    title = soup.find("title")
    print(f"ページタイトル: {title.get_text() if title else '(なし)'}")
    print(f"HTML全体の長さ: {len(html)} bytes\n")

    # --- Cloudflare/403チェック ---
    if soup.find(id="challenge-running") or soup.find(id="cf-challenge-running"):
        print("❌ Cloudflareチャレンジページが検出されました")
        return
    if title and "403" in title.get_text():
        print("❌ 403エラーページです")
        return

    # --- スケジュール関連クラスの探索 ---
    print("=" * 60)
    print("■ スケジュール関連クラスを持つ要素:")
    print("=" * 60)
    sch_pattern = re.compile(r"sch|schedule|shift|syukkin|week", re.I)
    found_elements = []
    for tag in soup.find_all(class_=sch_pattern):
        classes = " ".join(tag.get("class", []))
        found_elements.append((tag.name, classes))
        print(f"  <{tag.name} class=\"{classes}\">")
        # 子要素の構造を表示
        children = [c for c in tag.children if hasattr(c, 'name') and c.name]
        if children:
            child_summary = ", ".join(f"<{c.name} class=\"{' '.join(c.get('class', []))}\">({c.get_text(strip=True)[:30]})" for c in children[:10])
            print(f"    子要素: {child_summary}")
            if len(children) > 10:
                print(f"    ... 他 {len(children) - 10} 要素")
        else:
            text = tag.get_text(strip=True)[:80]
            print(f"    テキスト: {text}")
    if not found_elements:
        print("  (なし)")

    # --- sch-date/sch-work 詳細 ---
    print("\n" + "=" * 60)
    print("■ div.sch-date の詳細:")
    print("=" * 60)
    all_sch_dates = soup.find_all("div", class_=re.compile(r"sch-date"))
    print(f"  個数: {len(all_sch_dates)}")
    for i, sd in enumerate(all_sch_dates):
        print(f"\n  --- sch-date #{i} ---")
        print(f"  HTML (先頭500文字):\n{sd.prettify()[:500]}")
        dts = sd.find_all("dt")
        print(f"  <dt> 個数: {len(dts)}")
        for j, dt in enumerate(dts[:7]):
            print(f"    dt[{j}]: text={dt.get_text(strip=True)!r}  html={str(dt)[:100]}")
        # dt以外の子要素もチェック
        non_dt = [c for c in sd.children if hasattr(c, 'name') and c.name and c.name != 'dt']
        if non_dt:
            print(f"  dt以外の子要素: {', '.join(c.name for c in non_dt[:10])}")

    print("\n" + "=" * 60)
    print("■ div.sch-work の詳細:")
    print("=" * 60)
    all_sch_works = soup.find_all("div", class_=re.compile(r"sch-work"))
    print(f"  個数: {len(all_sch_works)}")
    for i, sw in enumerate(all_sch_works):
        print(f"\n  --- sch-work #{i} ---")
        print(f"  HTML (先頭500文字):\n{sw.prettify()[:500]}")
        dds = sw.find_all("dd")
        print(f"  <dd> 個数: {len(dds)}")
        for j, dd in enumerate(dds[:7]):
            print(f"    dd[{j}]: text={dd.get_text(strip=True)!r}  html={str(dd)[:100]}")
        non_dd = [c for c in sw.children if hasattr(c, 'name') and c.name and c.name != 'dd']
        if non_dd:
            print(f"  dd以外の子要素: {', '.join(c.name for c in non_dd[:10])}")

    # --- sch-tbl 詳細 ---
    print("\n" + "=" * 60)
    print("■ .sch-tbl の詳細:")
    print("=" * 60)
    sch_tbls = soup.find_all(class_=re.compile(r"sch-tbl"))
    print(f"  個数: {len(sch_tbls)}")
    for i, st in enumerate(sch_tbls):
        print(f"\n  --- sch-tbl #{i} ---")
        print(f"  HTML (先頭800文字):\n{st.prettify()[:800]}")

    # --- dl/dt/dd 構造全体 ---
    print("\n" + "=" * 60)
    print("■ ページ全体の <dl> 構造:")
    print("=" * 60)
    dls = soup.find_all("dl")
    print(f"  <dl> 個数: {len(dls)}")
    for i, dl in enumerate(dls):
        parent_class = " ".join(dl.parent.get("class", [])) if dl.parent else ""
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        print(f"  dl[{i}]: parent_class={parent_class!r}  dt数={len(dts)}  dd数={len(dds)}")
        for j, (dt, dd) in enumerate(zip(dts[:5], dds[:5])):
            print(f"    [{j}] dt={dt.get_text(strip=True)[:30]!r}  dd={dd.get_text(strip=True)[:30]!r}")

    # --- テーブル構造 ---
    print("\n" + "=" * 60)
    print("■ <table> 構造:")
    print("=" * 60)
    tables = soup.find_all("table")
    print(f"  <table> 個数: {len(tables)}")
    for i, tbl in enumerate(tables):
        tbl_class = " ".join(tbl.get("class", []))
        rows = tbl.find_all("tr")
        print(f"  table[{i}]: class={tbl_class!r}  行数={len(rows)}")
        for j, row in enumerate(rows[:3]):
            ths = [th.get_text(strip=True)[:20] for th in row.find_all("th")]
            tds = [td.get_text(strip=True)[:20] for td in row.find_all("td")]
            print(f"    row[{j}]: ths={ths}  tds={tds}")

    # --- 日付パターンのテキスト検出 ---
    print("\n" + "=" * 60)
    print("■ テキスト内の日付パターン検出:")
    print("=" * 60)
    text = soup.get_text()

    # M/D(曜) + 時刻
    pattern1 = list(re.finditer(r"(\d{1,2})/(\d{1,2})\s*\([月火水木金土日]\)[^\n]{0,30}(\d{2}:\d{2})", text))
    print(f"  M/D(曜)...HH:MM 形式: {len(pattern1)}件")
    for m in pattern1[:5]:
        print(f"    {m.group()[:60]}")

    # M月D日 + 時刻
    pattern2 = list(re.finditer(r"(\d{1,2})月(\d{1,2})日[^\n]*?(\d{2}:\d{2})", text))
    print(f"  M月D日...HH:MM 形式: {len(pattern2)}件")
    for m in pattern2[:5]:
        print(f"    {m.group()[:60]}")

    # M/D形式（時刻なし）
    pattern3 = list(re.finditer(r"(\d{1,2})/(\d{1,2})\s*\([月火水木金土日]\)", text))
    print(f"  M/D(曜) 形式（時刻なし含む）: {len(pattern3)}件")
    for m in pattern3[:10]:
        # 前後のコンテキスト
        start = max(0, m.start() - 10)
        end = min(len(text), m.end() + 40)
        context = text[start:end].replace("\n", "\\n")
        print(f"    ...{context}...")

    # HH:MM パターン（全体）
    times = re.findall(r"\d{2}:\d{2}", text)
    print(f"  HH:MM 時刻パターン全体: {len(times)}件")
    if times:
        print(f"    先頭10件: {times[:10]}")

    # 「お休み」「未定」
    yasumi = len(re.findall(r"お?休み", text))
    mitei = len(re.findall(r"未定", text))
    manwaku = len(re.findall(r"満枠", text))
    print(f"  「休み」: {yasumi}件  「未定」: {mitei}件  「満枠」: {manwaku}件")

    # --- HTML全体をファイルに保存 ---
    out_file = "/tmp/debug_schedule_output.html"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✅ 取得したHTML全体を {out_file} に保存しました")


if __name__ == "__main__":
    main()
