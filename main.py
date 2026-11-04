
import asyncio
import os

from dtek_client import DtekClient
from dtek_client.browser_auth import get_cleared_cookies
from curl_cffi.requests import AsyncSession

BASE_URL = "https://www.dtek-kem.com.ua"
SCHEDULE_URL = f"{BASE_URL}/ua/shutdowns"
AJAX_URL = f"{BASE_URL}/ua/ajax"
CITY = "м. Київ"


async def main():
    cookies, csrf_token = await get_cleared_cookies(SCHEDULE_URL)

    headers = {
        "User-Agent": "Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": BASE_URL,
        "Referer": SCHEDULE_URL,
        "X-Requested-With": "XMLHttpRequest",
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

            print("Получаем список улиц...")
            streets = await client.get_streets(CITY)

            for street in streets:
                name = street.name

                if "драгоманова" in name.casefold() or "урлівська" in name.casefold() or "урловская" in name.casefold():
                    print(f"\nУЛИЦА: {name}")

                    response = await client.get_home_num(CITY, name)

                    print("ДОМА:")
                    for number in response.houses.keys():
                        print(f" - {number}")

            print("\nПРОВЕРКА ЗАВЕРШЕНА")

    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
