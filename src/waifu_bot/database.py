import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from pymongo import ASCENDING, AsyncMongoClient, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

from .config import Settings
from .parser import normalize, parse_caption
from .telegram import photo_files


def now():
    return datetime.now(UTC)


def stable_id(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


class Repository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncMongoClient(
            settings.mongo_uri,
            maxPoolSize=30,
            minPoolSize=1,
            serverSelectionTimeoutMS=5000,
            timeoutMS=15000,
            tz_aware=True,
        )
        self.db = self.client[settings.mongo_database]

    async def initialize(self):
        await self.client.admin.command("ping")
        await self.db.cards.create_index(
            [("catalog_id", ASCENDING), ("external_card_id", ASCENDING)],
            unique=True,
        )
        await self.db.cards.create_index("name_normalized")
        await self.db.cards.create_index("external_card_id")
        await self.db.cards.create_index("name_aliases")
        await self.db.cards.create_index("anime_aliases")
        await self.db.cards.create_index([("anime_normalized", 1), ("name_normalized", 1)])
        await self.db.assets.create_index([("index_status", 1), ("updated_at", 1)])
        await self.db.assets.create_index("sha256")
        await self.db.telegram_files.create_index("asset_id")
        await self.db.card_assets.create_index([("card_id", 1), ("asset_id", 1)], unique=True)
        await self.db.card_assets.create_index("asset_id")
        await self.db.card_assets.create_index("sources")
        await self.db.source_messages.create_index(
            [("channel_id", 1), ("message_id", 1)], unique=True
        )
        await self.db.source_messages.create_index("card_id")
        await self.db.source_messages.create_index("parse_status")
        await self.db.jobs.create_index([("status", 1), ("next_attempt_at", 1), ("kind", 1)])
        await self.db.jobs.create_index("expires_at", expireAfterSeconds=0)
        await self.db.audit_logs.create_index("created_at")
        await self.db.users.create_index([("subscribed", 1), ("_id", 1)])
        await self.db.settings.update_one(
            {"_id": "collector"},
            {"$setOnInsert": {"enabled": self.settings.collector_enabled}},
            upsert=True,
        )
        for channel_id in self.settings.channel_ids:
            await self.db.source_channels.update_one(
                {"_id": channel_id},
                {
                    "$setOnInsert": {
                        "enabled": True,
                        "catalog_id": self.settings.default_catalog,
                        "aliases": {},
                    }
                },
                upsert=True,
            )

    async def close(self):
        await self.client.close()

    async def source_config(self, channel_id: int):
        global_config = await self.db.settings.find_one({"_id": "collector"})
        if not global_config or not global_config["enabled"]:
            return None
        return await self.db.source_channels.find_one({"_id": channel_id, "enabled": True})

    async def collect(
        self, message: dict, bot_id: int, *, catalog: str | None = None, force: bool = False
    ) -> dict:
        channel_id = message["chat"]["id"]
        source_key = f"{channel_id}:{message['message_id']}"
        config = await self.source_config(channel_id)
        if not config and not force:
            return {"status": "ignored"}
        config = config or {"catalog_id": catalog or self.settings.default_catalog, "aliases": {}}
        files = photo_files(message)
        if not files:
            return {"status": "unsupported"}
        catalog = catalog or config["catalog_id"]
        parsed = parse_caption(message.get("caption", ""), config.get("aliases"))
        # Same largest Telegram file always picks the same asset. Record all other sizes as aliases.
        largest = max(
            files, key=lambda f: (f.get("width", 0) * f.get("height", 0), f.get("file_size", 0))
        )
        known = await self.db.telegram_files.find_one({"_id": largest["file_unique_id"]})
        asset_id = known["asset_id"] if known else stable_id("telegram", largest["file_unique_id"])
        await self.db.assets.update_one(
            {"_id": asset_id},
            {
                "$setOnInsert": {
                    "index_status": "pending",
                    "created_at": now(),
                    "updated_at": now(),
                    "width": largest.get("width"),
                    "height": largest.get("height"),
                }
            },
            upsert=True,
        )
        await self.db.telegram_files.bulk_write(
            [
                UpdateOne(
                    {"_id": item["file_unique_id"]},
                    {
                        "$setOnInsert": {
                            "asset_id": asset_id,
                            "width": item.get("width"),
                            "height": item.get("height"),
                            "file_size": item.get("file_size", 0),
                        },
                        "$set": {f"bot_files.{bot_id}": item["file_id"]},
                    },
                    upsert=True,
                )
                for item in files
            ],
            ordered=False,
        )
        # A size previously observed alone may already map to another asset. Link all aliases safely.
        asset_ids = {asset_id}
        async for ref in self.db.telegram_files.find(
            {"_id": {"$in": [f["file_unique_id"] for f in files]}}
        ):
            asset_ids.add(ref["asset_id"])
        card_id = None
        status = "needs_review"
        if parsed.ready:
            card_id, status = await self.upsert_card(catalog, parsed.fields)
            if status != "conflict":
                await self.db.card_assets.bulk_write(
                    [
                        UpdateOne(
                            {"card_id": card_id, "asset_id": aid},
                            {
                                "$setOnInsert": {"verified": True},
                                "$addToSet": {"sources": source_key},
                            },
                            upsert=True,
                        )
                        for aid in asset_ids
                    ],
                    ordered=False,
                )
        source = {
            "channel_id": channel_id,
            "message_id": message["message_id"],
            "caption_raw": message.get("caption", ""),
            "caption_entities": message.get("caption_entities", []),
            "forward_origin": message.get("forward_origin"),
            "catalog_id": catalog,
            "message_date": message.get("date"),
            "card_id": card_id,
            "asset_id": asset_id,
            "asset_ids": sorted(asset_ids),
            "metadata_candidates": parsed.fields,
            "parse_status": status,
            "issues": parsed.issues,
            "parser_version": 1,
            "updated_at": now(),
        }
        await self.db.source_messages.update_one(
            {"channel_id": channel_id, "message_id": message["message_id"]},
            {"$set": source, "$setOnInsert": {"created_at": now()}},
            upsert=True,
        )
        # Caption/image edits must not leave the old source-to-card mapping trusted.
        obsolete = {"sources": source_key}
        if status == "ready":
            obsolete["$nor"] = [{"card_id": card_id, "asset_id": {"$in": list(asset_ids)}}]
        await self.db.card_assets.update_many(obsolete, {"$pull": {"sources": source_key}})
        await self.db.card_assets.delete_many({"sources": {"$size": 0}, "manual": {"$ne": True}})
        await self.db.source_channels.update_one(
            {"_id": channel_id},
            {
                "$max": {"last_seen_message_id": message["message_id"]},
                "$set": {"last_received_at": now()},
            },
        )
        await self.enqueue("index", {"asset_id": asset_id}, key=f"index:{asset_id}", reset=True)
        await self.bump_generation()
        return {"status": status, "card_id": card_id, "issues": parsed.issues}

    async def upsert_card(self, catalog: str, fields: dict):
        card_id = stable_id(catalog, fields["external_card_id"])
        record = {
            **fields,
            "catalog_id": catalog,
            "name_normalized": normalize(fields["name"]),
            "anime_normalized": normalize(fields["anime"]),
            "name_aliases": [],
            "anime_aliases": [],
            "status": "active",
            "created_at": now(),
            "updated_at": now(),
        }
        await self.db.cards.update_one({"_id": card_id}, {"$setOnInsert": record}, upsert=True)
        existing = await self.db.cards.find_one({"_id": card_id})
        conflict = any(existing.get(key) != value for key, value in fields.items())
        if existing["status"] != "active":
            conflict = True
        return card_id, "conflict" if conflict else "ready"

    async def bump_generation(self):
        await self.db.settings.update_one(
            {"_id": "generation"}, {"$inc": {"value": 1}}, upsert=True
        )

    async def generation(self):
        record = await self.db.settings.find_one({"_id": "generation"})
        return record["value"] if record else 0

    async def exact(self, unique_ids: list[str]):
        refs = await self.db.telegram_files.find({"_id": {"$in": unique_ids}}).to_list()
        return await self.cards_for_assets([ref["asset_id"] for ref in refs])

    async def cards_for_assets(self, asset_ids: list[str], limit: int = 20):
        if not asset_ids:
            return []
        links = await self.db.card_assets.find(
            {"asset_id": {"$in": asset_ids}, "verified": True}
        ).to_list()
        return (
            await self.db.cards.find(
                {"_id": {"$in": list({link["card_id"] for link in links})}, "status": "active"}
            )
            .sort("_id", 1)
            .limit(limit)
            .to_list()
        )

    async def search(self, query: str, limit: int = 10, catalog: str | None = None):
        key = normalize(query)
        condition = {
            "status": "active",
            "$or": [
                {"external_card_id": query},
                {"name_normalized": key},
                {"anime_normalized": key},
                {"name_aliases": key},
                {"anime_aliases": key},
            ],
        }
        if catalog:
            condition["catalog_id"] = catalog
        return await self.db.cards.find(condition).sort("_id", 1).limit(limit).to_list()

    async def enqueue(self, kind: str, payload: dict, *, key: str | None = None, reset=False):
        key = key or str(uuid.uuid4())
        record = {
            "kind": kind,
            "payload": payload,
            "status": "pending",
            "attempts": 0,
            "next_attempt_at": now(),
            "created_at": now(),
        }
        if reset:
            await self.db.jobs.update_one(
                {"_id": key, "status": {"$nin": ["running", "pending"]}},
                {"$set": record, "$unset": {"expires_at": "", "last_error": ""}},
            )
        await self.db.jobs.update_one({"_id": key}, {"$setOnInsert": record}, upsert=True)
        return key

    async def claim_job(self, kinds: list[str], owner: str):
        return await self.db.jobs.find_one_and_update(
            {
                "kind": {"$in": kinds},
                "$or": [
                    {"status": "pending", "next_attempt_at": {"$lte": now()}},
                    {"status": "running", "lease_until": {"$lt": now()}},
                ],
            },
            {
                "$set": {
                    "status": "running",
                    "owner": owner,
                    "lease_until": now() + timedelta(seconds=120),
                },
                "$inc": {"attempts": 1},
            },
            sort=[("next_attempt_at", 1)],
            return_document=ReturnDocument.AFTER,
        )

    async def renew_job(self, job_id: str, owner: str):
        await self.db.jobs.update_one(
            {"_id": job_id, "owner": owner, "status": "running"},
            {"$set": {"lease_until": now() + timedelta(seconds=120)}},
        )

    async def finish_job(self, job_id: str, owner: str):
        await self.db.jobs.update_one(
            {"_id": job_id, "owner": owner, "status": "running"},
            {
                "$set": {
                    "status": "done",
                    "finished_at": now(),
                    "expires_at": now() + timedelta(days=7),
                },
                "$unset": {"lease_until": ""},
            },
        )

    async def fail_job(self, job: dict, owner: str, error: str):
        status = "failed" if job["attempts"] >= self.settings.job_max_attempts else "pending"
        await self.db.jobs.update_one(
            {"_id": job["_id"], "owner": owner, "status": "running"},
            {
                "$set": {
                    "status": status,
                    "last_error": error[:200],
                    "next_attempt_at": now()
                    + timedelta(seconds=min(300, 5 * 2 ** job["attempts"])),
                },
                "$unset": {"lease_until": ""},
            },
        )

    async def audit(self, actor: int, action: str, target: str, before=None):
        await self.db.audit_logs.insert_one(
            {
                "actor": actor,
                "action": action,
                "target": target,
                "before": before,
                "created_at": now(),
            }
        )

    async def acquire_instance(self, bot_id: int, owner: str):
        try:
            result = await self.db.instances.find_one_and_update(
                {"_id": bot_id, "$or": [{"lease_until": {"$lt": now()}}, {"owner": owner}]},
                {"$set": {"owner": owner, "lease_until": now() + timedelta(seconds=90)}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            return result["owner"] == owner
        except DuplicateKeyError:
            return False
