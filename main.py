import asyncio
import json
import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot
from dtek_client import DtekClient

CITY = os.getenv("DTEK_CITY", "м. Київ")
STREET_HINT = os.getenv("DTEK_STREET_HINT", "Драгоманова")
CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@Poznyaky_Svitlo24")
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
    # KEM = ДТЕК Київські електромережі (саме місто Київ).
    async with DtekClient("kem") as client:
        streets = await client.get_streets(CITY)
        matches = [s.name for s in streets if STREET_HINT.casefold() in s.name.casefold()]
        if not matches:
            raise RuntimeError(
                f"Не знайшов вулицю за підказкою {STREET_HINT!r}. "
                f"Перевірте назву міста/вулиці в налаштуваннях."
            )

        # Prefer the most specific full street name returned by DTEK.
        street_name = max(matches, key=len)
        response = await client.get_home_num(CITY, street_name)
        available = {str(k).strip().upper(): v for k, v in response.houses.items()}

        result = {}
        for house_no in HOUSES:
            entry = available.get(house_no.upper())
            if entry is None:
                raise RuntimeError(
                    f"ДТЕК не повернув будинок {house_no} для {street_name}. "
                    "Не надсилаю припущення."
                )
            # This is DTEK's reported outage flag, not a physical voltage sensor.
            result[house_no] = bool(entry.has_current_outage)
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

    # First successful run: publish a baseline for both houses.
    first_run = not old_state
    for house_no, outage in new_state.items():
        changed = old_state.get(house_no) != outage
        if first_run or changed:
            await bot.send_message(
                chat_id=CHANNEL,
                text=make_message(house_no, outage, street_name, initial=first_run),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

    save_state(new_state)
    print(f"OK: {new_state}; street={street_name}")


if __name__ == "__main__":
    asyncio.run(main())
