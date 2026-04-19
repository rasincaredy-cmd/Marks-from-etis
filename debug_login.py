"""
Отладочный скрипт — запусти его отдельно, чтобы понять что происходит при логине.
Покажет: куда редиректит, какой HTML возвращается, есть ли оценки/расписание.

Запуск:
    python debug_login.py <логин> <пароль>
"""

import asyncio
import sys
import aiohttp
from bs4 import BeautifulSoup

BASE_URL = "https://student.psu.ru/pls/stu_cus_et"
LOGIN_URL = f"{BASE_URL}/stu.login"
GRADES_URL = f"{BASE_URL}/stu.signs"
TIMETABLE_URL = f"{BASE_URL}/stu.timetable"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


async def debug(username: str, password: str):
    connector = aiohttp.TCPConnector(ssl=False)
    jar = aiohttp.CookieJar(unsafe=True)

    async with aiohttp.ClientSession(connector=connector, cookie_jar=jar) as session:

        # ── Step 1: GET login page (to get any initial cookies) ──────────────
        print("=" * 60)
        print("STEP 1: GET login page")
        async with session.get(LOGIN_URL, headers=HEADERS) as r:
            print(f"  Status: {r.status}")
            print(f"  URL: {r.url}")
            cookies_before = {c.key: c.value for c in jar}
            print(f"  Cookies after GET: {cookies_before}")

        # ── Step 2: POST login ────────────────────────────────────────────────
        print("\nSTEP 2: POST login")
        payload = {
            "p_redirect": "stu.signs",
            "p_username": username,
            "p_password": password,
        }
        # Try both: raw string and cp1251 encoded
        async with session.post(
            LOGIN_URL,
            data=payload,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=True,
        ) as r:
            print(f"  Final URL after redirects: {r.url}")
            print(f"  Status: {r.status}")
            body = await r.read()
            text = body.decode("cp1251", errors="replace")
            cookies_after = {c.key: c.value for c in jar}
            print(f"  Cookies after POST: {cookies_after}")

            soup = BeautifulSoup(text, "html.parser")
            title = soup.find("title")
            print(f"  Page title: {title.get_text() if title else 'N/A'}")

            # Check if we're still on login page
            has_login_form = bool(soup.find("input", {"name": "p_username"}))
            print(f"  Still on login page: {has_login_form}")

            # Check for error messages
            for tag in soup.find_all(["div", "p", "span"], class_=lambda c: c and "error" in c.lower() if c else False):
                print(f"  Error element: {tag.get_text(strip=True)[:100]}")

            print(f"  HTML preview (first 500 chars):")
            print("  " + text[:500].replace("\n", "\n  "))

        if has_login_form:
            print("\n❌ LOGIN FAILED — still on login page. Check credentials or encoding.")
            return

        # ── Step 3: GET grades page ───────────────────────────────────────────
        print("\nSTEP 3: GET grades page")
        async with session.get(
            GRADES_URL,
            params={"p_mode": "current"},
            headers=HEADERS,
            allow_redirects=True
        ) as r:
            print(f"  Final URL: {r.url}")
            print(f"  Status: {r.status}")
            body = await r.read()
            text = body.decode("cp1251", errors="replace")
            soup = BeautifulSoup(text, "html.parser")
            title = soup.find("title")
            print(f"  Title: {title.get_text() if title else 'N/A'}")

            has_login_form = bool(soup.find("input", {"name": "p_username"}))
            print(f"  Redirected to login: {has_login_form}")

            # Count subjects
            h3_tags = soup.find_all("h3")
            print(f"  <h3> tags found: {len(h3_tags)}")
            for h3 in h3_tags[:5]:
                print(f"    → {h3.get_text(strip=True)[:60]}")

            tables = soup.find_all("table", class_="common")
            print(f"  Tables with class='common': {len(tables)}")

            print(f"  HTML preview (first 800 chars):")
            print("  " + text[:800].replace("\n", "\n  "))

        # ── Step 4: GET timetable ─────────────────────────────────────────────
        print("\nSTEP 4: GET timetable")
        async with session.get(
            TIMETABLE_URL,
            params={"p_cons": "y"},
            headers=HEADERS,
            allow_redirects=True
        ) as r:
            print(f"  Final URL: {r.url}")
            print(f"  Status: {r.status}")
            body = await r.read()
            text = body.decode("cp1251", errors="replace")
            soup = BeautifulSoup(text, "html.parser")
            title = soup.find("title")
            print(f"  Title: {title.get_text() if title else 'N/A'}")

            has_login_form = bool(soup.find("input", {"name": "p_username"}))
            print(f"  Redirected to login: {has_login_form}")

            timetable_div = soup.find("div", class_="timetable")
            print(f"  Timetable div found: {timetable_div is not None}")

            week_items = soup.find_all("li", class_=lambda c: c and "week" in c)
            print(f"  Week items found: {len(week_items)}")

            current_week_li = soup.find("li", class_=lambda c: c and "current" in c)
            print(f"  Current week li: {current_week_li}")

            print(f"  HTML preview (first 800 chars):")
            print("  " + text[:800].replace("\n", "\n  "))

    print("\n✅ Debug complete")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python debug_login.py <login> <password>")
        sys.exit(1)
    asyncio.run(debug(sys.argv[1], sys.argv[2]))
