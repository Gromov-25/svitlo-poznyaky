
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
    "Петра Григоренка, 22/20": ("Петра Григоренка", "22/20"),
}


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


def format_time(value):
    if not value:
        return None

    if isinstance(value, datetime):
        if value.tzinfo:
            value = value.astimezone(KYIV)
        return value.strftime("%H:%M")

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, KYIV).strftime("%H:%M")
        except (ValueError, OSError):
            return None

    value = str(value).strip()

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone(KYIV)
        return parsed.strftime("%H:%M")
    except ValueError:
        return value


def get_schedule_slots(response, entry):
    if not entry.group_ids:
        return []

    result = []
    fact = response.fact
    preset = response.preset
    time_zone = getattr(preset, "time_zone", None) or {}

    for group_id in entry.group_ids:
        slots = None

        if fact:
            slots = fact.get_group_today(group_id)

        if slots is None and preset:
            group = preset.groups.get(group_id)
            if group:
                day = group.get_day(datetime.now(KYIV).isoweekday())
                if day:
                    slots = day.slots

        if not slots:
            continue

        for slot_id, status in sorted(
            slots.items(),
            key=lambda item: int(item[0]) if str(item[0]).isdigit() else 999,
        ):
            value = getattr(status, "value", str(status))

            if value not in {
                "no", "first", "second",
                "maybe", "mfirst", "msecond",
            }:
                continue

            label = time_zone.get(slot_id)

            if isinstance(label, list):
                label = label[0] if label else None

            if label:
                prefix = (
                    "Можливе відключення"
                    if value in {"maybe", "mfirst", "msecond"}
                    else "Відключення"
                )
                result.append(f"{prefix}: {label}")

    return list(dict.fromkeys(result))


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
                elif "урлівська" in lowered or "урловская" in lowered:
                    found_streets["Урлівська"] = name
                elif "григоренка" in lowered:
                    found_streets["Петра Григоренка"] = name

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
                    "schedule": get_schedule_slots(response, entry),
                    "graph_updated": graph_updated,
                }

            return results

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


def make_summary(addresses):
    parts = []

    for address, info in addresses.items():
        outage = info["outage"]
        reason = info["reason"]
        outage_type = info["outage_type"]

        if outage:
            if "авар" in reason.casefold():
                title = "🚨 <b>АВАРІЙНЕ ВІДКЛЮЧЕННЯ СВІТЛА</b>"
            elif outage_type == "1":
                title = "🚨 <b>ПЛАНОВЕ ВІДКЛЮЧЕННЯ СВІТЛА</b>"
            else:
                title = "🚨 <b>ВІДКЛЮЧЕННЯ СВІТЛА</b>"
        elif not info["has_schedule"]:
            title = "⚪ <b>ГРАФІК ВІД ДТЕК ВІДСУТНІЙ</b>"
        else:
            title = "🟢 <b>СВІТЛО Є</b>"

        prefix = (
            "просп."
            if "Григоренка" in address
            else "вул."
        )

        parts.append(title)
        parts.append(
            f"📍 Київ, {prefix} {escape(address)}"
        )

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

        elif not info["has_schedule"]:
            parts.append(
                "ℹ️ ДТЕК не надав графік для цієї адреси."
            )

        if info["groups"]:
            parts.append(
                f"📊 Черга: {escape(', '.join(info['groups']))}"
            )

        if info["schedule"]:
            parts.append("")
            parts.append("<b>Графік відключень на сьогодні:</b>")

            for item in info["schedule"]:
                parts.append(f"▫️ {escape(item)}")

        parts.extend(["", "━━━━━━━━━━━━━━", ""])

    while parts and parts[-1] == "":
        parts.pop()

    graph_updates = []

    for info in addresses.values():
        updated = format_time(info.get("graph_updated"))
        if updated and updated not in graph_updates:
            graph_updates.append(updated)

    parts.append("")

    if graph_updates:
        parts.append(
            f"🕒 <b>Оновлено графік о {escape(graph_updates[-1])}</b>"
        )
    else:
        parts.append(
            "🕒 ДТЕК не передав час оновлення графіка."
        )

    return "\n".join(parts)


async def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не знайдено BOT_TOKEN у Secrets.")

    old_state = load_state()
    current = await get_statuses()

    addresses, changed_addresses = prepare_state(
        current,
        old_state,
    )

    if not old_state or changed_addresses:
        async with Bot(token=token) as bot:
            message = await bot.send_message(
                chat_id=CHANNEL,
                text=make_summary(addresses),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        print(f"Повідомлення створено. ID: {message.message_id}")
        print("Змінені адреси:", sorted(changed_addresses))
    else:
        print("Змін немає. Повідомлення не надсилаємо.")

    save_state({"addresses": addresses})
    print("Перевірені адреси:", list(current.keys()))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(
            f"ПОМИЛКА МОНІТОРИНГУ: "
            f"{type(error).__name__}: {error}"
        )
        raise
