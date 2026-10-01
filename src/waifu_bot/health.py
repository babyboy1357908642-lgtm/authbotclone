"""Local process/database lease health check; makes no Telegram requests."""

import asyncio

from .config import Settings
from .database import Repository, now


async def check():
    settings = Settings.from_env()
    repo = Repository(settings)
    try:
        await repo.client.admin.command("ping")
        bot_id = int(settings.token.split(":", 1)[0])
        live = await repo.db.instances.find_one({"_id": bot_id, "lease_until": {"$gt": now()}})
        return 0 if live else 1
    finally:
        await repo.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(check()))
    except Exception:
        raise SystemExit(1) from None
