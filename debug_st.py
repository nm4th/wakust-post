import requests, re
from bs4 import BeautifulSoup

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})
session.post(
    "https://wakust.com/wp-content/themes/wakust/user_edit/login_mypage.php",
    data={"login_email": "jesuisallerajapon@yahoo.co.jp", "login_password": "motosue4"}
)

res  = session.get("https://wakust.com/mypage/?post_edit=1598368")

# post_stに関するHTML部分をそのまま表示
m = re.search(r'.{0,200}post_st.{0,500}', res.text, re.DOTALL)
if m:
    print("=== post_st周辺のHTML ===")
    print(m.group()[:1000])

# edit_b_st_2周辺も確認
m2 = re.search(r'.{0,200}edit_b_st.{0,500}', res.text, re.DOTALL)
if m2:
    print("\n=== edit_b_st周辺のHTML ===")
    print(m2.group()[:1000])
