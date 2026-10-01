import asyncio
import contextlib
import dataclasses
import logging
import time
import uuid

from .admin import Admin
from .database import now
from .images import fingerprint
from .lookup import Result, render
from .telegram import TelegramError, photo_files
from .transfer import import_record

log = logging.getLogger(__name__)


class JobStopped(Exception):
    pass


class Application:
    def __init__(self, settings, repo, telegram, lookup, bot_id):
        self.settings, self.repo, self.tg, self.lookup, self.bot_id = (
            settings,
            repo,
            telegram,
            lookup,
            bot_id,
        )
        self.admin = Admin(settings, repo, telegram, lookup, bot_id)
        self.last_lookup = {}
        self.owner = str(uuid.uuid4())

    async def run(self):
        # Railway can start the replacement before the previous process has fully drained.
        # Wait for graceful release or expiry; never run two pollers at the same time.
        deadline = time.monotonic() + 120
        announced = False
        while not await self.repo.acquire_instance(self.bot_id, self.owner):
            if time.monotonic() >= deadline:
                raise RuntimeError("Another instance of this bot still holds the database lease.")
            if not announced:
                log.info("Waiting for the previous bot process to release its lease.")
                announced = True
            await asyncio.sleep(2)
        try:
            webhook = await self.tg.call("getWebhookInfo")
            if webhook.get("url"):
                raise RuntimeError(
                    "This bot has a webhook. Remove it deliberately before using long polling."
                )
            async with asyncio.TaskGroup() as group:
                group.create_task(self.poll())
                group.create_task(self.instance_heartbeat())
                group.create_task(self.worker(["update"]))
                group.create_task(self.worker(["index"]))
                group.create_task(self.worker(["import", "export", "reindex", "broadcast"]))
                for _ in range(self.settings.lookup_concurrency):
                    group.create_task(self.worker(["lookup"]))
        finally:
            await self.repo.db.instances.delete_one({"_id": self.bot_id, "owner": self.owner})

    async def instance_heartbeat(self):
        while True:
            await asyncio.sleep(20)
            if not await self.repo.acquire_instance(self.bot_id, self.owner):
                raise RuntimeError(
                    "Lost the bot instance lease; stopping to avoid duplicate processing."
                )

    async def poll(self):
        key = f"offset:{self.bot_id}"
        state = await self.repo.db.settings.find_one({"_id": key})
        offset = state.get("value", 0) if state else 0
        while True:
            try:
                updates = await self.tg.call(
                    "getUpdates",
                    offset=offset,
                    timeout=25,
                    limit=100,
                    allowed_updates=[
                        "message",
                        "channel_post",
                        "edited_channel_post",
                        "callback_query",
                    ],
                )
                for update in updates:
                    await self.repo.enqueue(
                        "update",
                        {"update": update},
                        key=f"update:{self.bot_id}:{update['update_id']}",
                    )
                    offset = max(offset, update["update_id"] + 1)
                if updates:
                    await self.repo.db.settings.update_one(
                        {"_id": key}, {"$set": {"value": offset}}, upsert=True
                    )
            except TelegramError as exc:
                if exc.code in {401, 409}:
                    raise RuntimeError(
                        "Telegram rejected polling. Check token and ensure only one poller is running."
                    ) from None
                log.warning("Polling Telegram error code=%s", exc.code)
                await asyncio.sleep(max(3, min(exc.retry_after, 60)))
            except Exception as exc:
                log.warning("Polling failed: %s", type(exc).__name__)
                await asyncio.sleep(3)

    async def worker(self, kinds):
        while True:
            owner = str(uuid.uuid4())
            try:
                job = await self.repo.claim_job(kinds, owner)
            except Exception as exc:
                log.warning("Job claim failed: %s", type(exc).__name__)
                await asyncio.sleep(3)
                continue
            if not job:
                await asyncio.sleep(0.25)
                continue
            heartbeat = asyncio.create_task(self.job_heartbeat(job["_id"], owner))
            try:
                await self.execute(job, owner)
                await self.repo.finish_job(job["_id"], owner)
            except JobStopped:
                pass
            except Exception as exc:
                log.warning("Job failed kind=%s error=%s", job["kind"], type(exc).__name__)
                await self.repo.fail_job(job, owner, type(exc).__name__)
                if job["kind"] == "index" and job["attempts"] >= self.settings.job_max_attempts:
                    await self.repo.db.assets.update_one(
                        {"_id": job["payload"]["asset_id"]}, {"$set": {"index_status": "failed"}}
                    )
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def job_heartbeat(self, job_id, owner):
        while True:
            await asyncio.sleep(30)
            await self.repo.renew_job(job_id, owner)

    async def check_active(self, job, owner):
        current = await self.repo.db.jobs.find_one(
            {"_id": job["_id"], "owner": owner, "status": "running"}
        )
        if not current:
            raise JobStopped

    async def execute(self, job, owner):
        kind, payload = job["kind"], job["payload"]
        if kind == "update":
            await self.handle_update(payload["update"])
        elif kind == "lookup":
            started = time.monotonic()
            try:
                result = await self.lookup.query(payload["files"])
                await self.tg.send(payload["chat_id"], render(result))
                await self.repo.db.settings.update_one(
                    {"_id": "lookup_stats"},
                    {
                        "$inc": {
                            result.kind: 1,
                            "total_ms": int((time.monotonic() - started) * 1000),
                            "total": 1,
                        }
                    },
                    upsert=True,
                )
            except (ValueError, OSError) as exc:
                await self.tg.send(
                    payload["chat_id"], f"Cannot process this image: {str(exc)[:200]}"
                )
        elif kind == "index":
            await self.index_asset(payload)
        elif kind == "import":
            async for row in self.repo.db.import_rows.find(
                {
                    "import_id": payload["import_id"],
                    "position": {"$gt": payload.get("position", -1)},
                }
            ).sort("position", 1):
                await self.check_active(job, owner)
                status = await import_record(self.repo, row["record"], self.bot_id)
                await self.repo.db.jobs.update_one(
                    {"_id": job["_id"], "owner": owner, "status": "running"},
                    {
                        "$set": {"payload.position": row["position"]},
                        "$inc": {f"payload.{'conflicts' if status == 'conflict' else 'ready'}": 1},
                    },
                )
                if status == "conflict":
                    await self.repo.db.import_rows.update_one(
                        {"_id": row["_id"]}, {"$set": {"status": "conflict"}}
                    )
            current = await self.repo.db.jobs.find_one({"_id": job["_id"]})
            counts = current["payload"]
            await self.tg.send(
                payload["chat_id"],
                f"Import complete. Accepted: {counts['ready']}; conflicts skipped: {counts['conflicts']}. Job: {job['_id']}",
            )
            await self.repo.db.import_rows.delete_many(
                {"import_id": payload["import_id"], "status": {"$ne": "conflict"}}
            )
        elif kind == "export":
            await self.admin.export_job(payload)
        elif kind == "reindex":
            if payload.get("all"):
                cursor = self.repo.db.assets.find({}, {"_id": 1})
            else:
                cursor = self.repo.db.card_assets.find({"card_id": payload["card_id"]})
            count = 0
            async for item in cursor:
                aid = item["_id"] if payload.get("all") else item["asset_id"]
                await self.repo.enqueue(
                    "index", {"asset_id": aid, "force": True}, key=f"index:{aid}", reset=True
                )
                count += 1
            await self.tg.send(
                payload["chat_id"],
                f"Queued {count} image indexing jobs. See /jobs and /stats for results.",
            )
        elif kind == "broadcast":
            await self.broadcast(job, owner)
        else:
            raise ValueError("Unknown job kind.")

    async def index_asset(self, payload):
        aid = payload["asset_id"]
        asset = await self.repo.db.assets.find_one({"_id": aid})
        if not asset or (asset.get("index_status") == "ready" and not payload.get("force")):
            return
        refs = await self.repo.db.telegram_files.find(
            {"asset_id": aid, f"bot_files.{self.bot_id}": {"$exists": True}}
        ).to_list()
        refs.sort(
            key=lambda r: ((r.get("width") or 0) * (r.get("height") or 0), r.get("file_size", 0)),
            reverse=True,
        )
        if not refs:
            await self.repo.db.assets.update_one(
                {"_id": aid}, {"$set": {"index_status": "unavailable"}}
            )
            return
        last_error = None
        for ref in refs:
            try:
                data = await self.tg.download(
                    ref["bot_files"][str(self.bot_id)], self.settings.max_download_mb * 1024 * 1024
                )
                hashed = await asyncio.to_thread(fingerprint, data, self.settings.max_image_pixels)
                await self.repo.db.assets.update_one(
                    {"_id": aid},
                    {
                        "$set": {
                            **dataclasses.asdict(hashed),
                            "index_status": "ready",
                            "hash_version": 1,
                            "preprocessing_version": 1,
                            "updated_at": now(),
                        }
                    },
                )
                await self.repo.bump_generation()
                return
            except TelegramError as exc:
                if exc.code not in {400, 404}:
                    raise
                last_error = exc
        if last_error:
            raise last_error

    async def broadcast(self, job, owner):
        payload = job["payload"]
        cursor = self.repo.db.users.find(
            {"subscribed": True, "_id": {"$gt": payload.get("last_user", 0)}}
        ).sort("_id", 1)
        async for user in cursor:
            await self.check_active(job, owner)
            consent = await self.repo.db.users.find_one({"_id": user["_id"], "subscribed": True})
            sent = False
            if consent:
                try:
                    await self.tg.broadcast_one(user["_id"], payload["text"])
                    sent = True
                except TelegramError as exc:
                    if exc.code in {400, 403}:
                        await self.repo.db.users.update_one(
                            {"_id": user["_id"]}, {"$set": {"subscribed": False}}
                        )
                    else:
                        raise
            await self.repo.db.jobs.update_one(
                {"_id": job["_id"], "owner": owner},
                {
                    "$set": {"payload.last_user": user["_id"]},
                    "$inc": {"payload.sent" if sent else "payload.failed": 1},
                },
            )
            await asyncio.sleep(0.06)
        current = await self.repo.db.jobs.find_one({"_id": job["_id"]})
        await self.tg.send(
            payload["chat_id"],
            f"Broadcast complete. Sent: {current['payload']['sent']}; skipped/failed: {current['payload']['failed']}.",
        )

    async def handle_update(self, update):
        if "callback_query" in update:
            await self.admin.callback(update["callback_query"])
            return
        message = update.get("channel_post") or update.get("edited_channel_post")
        if message:
            await self.repo.collect(message, self.bot_id)
            return
        message = update.get("message")
        if not message or message.get("from", {}).get("is_bot"):
            return
        chat_id, user_id = message["chat"]["id"], message.get("from", {}).get("id")
        if not user_id:
            return
        is_private = message["chat"].get("type") == "private"
        if is_private:
            await self.repo.db.users.update_one(
                {"_id": user_id},
                {"$setOnInsert": {"subscribed": False, "created_at": now()}},
                upsert=True,
            )
        text = message.get("text", "")
        command = text.split(maxsplit=1)[0].split("@")[0] if text.strip() else ""
        if command in {"/start", "/help"} and user_id not in self.settings.admin_ids:
            await self.tg.send(
                chat_id,
                "Send a card photo for lookup. Exact file first, then image matching."
                + "\n/find <exact name, anime or card ID>\n/subscribe — receive announcements\n/unsubscribe — stop announcements",
            )
        elif command in {"/subscribe", "/unsubscribe"}:
            if not is_private:
                await self.tg.send(chat_id, "Manage subscriptions in private chat with this bot.")
                return
            await self.repo.db.users.update_one(
                {"_id": user_id}, {"$set": {"subscribed": command == "/subscribe"}}
            )
            await self.tg.send(
                chat_id, "Subscribed." if command == "/subscribe" else "Unsubscribed."
            )
        elif command == "/find":
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                await self.tg.send(chat_id, "/find <exact name, anime or card ID>")
                return
            cards = await self.repo.search(parts[1].strip())
            await self.tg.send(chat_id, render(Result("possible" if cards else "not_found", cards)))
        elif command.startswith("/"):
            if command == "/start":
                message = {**message, "text": "/admin"}
            try:
                await self.admin.handle(message)
            except (ValueError, TypeError, KeyError) as exc:
                await self.tg.send(chat_id, f"Invalid request: {str(exc)[:300]}")
        elif files := photo_files(message):
            current = time.monotonic()
            if (
                current - self.last_lookup.get(user_id, -1e9)
                < self.settings.lookup_cooldown_seconds
            ):
                await self.tg.send(chat_id, "Please wait a moment before the next lookup.")
                return
            if len(self.last_lookup) >= 10_000:
                cutoff = current - self.settings.lookup_cooldown_seconds
                self.last_lookup = {
                    key: value for key, value in self.last_lookup.items() if value >= cutoff
                }
            self.last_lookup[user_id] = current
            await self.repo.enqueue(
                "lookup",
                {"chat_id": chat_id, "files": files},
                key=f"lookup:{self.bot_id}:{update['update_id']}",
            )
