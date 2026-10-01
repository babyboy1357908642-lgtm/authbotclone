import argparse
import asyncio
import logging
import signal

from .config import Settings
from .database import Repository
from .lookup import Lookup
from .runtime import Application
from .telegram import Telegram


async def run(check=False):
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for name in ("httpx", "httpcore", "pymongo"):
        logging.getLogger(name).setLevel(logging.WARNING)
    repo, telegram = Repository(settings), Telegram(settings.token)
    try:
        await repo.initialize()
        identity = await telegram.call("getMe")
        logging.info("Database ready; Telegram bot ID=%s", identity["id"])
        if check:
            async for source in repo.db.source_channels.find({"enabled": True}):
                member = await telegram.call(
                    "getChatMember", chat_id=source["_id"], user_id=identity["id"]
                )
                if member["status"] not in {"administrator", "creator", "member"}:
                    raise RuntimeError("Bot lacks access to a configured source channel.")
            print("MongoDB, indexes, Telegram identity and configured channel memberships checked.")
            return
        lookup = Lookup(settings, repo, telegram)
        app = Application(settings, repo, telegram, lookup, identity["id"])
        await app.run()
    finally:
        await telegram.close()
        await repo.close()


async def run_with_signals(check=False):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed = False
    try:
        try:
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
            installed = True
        except (NotImplementedError, RuntimeError):
            # Windows does not implement add_signal_handler; Ctrl+C still works.
            pass
        await run(check)
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)


def main():
    parser = argparse.ArgumentParser(description="Standalone Waifu Card Collector & Lookup Bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check configuration, MongoDB and Telegram access, then exit",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run_with_signals(args.check))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception as exc:
        logging.error(
            "Stopped (%s). Verify .env, database connectivity and single-instance polling.",
            type(exc).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
