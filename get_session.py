import asyncio

# Создаем событие цикла ДО импорта библиотеки (фикс для Python 3.14)
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from pyrogram import Client

# Впиши свои ключи сюда:
API_ID = 33614541  # Твое число без кавычек
API_HASH = "779178fa1e19007591e538487243defb"  # Твой полный hash в кавычках


async def main():
    async with Client("temp_session", api_id=API_ID, api_hash=API_HASH) as app:
        session_str = await app.export_session_string()
        print("\n" + "=" * 50)
        print("ГОТОВО! ВОТ ТВОЯ СТРОКА СЕССИИ (SESSION_STRING):\n")
        print(session_str)
        print("=" * 50 + "\n")


if __name__ == "__main__":
    loop.run_until_complete(main())