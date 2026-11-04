
import asyncio
import json
import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape

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
KYIV = ZoneInfo("Europe/Kyiv")

ADDRESSES = {
    "Драгоманова, 42": ("Драгоманова", "42"),
    "Драгоманова, 42А": ("Драгоманова", "42/А"),
    "Урлівська, 36": ("Урлівська", "36"),
    "Петра Григоренка, 21": ("Петра Григоренка", "21"),
}


def now_time():
    return datetime.now(KYIV).strftime("%H:%M")


def normalize_house(number):
    return str(number).strip().upper().replace(" ", "")


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
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

                if "григоренка" in lowered:
                    found_streets["Петра Григоренка"] = name

            results = {}

            for address, (street_key, house_number) in ADDRESSES.items():
                street_name = found_streets.get(street_key)

                if not street_name:
                    raise RuntimeError(
                        f"ДТЕК не знайшов вулицю: {street_key}"
                    )

                response = await client.get_home_num(CITY, street_name)

                houses = {
                    normalize_house(number): entry
                    for number, entry in response.houses.items()
                }

                target = normalize_house(house_number)
                entry = houses.get(target)

                if entry is None:
                    raise RuntimeError(
                        f"ДТЕК не повернув будинок {address}. "
                        f"Шукали номер: {target}"
                    )

                results[address] = bool(entry.has_current_outage)

            return results

    finally:
        await session.close()


def prepare_state(current, old_state):
    # Підтримуємо і старий, і новий формат state.json
    previous = old_state.get("addresses", old_state)

    new_addresses = {}
    changed_addresses = set()
    current_time = now_time()

    for address, outage in current.items():
        old = previous.get(address)

        if isinstance(old, bool):
            old_outage = old
            old_event_time = None
        elif isinstance(old, dict):
            old_outage = old.get("outage")
            old_event_time = old.get("event_time")
        else:
            old_outage = None
            old_event_time = None

        changed = (
            old_outage is not None
            and old_outage != outage
        )

        if changed:
            changed_addresses.add(address)
            event_time = current_time
        else:
            event_time = old_event_time

        new_addresses[address] = {
            "outage": outage,
            "event_time": event_time,
        }

    return new_addresses, changed_addresses


def make_summary(addresses, changed_addresses):
    parts = []

    for address, info in addresses.items():
        outage = info["outage"]
        event_time = info["event_time"]

        if outage:
            title = "🚨 <b>ВІДКЛЮЧЕННЯ СВІТЛА</b>"
        elif address in changed_addresses:
            title = "🟢 <b>СВІТЛО ПОВЕРТАЄТЬСЯ</b>"
        else:
            title = "🟢 <b>СВІТЛО Є</b>"

        parts.append(title)

        if address in changed_addresses and event_time:
            parts.append(f"О {event_time}")

        parts.append(f"📍 Київ, вул. {escape(address)}")

        if event_time and not address in changed_addresses:
            parts.append(f"🕒 Остання зміна: {event_time}")

        parts.append("")
        parts.append("━━━━━━━━━━━━━━")
        parts.append("")

    if parts:
        parts.pop()
        parts.pop()

    parts.append("")
    parts.append("ℹ️ Очікуємо оновлення інформації від ДТЕК.")

    return "\n".join(parts)


async def main():
    token = os.environ["BOT_TOKEN"]
    bot = Bot(token=token)

    old_state = load_state()
    current = await get_statuses()

    addresses, changed_addresses = prepare_state(
        current,
        old_state,
    )

    first_run = not old_state

    if first_run or changed_addresses:
        message_text = make_summary(
            addresses,
            changed_addresses,
        )

        message = await bot.send_message(
            chat_id=CHANNEL,
            text=message_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        print(
            f"Нова загальна публікація створена. "
            f"ID: {message.message_id}"
        )

        print("Змінені адреси:", changed_addresses)

    else:
        print("Змін немає. Повідомлення не надсилаємо.")

    save_state({
        "addresses": addresses,
    })

    print("Перевірені адреси:", current)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(
            f"ПОМИЛКА МОНІТОРИНГУ: "
            f"{type(error).__name__}: {error}"
        )
        raise
