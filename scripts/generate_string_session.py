import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    """
    Jalankan script ini SEKALI untuk generate TELETHON_SESSION (StringSession).
    Setelah muncul string session, simpan ke file .env:
      TELETHON_SESSION=PASTE_DI_SINI
    """
    load_dotenv()
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")

    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID / TELEGRAM_API_HASH belum terisi di .env")

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.start()  # akan minta phone + code (sekali saja)
    session_str = client.session.save()
    print("\n=== TELETHON_SESSION (copy ke .env) ===\n")
    print(session_str)
    print("\n=== END ===\n")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

