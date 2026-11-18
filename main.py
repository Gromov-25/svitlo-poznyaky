
import asyncio
import json
import os
import re
from pathlib import Path
from datetime import datetime, timedelta, date
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
    "Драгоманова, 25": ("Драгоманова", "25"),
    "Драгоманова, 42": ("Драгоманова", "42"),
    "Драгоманова, 42А": ("Драгоманова", "42/А"),
    "Урлівська, 36": ("Урлівська", "36"),
    "Петра Григоренка, 21": ("Петра Григоренка", "21"),
    "Петра Григоренка, 22/20": ("Петра Григоренка", "22/20"),
    "Анни Ахматової, 14Б": ("Анни Ахматової", "14/Б"),
}


def normalize_house(number):
    return (
        str(number).strip().upper()
        .replace(" ", "").replace("/", "")
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
            return datetime.fromtimestamp(
                value, KYIV
            ).strftime("%d.%m.%Y %H:%M")
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
            return datetime.strptime(
                value, fmt
            ).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            pass

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
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


def slot_start_minutes(slot_key, time_zone):
    label = (time_zone or {}).get(str(slot_key), "")
    match = re.search(r"(\d{1,2}):(\d{2})", str(label))

    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour < 24 and minute < 60:
            return hour * 60 + minute

    # DTEK schedule slots are hourly indices (0–23).
    try:
        number = int(slot_key)
        if 0 <= number <= 23:
            return number * 60
    except (TypeError, ValueError):
        pass

    return None


def get_slot_outage_interval(slot_key, status, time_zone):
    value = getattr(status, "value", status)
    value = str(value).strip().lower()

    start = slot_start_minutes(slot_key, time_zone)

    if start is None:
        return None

    # DTEK encodes each schedule slot as one hour.
    # NO = full hour, FIRST = first half,
    # SECOND = second half.
    if value in ("no",):
        return start, start + 60, False

    if value in ("maybe", "?"):
        return start, start + 60, True

    if value in ("first",):
        return start, start + 30, False

    if value in ("mfirst",):
        return start, start + 30, True

    if value in ("second",):
        return start + 30, start + 60, False

    if value in ("msecond",):
        return start + 30, start + 60, True

    return None


def get_fact_day(fact, group_id, day):
    if not fact:
        return None

    method = getattr(fact, "get_group_for_date", None)

    if callable(method):
        try:
            return method(day, group_id)
        except Exception:
            pass

    return None


def get_day_slots(response, group_id, day):
    # First priority: confirmed DTEK fact schedule.
    fact = getattr(response, "fact", None)
    fact_day = get_fact_day(fact, group_id, day)

    if fact_day is not None:
        slots = getattr(fact_day, "slots", None)
        if slots:
            return slots, True

    # Second priority: published weekly preset.
    preset = getattr(response, "preset", None)

    if not preset or not getattr(preset, "is_active", False):
        return None, False

    groups = getattr(preset, "groups", {}) or {}
    group = groups.get(str(group_id))

    if not group:
        return None, False

    days = getattr(group, "days", {}) or {}
    day_schedule = (
        days.get(day.isoweekday())
        or days.get(str(day.isoweekday()))
    )

    if not day_schedule:
        return None, False

    slots = getattr(day_schedule, "slots", {}) or {}

    if not slots:
        return None, False

    return slots, True


def find_next_outage(response, group_ids):
    now = datetime.now(KYIV)
    candidates = []
    schedule_found = False

    preset = getattr(response, "preset", None)
    time_zone = (
        getattr(preset, "time_zone", {}) or {}
        if preset else {}
    )

    fact = getattr(response, "fact", None)
    fact_time_zone = getattr(fact, "time_zone", None)

    if fact_time_zone:
        time_zone = fact_time_zone

    for day_offset in range(8):
        day = (now + timedelta(days=day_offset)).date()

        for group_id in group_ids:
            slots, available = get_day_slots(
                response, group_id, day
            )

            if not available:
                continue

            schedule_found = True

            for slot_key, status in slots.items():
                interval = get_slot_outage_interval(
                    slot_key, status, time_zone
                )

                if not interval:
                    continue

                start_minute, end_minute, possible = interval

                start_dt = datetime(
                    day.year, day.month, day.day,
                    start_minute // 60,
                    start_minute % 60,
                    tzinfo=KYIV,
                )

                end_dt = datetime(
                    day.year, day.month, day.day,
                    end_minute // 60,
                    end_minute % 60,
                    tzinfo=KYIV,
                )

                if end_dt <= now:
                    continue

                # Do not show an outage which has already started.
                if start_dt < now:
                    continue

                candidates.append(
                    (start_dt, end_dt, possible)
                )

    if not candidates:
        return None, schedule_found

    candidates.sort(key=lambda item: item[0])

    # Join consecutive outage intervals into one period.
    first_start, first_end, first_possible = candidates[0]
    final_end = first_end
    any_possible = first_possible

    for start_dt, end_dt, possible in candidates[1:]:
        if start_dt == final_end:
            final_end = end_dt
            any_possible = any_possible or possible
        else:
            break

    if first_start.date() == now.date():
        day_label = "сьогодні"
    elif first_start.date() == (now + timedelta(days=1)).date():
        day_label = "завтра"
    else:
        day_label = first_start.strftime("%d.%m")

    result = (
        f"{day_label} "
        f"{first_start.strftime('%H:%M')}–"
        f"{final_end.strftime('%H:%M')}"
    )

    if any_possible:
        result += " (можливе)"

    return result, schedule_found


async def get_statuses():
    cookies, csrf_token = await get_cleared_cookies(
        SCHEDULE_URL
    )

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

        page_update = (
            get_page_update_time(page_response.text)
            if page_response.status_code == 200
            else None
        )

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
                    street_cache[street_name] = (
                        await client.get_home_num(
                            CITY, street_name
                        )
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

                groups = list(entry.group_ids or [])

                next_outage, schedule_found = (
                    find_next_outage(response, groups)
                    if groups and not entry.is_excluded
                    else (None, False)
                )

                graph_updated = None

                if response.fact:
                    graph_updated = getattr(
                        response.fact, "update", None
                    )

                if not graph_updated and response.preset:
                    graph_updated = getattr(
                        response.preset, "update", None
                    )

                results[address] = {
                    "outage": bool(entry.has_current_outage),
                    "reason": str(
                        entry.sub_type or ""
                    ).strip(),
                    "outage_type": str(
                        entry.outage_type or ""
                    ).strip(),
                    "start_date": str(
                        entry.start_date or ""
                    ),
                    "end_date": str(
                        entry.end_date or ""
                    ),
                    "has_schedule": (
                        not bool(entry.is_excluded)
                        and schedule_found
                    ),
                    "groups": groups,
                    "next_outage": next_outage,
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
        parts.append(
            f"📍 Київ, {prefix} {escape(address)}"
        )

        if outage:
            if info["start_date"]:
                parts.append(
                    f"🕒 Початок: "
                    f"{escape(info['start_date'])}"
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
        else:
            if not info["has_schedule"]:
                next_outage_text = "Немає актуальних даних графіка"
            elif info["next_outage"]:
                next_outage_text = info["next_outage"]
            else:
                next_outage_text = (
                    "Планових відключень не прогнозується"
                )

            parts.append(
                f"🕒 Орієнтовне відключення: "
                f"{escape(next_outage_text)}"
            )

        if info["groups"]:
            parts.append(
                f"📊 Черга: "
                f"{escape(', '.join(info['groups']))}"
            )

        parts.extend(["", "━━━━━━━━━━━━━━", ""])

    while parts and parts[-1] == "":
        parts.pop()

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

    graph_updates = []

    for info in addresses.values():
        updated = format_dtek_datetime(
            info.get("graph_updated")
        )

        if updated and updated not in graph_updates:
            graph_updates.append(updated)

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
        raise RuntimeError(
            "Не знайдено BOT_TOKEN у Secrets."
        )

    old_state = load_state()

    current, page_update = await get_statuses()

    addresses, changed_addresses = prepare_state(
        current, old_state
    )

    if not old_state or changed_addresses:
        async with Bot(token=token) as bot:
            message = await bot.send_message(
                chat_id=CHANNEL,
                text=make_summary(addresses, page_update),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        print(
            f"Повідомлення створено. ID: "
            f"{message.message_id}"
        )
        print(
            "Змінені адреси:",
            sorted(changed_addresses)
        )
    else:
        print(
            "Змін немає. Повідомлення не надсилаємо."
        )

    save_state({"addresses": addresses})

    print(
        "Перевірені адреси:",
        list(current.keys())
    )
    print(
        "Оновлення інформації ДТЕК:",
        page_update
    )

    for address, info in current.items():
        print(
            f"{address}: "
            f"групи={info['groups']}, "
            f"наступне відключення={info['next_outage']}, "
            f"графік доступний={info['has_schedule']}"
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(
            f"ПОМИЛКА МОНІТОРИНГУ: "
            f"{type(error).__name__}: {error}"
        )
        raise
