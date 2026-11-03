
import asyncio
import json
import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot
from dtek_client import DtekClient
from dtek_client.browser_auth import get_cleared_cookies
from curl_cffi.requests import AsyncSession

BASE_URL = "https://www.dtek-kem.com.ua"
SCHEDULE_URL = f"{BASE_URL}/ua/shutdowns"
AJAX_URL = f"{BASE_URL}/ua/ajax"

CITY = os.getenv("DTEK_CITY", "м. Київ")
STREET_HINT = os.getenv("DTEK_STREET_HINT", "Драгоманова")
CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@Poznyaky_Svitlo24")

STATE_FILE = Path("state.json")
KYIV_TZ = ZoneInfo("Europe/Kyiv")

HOUSES = ["42", "42А"]


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def get_dtek_status():
    cookies, csrf_token = await get_cleared_cookies(SCHEDULE_URL)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "uk,en;q=0.9",
        "Origin": BASE_URL,
        "Referer": SCHEDULE_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    if csrf_token:
        headers["X-CSRF-Token"] = csrf_token

    session = AsyncSession(
        timeout=30,
        headers=headers,
        cookies=cookies,
        impersonate="chrome120",
    )

    try:
        async with DtekClient(
            "kem",
            ajax_url=AJAX_URL,
            session=session,
        ) as client:
            streets = await client.get_streets(CITY)

            matches = [
                street.name
                for street in streets
                if STREET_HINT.casefold() in street.name.casefold()
            ]

            if not matches:
                raise RuntimeError("Не найдена улица Драгоманова")

            street_name = max(matches, key=len)
            response = await client.get_home_num(CITY, street_name)

            available = {
                str(number).strip().upper(): entry
                for number, entry in response.houses.items()
            }

            result = {}

            for number in HOUSES:
                entry = available.get(number.upper())

                if entry is None:
                    raise RuntimeError(
                        f"ДТЕК не вернул данные для дома {number}"
                    )

                result[number] = bool(entry.has_current_outage)

            return result, street_name
    finally:
        await session.close()


def make_message(number, outage, street_name, initial=False):
    now = datetime.now(KYIV_TZ).strftime("%d.%m.%Y о %H:%M")
    address = f"Київ, {street_name}, {number}"

    if outage:
        title = "🔴 ДТЕК ПОВІДОМЛЯЄ ПРО ВІДКЛЮЧЕННЯ"
        detail = "За даними ДТЕК, за адресою є повідомлення про відключення."
    else:
        title = "🟢 ДТЕК НЕ ПОКАЗУЄ ВІДКЛЮЧЕННЯ"
        detail = "За даними ДТЕК, активне відключення не відображається."

    prefix = "Поточний статус" if initial else "Зміна статусу"

    return (
        f"⚡ <b>СВІТЛО | ПОЗНЯКИ ²⁴/⁷</b>\n\n"
        f"<b>{title}</b>\n"
        f"📍 {address}\n\n"
        f"{detail}\n"
        f"🕒 {prefix}: {now}\n\n"
        f"<i>Джерело: ДТЕК Київські електромережі. "
        f"Дані можуть оновлюватися із затримкою та не є вимірюванням напруги.</i>"
    )


async def main():
    token = os.environ["BOT_TOKEN"]
    bot = Bot(token=token)

    old_state = load_state()
    current, street_name = await get_dtek_status()

    first_run = not old_state

    for number, outage in current.items():
        if first_run or old_state.get(number) != outage:
            await bot.send_message(
                chat_id=CHANNEL,
                text=make_message(
                    number, outage, street_name, first_run
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

    save_state(current)
    print(f"OK: {current}; street={street_name}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(f"ОШИБКА МОНИТОРИНГА: {type(error).__name__}: {error}")
        raise
