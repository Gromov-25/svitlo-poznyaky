import asyncio
import json
import os
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape, unescape

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
    "Петра Григоренка, 22/20": ("Петра Григоренка", "22/20"),
    "Анни Ахматової, 14Б": ("Анни Ахматової", "14/Б"),
}


def normalize_house(number):
    return (
        str(number)
        .strip()
        .upper()
        .replace(" ", "")
        .replace("/", "")
    )


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


def format_dtek_datetime(value):
    if not value:
        return None

    if isinstance(value, datetime):
        if value.tzinfo:
            value = value.astimezone(KYIV)
        return value.strftime("%d.%m.%Y %H:%M")

    if isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(value, KYIV)
            return parsed.strftime("%d.%m.%Y %H:%M")
        except (ValueError, OSError):
            return None

    value = str(value).strip()

    formats = (
        "%H:%M %d.%m.%Y",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    )

    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            pass

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone(KYIV)
        return parsed.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def get_page_update_time(page_html):
    text = unescape(re.sub(r"<[^>]+>", " ", page_html))
    text = re.sub(r"\s+", " ", text)

    match = re.search(
        r"Дата\s+оновлення\s+інформації\s*[–—-]\s*"
        r"(\d{2}:\d{2}\s+\d{2}\.\d{2}\.\d{4})",
        text,
        re.IGNORECASE,
    )

    return match.group(1) if match else None


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
        page_response = await session.get(SCHEDULE_URL)

        if page_response.status_code == 200:
            page_update = get_page_update_time(page_response.text)
        else:
            page_update = None

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

                elif "урлівська" in lowered or "урловская" in lowered:
                    found_streets["Урлівська"] = name

                elif "григоренка" in lowered:
                    found_streets["Петра Григоренка"] = name

                elif "ахматов" in lowered:
                    found_streets["Анни Ахматової"] = name

            results = {}
            street_cache = {}

            for address, (street_key, house_number) in ADDRESSES.items():
                street_name = found_streets.get(street_key)

                if not street_name:
                    raise RuntimeError(
                        f"ДТЕК не знайшов вулицю: {street_key}"
                    )

                if street_name not in street_cache:
                    street_cache[street_name] = await client.get_home_num(
                        CITY,
                        street_name,
                    )

                response = street_cache[street_name]

                houses = {
                    normalize_house(number): entry
                    for number, entry in response.houses.items()
                }

                entry = houses.get(normalize_house(house_number))

                if entry is None:
                    raise RuntimeError(
                        f"ДТЕК не повернув будинок {address}"
                    )

                graph_updated = None

                if response.fact:
                    graph_updated = response.fact.update

                results[address] = {
                    "outage": bool(entry.has_current_outage),
                    "reason": str(entry.sub_type or "").strip(),
                    "outage_type": str(entry.outage_type or "").strip(),
                    "start_date": str(entry.start_date or ""),
                    "end_date": str(entry.end_date or ""),
                    "has_schedule": not bool(entry.is_excluded),
                    "groups": list(entry.group_ids or []),
                    "graph_updated": graph_updated,
                }

            return results, page_update

    finally:
        await session.close()


def prepare_state(current, old_state):
    previous = old_state.get("addresses", old_state)
    new_addresses = {}
    changed_addresses = set()

    for address, info in current.items():
        old = previous.get(address)

        if isinstance(old, bool):
            changed = old != info["outage"]
        elif isinstance(old, dict):
            changed = old != info
        else:
            changed = True

        if changed:
            changed_addresses.add(address)

        new_addresses[address] = info

    return new_addresses, changed_addresses


def make_summary(addresses, page_update):
    parts = []

    for address, info in addresses.items():
        outage = info["outage"]
        reason = info["reason"]

        if outage and "авар" in reason.casefold():
            title = "🚨 <b>АВАРІЙНЕ ВІДКЛЮЧЕННЯ</b>"
        elif outage:
            title = "🔴 <b>СВІТЛА НЕМАЄ</b>"
        else:
            title = "🟢 <b>СВІТЛО Є</b>"

        prefix = (
            "просп."
            if "Григоренка" in address
            else "вул."
        )

        parts.append(title)
        parts.append(f"📍 Київ, {prefix} {escape(address)}")

        if outage:
            if info["start_date"]:
                parts.append(
                    f"🕒 Початок: {escape(info['start_date'])}"
                )

            if reason:
                parts.append(
                    f"🔧 Причина: {escape(reason)}"
                )

            if info["end_date"]:
                parts.append(
                    f"🟢 Орієнтовне відновлення: "
                    f"{escape(info['end_date'])}"
                )

        if info["groups"]:
            parts.append(
                f"📊 Черга: {escape(', '.join(info['groups']))}"
            )

        parts.extend(["", "━━━━━━━━━━━━━━", ""])

    while parts and parts[-1] == "":
        parts.pop()

    graph_updates = []

    for info in addresses.values():
        updated = format_dtek_datetime(
            info.get("graph_updated")
        )

        if updated and updated not in graph_updates:
            graph_updates.append(updated)

    parts.append("")

    if page_update:
        parts.append(
            f"🕒 <b>Остання перевірка ДТЕК: "
            f"{escape(page_update)}</b>"
        )
    else:
        parts.append(
            "🕒 <b>Час оновлення інформації ДТЕК: "
            "не вдалося отримати</b>"
        )

    if graph_updates:
        parts.append(
            f"📅 <b>Графік ГПВ востаннє оновлено: "
            f"{escape(graph_updates[-1])}</b>"
        )
    else:
        parts.append(
            "📅 <b>Час оновлення графіка ГПВ: "
            "не вдалося отримати</b>"
        )

    return "\n".join(parts)


async def main():
    token = os.environ.get("BOT_TOKEN")

    if not token:
        raise RuntimeError("Не знайдено BOT_TOKEN у Secrets.")

    old_state = load_state()

    current, page_update = await get_statuses()

    addresses, changed_addresses = prepare_state(
        current,
        old_state,
    )

    if not old_state or changed_addresses:
        async with Bot(token=token) as bot:
            message = await bot.send_message(
                chat_id=CHANNEL,
                text=make_summary(addresses, page_update),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        print(f"Повідомлення створено. ID: {message.message_id}")
        print("Змінені адреси:", sorted(changed_addresses))
    else:
        print("Змін немає. Повідомлення не надсилаємо.")

    save_state({"addresses": addresses})

    print("Перевірені адреси:", list(current.keys()))
    print("Оновлення інформації ДТЕК:", page_update)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(
            f"ПОМИЛКА МОНІТОРИНГУ: "
            f"{type(error).__name__}: {error}"
        )
        raise
