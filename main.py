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

CITY = os.getenv("DTEK_CITY", "м. Київ")
STREET_HINT = os.getenv("DTEK_STREET_HINT", "Драгоманова")
CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@Poznyaky_Svitlo24")

BASE_URL = "https://www.dtek-kem.com.ua"
SCHEDULE_URL = f"{BASE_URL}/ua/shutdowns"
AJAX_URL = f"{BASE_URL}/ua/ajax"

STATE_FILE = Path("state.json")
KYIV_TZ = ZoneInfo("Europe/Kyiv")

HOUSES = {
    "42": "вул. Драгоманова, 42",
    "42А": "вул. Драгоманова, 42А",
}


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
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
        timeout=30.0,
        headers=headers,
        cookies=cookies,
        impersonate="chrome120",
    )

    async with DtekClient(
        "kem",
        ajax_url=AJAX_URL,
        session=session,
    ) as client:
        streets = await client.get_streets(CITY)

        matches = [
            s.name for s in streets
            if STREET_HINT.casefold() in s.name.casefold()
        ]

        if not matches:
            raise RuntimeError("Не найдена улица Драгоманова в ДТЕК")

        street_name = max(matches, key=len)
        response = await client.get_home_num(CITY, street_name)

        available = {
            str(k).strip().upper(): v
            for k, v in response.houses.items()
        }

        result = {}

        for house_no in HOUSES:
            entry = available.get(house_no.upper())

            if entry is None:
                raise RuntimeError(f"ДТЕК не вернул дом {house_no}")

            result[house_no] = bool(entry.has_current_outage)

    await session.close()
    return result, street_name


def make_message(house_no, outage, street_name, initial=False):
    address = f"Київ, {street_name}, {house_no}"
    now = datetime.now(KYIV_TZ).strftime("%d.%m.%Y о %H:%M")

    if outage:
        title = "🔴 ДТЕК ПОВІДОМЛЯЄ ПРО ВІДКЛЮЧЕННЯ"
        detail = "За даними ДТЕК, за адресою є повідомлення про відключення."
    else:
        title = "🟢 ДТЕК НЕ ПОКАЗУЄ ВІДКЛЮЧЕННЯ"
        detail = "За даними ДТЕК, активне відключення за адресою не відображається."

    prefix = "Поточний статус" if initial else "Зміна статусу"

    return (
        f"⚡ <b>СВІТЛО | ПОЗНЯКИ ²⁴/⁷</b>\n\n"
        f"<b>{title}</b>\n"
        f"📍 {address}\n\n"
        f"{detail}\n"
        f"🕒 {prefix}: {now}\n\n"
        f"<i>Джерело: ДТЕК Київські електромережі. "
        f"Дані можуть оновлюватися із затримкою та не є вимірюванням напруги в будинку.</i>"
    )


async def main():
    token = os.environ["BOT_TOKEN"]
    bot = Bot(token=token)

    old_state = load_state()
    current, street_name = await get_dtek_status()
    new_state = {k: bool(v) for k, v in current.items()}

    first_run = not old_state

    for house_no, outage in new_state.items():
        changed = old_state.get(house_no) != outage

        if first_run or changed:
            await bot.send_message(
                chat_id=CHANNEL,
                text=make_message(
                    house_no, outage, street_name, initial=first_run
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

    save_state(new_state)
    print(f"OK: {new_state}; street={street_name}")


if __name__ == "__main__":
    asyncio.run(main())
