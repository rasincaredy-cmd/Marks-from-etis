"""
ETIS parser — авторизация, оценки, расписание.
ETIS использует Windows-1251 и форму с POST.

Ключевые особенности:
- После POST /stu.login сервер возвращает <meta http-equiv="refresh" content="0;URL=stu.signs">
  (HTML-редирект, не HTTP 302) — aiohttp его не следует, поэтому после POST
  вручную делаем GET на целевой URL.
- Сессия (куки) должна жить на уровне экземпляра ETISParser и переиспользоваться
  для всех последующих запросов.
"""

import logging
import re
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL    = "https://student.psu.ru/pls/stu_cus_et"
LOGIN_URL   = f"{BASE_URL}/stu.login"
GRADES_URL  = f"{BASE_URL}/stu.signs"
TIMETABLE_URL = f"{BASE_URL}/stu.timetable"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
}


class ETISParser:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    # ── Session management ────────────────────────────────────────────────────

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(
                headers=HEADERS,
                connector=connector,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ── Login ─────────────────────────────────────────────────────────────────

    async def login(self, username: str, password: str) -> bool:
        """
        POST логин-форму. ЕТИС отвечает meta-refresh на stu.signs —
        вручную делаем GET на него, чтобы убедиться что сессия активна.
        """
        session = await self._ensure_session()

        # Кодируем вручную в cp1251, как это делает браузер
        payload = (
            f"p_redirect=stu.signs"
            f"&p_username={_url_encode_cp1251(username)}"
            f"&p_password={_url_encode_cp1251(password)}"
        )

        try:
            # POST — не следуем редиректам сами
            async with session.post(
                LOGIN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.read()
                text = body.decode("cp1251", errors="replace")

            # Проверяем: session_id кука должна появиться
            cookies = {c.key: c.value for c in session.cookie_jar}
            if "session_id" not in cookies:
                logger.warning("ETIS: no session_id cookie after POST — login failed")
                return False

            # Следуем мета-редиректу вручную (GET stu.signs)
            async with session.get(
                GRADES_URL,
                params={"p_mode": "current"},
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp2:
                body2 = await resp2.read()
                text2 = body2.decode("cp1251", errors="replace")

            # Если снова видим форму логина — что-то пошло не так
            if 'name="p_username"' in text2:
                logger.warning("ETIS: redirected back to login after GET stu.signs")
                return False

            logger.info("ETIS login OK for %s", username)
            return True

        except Exception as e:
            logger.error("Login exception: %s", e)
            return False

    # ── Fetch helper ──────────────────────────────────────────────────────────

    async def _fetch(self, url: str, params: dict | None = None) -> str | None:
        session = await self._ensure_session()
        try:
            async with session.get(
                url,
                params=params,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.read()
                return body.decode("cp1251", errors="replace")
        except Exception as e:
            logger.error("Fetch %s error: %s", url, e)
            return None

    # ── Grades ────────────────────────────────────────────────────────────────

    async def get_grades(self, term: int | None = None) -> dict[str, list[dict]]:
        params: dict = {"p_mode": "current"}
        if term is not None:
            params["p_term"] = str(term)
        html = await self._fetch(GRADES_URL, params=params)
        if html is None:
            return {}
        return self._parse_grades(html)

    def _parse_grades(self, html: str) -> dict[str, list[dict]]:
        soup = BeautifulSoup(html, "html.parser")
        result: dict[str, list[dict]] = {}

        content = soup.find("div", class_="content") or soup

        for h3 in content.find_all("h3"):
            subject_raw = h3.get_text(strip=True)
            subject_name = re.sub(r"\s*\[.*?\]\s*$", "", subject_raw).strip()
            if not subject_name:
                continue

            table = h3.find_next_sibling("table")
            if not table:
                continue

            rows_data: list[dict] = []
            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) < 7:
                    continue
                if cells[0].get("align") == "right":   # строка "Итого"
                    continue

                score_span = cells[3].find("span")
                score_text = (score_span.get_text(strip=True) if score_span
                              else cells[3].get_text(strip=True))

                # Тема КТ — текст ссылки в cells[0] (убираем лишние пробелы/переносы)
                theme_tag = cells[0].find("a")
                theme = theme_tag.get_text(strip=True) if theme_tag else cells[0].get_text(strip=True)

                rows_data.append({
                    # cells[3] = Оценка (набранный балл за работу)
                    # cells[4] = Проходной балл
                    # cells[5] = Балл в рейтинг текущий (фактически зачтено)
                    # cells[6] = Балл в рейтинг максимальный (сколько можно за КТ)
                    "theme":          theme,
                    "current_score":  _to_int(score_text),
                    "passing_score":  _to_int(cells[4].get_text(strip=True)),
                    "rating_score":   _to_int(cells[3].get_text(strip=True)),
                    "max_score":      _to_int(cells[6].get_text(strip=True)),
                    "work_type":      cells[1].get_text(strip=True) if len(cells) > 1 else "",
                    "control_type":   cells[2].get_text(strip=True) if len(cells) > 2 else "",
                    "date":           cells[7].get_text(strip=True) if len(cells) > 7 else "",
                    "teacher":        cells[8].get_text(strip=True) if len(cells) > 8 else "",
                    "is_red":         "color:red" in (tr.get("style") or ""),
                })

            if rows_data:
                result[subject_name] = rows_data

        return result

    # ── Timetable ─────────────────────────────────────────────────────────────

    async def get_timetable(self, week: int | None = None) -> dict:
        params: dict = {"p_cons": "y"}
        if week is not None:
            params["p_week"] = str(week)
        html = await self._fetch(TIMETABLE_URL, params=params)
        if html is None:
            return {"week": week or 0, "week_label": "", "days": []}
        return self._parse_timetable(html)

    def _parse_timetable(self, html: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")

        # ── Определяем отображаемую неделю ───────────────────────────────────
        # Среди всех <li class="week ..."> та, у которой НЕТ тега <a> внутри —
        # это и есть отображаемая/текущая неделя (браузер тоже её не делает ссылкой).
        displayed_week = 0
        current_week = 0

        for li in soup.find_all("li", class_=lambda c: c and "week" in c.split()):
            if li.find("a") is None:
                txt = li.get_text(strip=True)
                if txt.isdigit():
                    displayed_week = int(txt)
                    # Если у li есть класс "current" — это реальная текущая неделя
                    if "current" in li.get("class", []):
                        current_week = displayed_week
                    break

        if displayed_week == 0:
            # Фолбэк: берём из класса current
            current_li = soup.find("li", class_=lambda c: c and "current" in c.split())
            if current_li:
                txt = current_li.get_text(strip=True)
                if txt.isdigit():
                    displayed_week = current_week = int(txt)

        # ── Метка недели ──────────────────────────────────────────────────────
        week_label = f"Неделя {displayed_week}"
        for div in soup.find_all("div"):
            style = div.get("style", "")
            if "text-align:center" in style or "text-align: center" in style:
                t = div.get_text(strip=True).split("<!--")[0].strip()
                if t and re.search(r"\d{2}\.\d{2}\.\d{4}", t):
                    week_label = t
                    break

        # ── Парсим дни ────────────────────────────────────────────────────────
        timetable_div = soup.find("div", class_="timetable")
        days = []
        if timetable_div:
            for day_div in timetable_div.find_all("div", class_="day"):
                h3 = day_div.find("h3")
                day_name = h3.get_text(strip=True) if h3 else "День"

                pairs = []
                for tr in day_div.find_all("tr"):
                    pair_num_td = tr.find("td", class_="pair_num")
                    pair_info_td = tr.find("td", class_="pair_info")
                    if not pair_num_td or not pair_info_td:
                        continue
                    if pair_info_td.get_text(strip=True) in ("", "\xa0"):
                        continue

                    num_text = pair_num_td.get_text(separator=" ", strip=True)
                    m = re.match(r"(\d+)\s*пара\s*(.+)", num_text)
                    pair_num  = m.group(1) if m else num_text
                    pair_time = m.group(2).strip() if m else ""

                    inner_divs = pair_info_td.find_all("div", recursive=False)
                    sub_items = inner_divs if inner_divs else [pair_info_td]

                    for sub in sub_items:
                        subject_span = sub.find("span", class_="dis")
                        teacher_span = sub.find("span", class_="teacher")
                        room_span    = sub.find("span", class_="aud")

                        subject = subject_span.get_text(strip=True) if subject_span else ""
                        if not subject:
                            continue

                        teacher = ""
                        if teacher_span:
                            teacher_a = teacher_span.find("a")
                            if teacher_a:
                                teacher = teacher_a.get_text(strip=True)
                            else:
                                teacher = teacher_span.get_text("\n", strip=True).split("\n")[0]

                        room = room_span.get_text(strip=True) if room_span else ""

                        pairs.append({
                            "num": pair_num,
                            "time": pair_time,
                            "subject": subject,
                            "teacher": teacher,
                            "room": room,
                        })

                days.append({"name": day_name, "pairs": pairs})

        return {
            "week":       displayed_week,
            "week_label": week_label,
            "days":       days,
        }


# ── Утилиты ───────────────────────────────────────────────────────────────────

def _to_int(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _url_encode_cp1251(s: str) -> str:
    """Кодирует строку как cp1251 и percent-encode каждый байт."""
    from urllib.parse import quote
    return quote(s.encode("cp1251", errors="replace"))
