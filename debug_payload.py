"""
送信前のペイロードを確認するデバッグスクリプト
※ 実際には更新しない（POSTしない）
"""
import requests
from bs4 import BeautifulSoup
import re

BASE_URL       = "https://wakust.com"
LOGIN_AJAX_URL = "https://wakust.com/wp-content/themes/wakust/user_edit/login_mypage.php"

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})
session.post(LOGIN_AJAX_URL, data={
    "login_email":    "jesuisallerajapon@yahoo.co.jp",
    "login_password": "motosue4",
})

# 確認したい記事ID（直近で変わってしまったもの）
POST_ID = "1598368"

res  = session.get(f"{BASE_URL}/mypage/?post_edit={POST_ID}")
soup = BeautifulSoup(res.text, "html.parser")
form = soup.find("form", action=lambda a: a and "useredit" in a)

print("=== フォームから取得できる全フィールド ===")
for inp in form.find_all(["input", "textarea", "select"]):
    name = inp.get("name")
    if not name:
        continue
    if inp.name == "select":
        # 全optionとselected状態を表示
        opts = [(o.get("value"), o.get_text(strip=True)[:15], "★" if o.get("selected") else "") for o in inp.find_all("option")]
        selected_opt = next((o for o in inp.find_all("option") if o.get("selected")), None)
        print(f"\n[select] name={name!r}")
        print(f"  HTML上のselected: {selected_opt.get('value') if selected_opt else 'なし（JS依存）'}")
        for v, t, s in opts[:5]:
            print(f"  option value={v!r} text={t!r} {s}")
        if len(opts) > 5:
            print(f"  ... ({len(opts)}件)")
    elif inp.name == "textarea":
        t = inp.decode_contents()
        print(f"[textarea] name={name!r} 長さ={len(t)}")
    elif inp.get("type") == "hidden":
        print(f"[hidden] name={name!r} value={inp.get('value')!r}")
    elif inp.get("type") in ("checkbox", "radio"):
        print(f"[{inp.get('type')}] name={name!r} value={inp.get('value')!r} checked={inp.get('checked')}")
    else:
        v = inp.get("value", "")
        print(f"[{inp.get('type','input')}] name={name!r} value={str(v)[:50]!r}")

# 記事ページからカテゴリーURLを確認
print("\n=== 記事ページのカテゴリーリンク ===")
res2 = session.get(f"https://wakust.com/Risingnoboru/{POST_ID}/")
soup2 = BeautifulSoup(res2.text, "html.parser")
for a in soup2.find_all("a", href=True):
    if "post-category" in a["href"]:
        print(f"  {a['href']} → {a.get_text(strip=True)}")
