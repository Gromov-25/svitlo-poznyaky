
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

CITY = "м. Київ"
CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@Poznyaky_Svitlo24")
STATE_FILE = Path("state.json")
TZ = ZoneInfo("Europe/Kyiv")

ADDRESSES = {
    "Драгоманова, 42": ("Драгоманова", "42"),
    "Драгоманова, 42А": ("Драгоманова", "42А"),
    "Урлівська, 36": ("Урлівська", "36"),
}


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def get_statuses():
    cookies, csrf_token = await get_cleared_cookies(SCHEDULE_URL)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
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

            found_streets = {}

            for street in streets:
                name = street.name
                lowered = name.casefold()

                if "драгоманова" in lowered:
                    found_streets["Драгоманова"] = name

                if "урлівська" in lowered or "урловская" in lowered:
                    found_streets["Урлівська"] = name

            results = {}

            for address, (street_key, house_number) in ADDRESSES.items():
                street_name = found_streets.get(street_key)

                if not street_name:
                    raise RuntimeError(
                        f"ДТЕК не знайшов вулицю для адреси {address}"
                    )

                response = await client.get_home_num(CITY, street_name)

                houses = {
                    str(number).strip().upper(): entry
                    for number, entry in response.houses.items()
                }

                entry = houses.get(house_number.upper())

                if entry is None:
                    raise RuntimeError(
                        f"ДТЕК не повернув будинок {address}"
                    )

                results[address] = bool(entry.has_current_outage)

            return results

    finally:
        await session.close()


def make_message(address, outage, initial=False):
    now = datetime.now(TZ).strftime("%d.%m.%Y о %H:%M")

    if outage:
        status = "🔴 ДТЕК ПОКАЗУЄ ВІДКЛЮЧЕННЯ"
        detail = "За даними ДТЕК, для адреси відображається відключення."
    else:
        status = "🟢 ДТЕК НЕ ПОКАЗУЄ ВІДКЛЮЧЕННЯ"
        detail = "За даними ДТЕК, активне відключення не відображається."

    event = "Початковий статус" if initial else "Зміна статусу"

    return (
        "⚡ <b>СВІТЛО | ПОЗНЯКИ 24/7</b>\n\n"
        f"<b>{status}</b>\n"
        f"📍 Київ, вул. {address}\n\n"
        f"{detail}\n"
        f"🕒 {event}: {now}\n\n"
        "<i>Джерело: ДТЕК Київські електромережі. "
        "Це дані ДТЕК, а не вимірювання напруги у квартирі.</i>"
    )


async def main():
    token = os.environ["BOT_TOKEN"]
    bot = Bot(token=token)

    old_state = load_state()

    # Якщо ДТЕК не відповість, помилка зупинить запуск.
    # Старий стан не буде перезаписаний.
    current = await get_statuses()

    first_run = not old_state

    for address, outage in current.items():
        if first_run or old_state.get(address) != outage:
            await bot.send_message(
                chat_id=CHANNEL,
                text=make_message(address, outage, first_run),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

    save_state(current)
    print(f"Успішно перевірено адреси: {current}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(f"ПОМИЛКА МОНІТОРИНГУ: {type(error).__name__}: {error}")
        raise
