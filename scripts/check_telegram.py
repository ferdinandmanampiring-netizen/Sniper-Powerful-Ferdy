import asyncio
import sys
from pathlib import Path

# Ensure project root is on sys.path when running from /scripts
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.telegram_agent import TelegramAgent


async def main() -> None:
    agent = TelegramAgent()
    await agent.client.connect()
    print("CONNECT_OK:", agent.client.is_connected())
    print("AUTHORIZED:", await agent.client.is_user_authorized())
    await agent.client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

