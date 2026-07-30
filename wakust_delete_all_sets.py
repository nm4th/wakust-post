"""
ワクスト セット販売 一括削除スクリプト
====================================================================
現在登録されている全ての「セット販売」を削除する。

使い方:
  # ドライラン（削除せず一覧のみ表示）
  python wakust_delete_all_sets.py --dry-run

  # 実際に削除
  python wakust_delete_all_sets.py

環境変数:
  WAKUST_EMAIL, WAKUST_PASSWORD
"""

import argparse
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

from wakust_auto_update import (
    BASE_URL,
    login_wakust,
    log,
)

SETPRICE_LIST_URL = f"{BASE_URL}/mypage/?setprice"
DELETE_SET_URL    = f"{BASE_URL}/wp-content/themes/wakust/user_edit/edit_set.php"


def fetch_set_list(session):
    """セット販売一覧ページから (set_id, title, sales_line) のリストを全ページ取得"""
    sets = []
    seen_ids = set()
    page = 1
    while True:
        url = f"{SETPRICE_LIST_URL}&cp={page}" if page > 1 else SETPRICE_LIST_URL
        try:
            res = session.get(url, timeout=30)
        except requests.RequestException as e:
            log.error(f"❌ セット一覧取得エラー (page={page}): {e}")
            break
        if res.status_code != 200:
            log.error(f"❌ セット一覧取得失敗 (page={page}, HTTP {res.status_code})")
            break
        soup = BeautifulSoup(res.text, "html.parser")
        page_items = []
        for i_del in soup.find_all("i", class_=re.compile(r"delete_set")):
            set_id = i_del.get("data-id")
            if not set_id or set_id in seen_ids:
                continue
            seen_ids.add(set_id)
            tr = i_del.find_parent("tr")
            title = ""
            sales_line = ""
            if tr:
                tds = tr.find_all("td")
                if tds:
                    a = tds[0].find("a")
                    title = (a.get_text(strip=True) if a else tds[0].get_text(strip=True))
                if len(tds) >= 3:
                    sales_line = tds[2].get_text(" ", strip=True)
            page_items.append((set_id, title, sales_line))
        if not page_items:
            break
        sets.extend(page_items)
        next_page = page + 1
        next_link = soup.find("a", href=re.compile(rf"cp={next_page}\b"))
        if not next_link:
            for a in soup.find_all("a", href=re.compile(r"cp=(\d+)")):
                m_cp = re.search(r"cp=(\d+)", a["href"])
                if m_cp and int(m_cp.group(1)) > page:
                    next_link = a
                    break
        if not next_link:
            break
        page += 1
        time.sleep(0.5)
    log.info(f"    📋 セット取得数: {len(sets)}件（{page}ページ）")
    return sets


def delete_set(session, set_id):
    """指定IDのセットを削除する"""
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": SETPRICE_LIST_URL,
        "Origin": BASE_URL,
    }
    try:
        res = session.post(
            DELETE_SET_URL,
            data={"delete_set_id": str(set_id)},
            headers=headers,
            timeout=30,
        )
        return res.status_code == 200
    except requests.RequestException as e:
        log.error(f"    ❌ 削除リクエスト例外 [id={set_id}]: {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="削除せず一覧のみ表示")
    args = parser.parse_args()

    session = login_wakust()
    if not session:
        sys.exit(1)

    sets = fetch_set_list(session)
    log.info(f"\n📦 現在のセット販売: {len(sets)}件\n")
    for sid, title, sales in sets:
        log.info(f"  [{sid}] {title}  ({sales})")

    if not sets:
        log.info("削除対象なし。終了。")
        session.close()
        return

    if args.dry_run:
        log.info("\n🧪 ドライランのため削除しません。")
        session.close()
        return

    log.info(f"\n🗑️  削除開始 ({len(sets)}件)")
    ok, ng = 0, 0
    for sid, title, _ in sets:
        if delete_set(session, sid):
            log.info(f"  ✅ 削除 [{sid}] {title}")
            ok += 1
        else:
            log.error(f"  ❌ 削除失敗 [{sid}] {title}")
            ng += 1
        time.sleep(0.5)

    log.info(f"\n📊 完了: 成功={ok}, 失敗={ng}")
    session.close()


if __name__ == "__main__":
    main()
