import asyncio
import json
import secrets
import shlex
import time

from .database import now
from .lookup import Result, render
from .parser import REQUIRED, normalize, parse_value
from .transfer import MAX_IMPORT_BYTES, card_key, export_parts, read_import, stage_import

HELP = """Admin commands (private chat only):
/stats
/find <exact name, anime or card ID>
/add catalog=<catalog> id=<id> name="Name" anime="Anime" rarity="Rarity" value=25000
  Reply to a photo. Without fields, its caption is parsed.
/edit <catalog> <card-id> name="..." anime="..." rarity="..." value=123
  Also: name_aliases="alias one|alias two" anime_aliases="..."
/delete <catalog> <card-id> — asks for confirmation
/sources — list; /sources add <channel-id> <catalog>
/sources on|off|remove <channel-id>
/sources aliases <channel-id> {"character name":"name"}
/collector on|off [channel-id]
/review — recent parse failures/conflicts
/resolve <channel-id> <message-id> — link reviewed data to an existing matching card
/reindex all|<catalog> <card-id>
/import — reply to a JSONL/CSV document; validates before confirmation
/export [catalog] — portable JSONL catalog, split into parts
/broadcast <text> — subscribed users only; asks for confirmation
/jobs — recent jobs; /jobs retry|cancel|pause|resume <job-id>
Use /subscribe or /unsubscribe to manage broadcast consent.
"""


def fields_from_args(args: list[str]) -> dict:
    result = {}
    mapping = {"id": "external_card_id", "catalog": "catalog_id"}
    for arg in args:
        if "=" not in arg:
            raise ValueError('Use key=value; quote multiword values, e.g. name="Mikasa Ackerman".')
        key, value = arg.split("=", 1)
        key = mapping.get(key, key)
        if key not in {*REQUIRED, "catalog_id", "value", "name_aliases", "anime_aliases"}:
            raise ValueError(f"Unknown field: {key}")
        if not value.strip() or len(value) > 500:
            raise ValueError("Values must be nonempty and at most 500 characters.")
        if key == "value":
            result[key] = parse_value(value)
        elif key.endswith("_aliases"):
            result[key] = [normalize(v) for v in value.split("|") if v.strip()]
        else:
            result[key] = value.strip()
    return result


class Admin:
    def __init__(self, settings, repo, telegram, lookup, bot_id):
        self.settings, self.repo, self.tg, self.lookup, self.bot_id = (
            settings,
            repo,
            telegram,
            lookup,
            bot_id,
        )
        self.pending = {}

    def authorized(self, message):
        return (
            message.get("from", {}).get("id") in self.settings.admin_ids
            and message.get("chat", {}).get("type") == "private"
        )

    async def confirm(self, message, action: str, payload: dict, preview: str):
        self.pending = {
            key: item for key, item in self.pending.items() if item["expires"] > time.monotonic()
        }
        if len(self.pending) >= 20:
            raise ValueError("Too many pending confirmations. Wait five minutes or cancel one.")
        token = secrets.token_urlsafe(12)
        self.pending[token] = {
            "actor": message["from"]["id"],
            "chat_id": message["chat"]["id"],
            "expires": time.monotonic() + 300,
            "action": action,
            "payload": payload,
        }
        await self.tg.send(
            message["chat"]["id"],
            preview + "\nConfirm within 5 minutes.",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Confirm", "callback_data": f"confirm:{token}"},
                        {"text": "Cancel", "callback_data": f"cancel:{token}"},
                    ]
                ],
            },
        )

    async def callback(self, query):
        data = query.get("data", "")
        actor = query.get("from", {}).get("id")
        message = query.get("message", {})
        mode, _, token = data.partition(":")
        item = self.pending.get(token)
        allowed = (
            actor in self.settings.admin_ids
            and item
            and item["actor"] == actor
            and item["chat_id"] == message.get("chat", {}).get("id")
            and item["expires"] > time.monotonic()
            and mode in {"confirm", "cancel"}
        )
        if not allowed:
            await self.tg.call(
                "answerCallbackQuery",
                callback_query_id=query["id"],
                text="Unauthorized or expired.",
            )
            return
        self.pending.pop(token)
        await self.tg.call(
            "answerCallbackQuery",
            callback_query_id=query["id"],
            text="Cancelled." if mode == "cancel" else "Confirmed.",
        )
        if mode == "cancel":
            return
        payload, action = item["payload"], item["action"]
        if action == "delete":
            before = await self.repo.db.cards.find_one({"_id": payload["card_id"]})
            if before:
                await self.repo.audit(actor, "delete", payload["card_id"], before)
                await self.repo.db.cards.update_one(
                    {"_id": payload["card_id"]},
                    {"$set": {"status": "deleted", "updated_at": now()}},
                )
                await self.repo.bump_generation()
                await self.lookup.clear()
            response = "Card soft-deleted. Shared images and source records were retained."
        elif action == "import":
            await stage_import(self.repo, payload["records"], token)
            job_id = await self.repo.enqueue(
                "import",
                {
                    "import_id": token,
                    "position": -1,
                    "chat_id": item["chat_id"],
                    "actor": actor,
                    "ready": 0,
                    "conflicts": 0,
                },
                key=f"import:{token}",
            )
            await self.repo.audit(actor, "import", job_id)
            response = f"Import queued: {job_id}"
        elif action == "broadcast":
            job_id = await self.repo.enqueue(
                "broadcast",
                {
                    "text": payload["text"],
                    "last_user": 0,
                    "chat_id": item["chat_id"],
                    "sent": 0,
                    "failed": 0,
                },
                key=f"broadcast:{token}",
            )
            await self.repo.audit(actor, "broadcast", job_id)
            response = f"Broadcast queued: {job_id}"
        else:
            raise ValueError("Unknown action.")
        await self.tg.send(item["chat_id"], response)

    async def handle(self, message):
        if not self.authorized(message):
            await self.tg.send(
                message["chat"]["id"],
                "Admin commands require an authorized account in private chat.",
            )
            return
        raw = message.get("text", "")
        command, _, tail = raw.partition(" ")
        command = command.split("@")[0]
        args = [] if command in {"/broadcast", "/find"} else shlex.split(tail)
        chat_id, actor = message["chat"]["id"], message["from"]["id"]
        if command in {"/admin", "/help"}:
            await self.tg.send(chat_id, HELP)
        elif command == "/stats":
            lines = []
            for label, collection, condition in [
                ("Active cards", "cards", {"status": "active"}),
                ("Image assets", "assets", {}),
                ("Indexed assets", "assets", {"index_status": "ready"}),
                ("Source messages", "source_messages", {}),
                ("Needs review", "source_messages", {"parse_status": {"$ne": "ready"}}),
                ("Pending jobs", "jobs", {"status": "pending"}),
                ("Failed jobs", "jobs", {"status": "failed"}),
                ("Subscribers", "users", {"subscribed": True}),
            ]:
                lines.append(
                    f"{label}: {await self.repo.db[collection].count_documents(condition)}"
                )
            counters = await self.repo.db.settings.find_one({"_id": "lookup_stats"}) or {}
            lines.append(
                "Lookups: " + json.dumps({k: v for k, v in counters.items() if k != "_id"})
            )
            await self.tg.send(chat_id, "\n".join(lines))
        elif command == "/find":
            if not tail.strip():
                raise ValueError("Usage: /find <exact name, anime or card ID>")
            cards = await self.repo.search(tail.strip())
            await self.tg.send(chat_id, render(Result("possible" if cards else "not_found", cards)))
        elif command == "/add":
            reply = message.get("reply_to_message")
            if not reply:
                raise ValueError("Reply to a card photo with /add and optional key=value metadata.")
            fields = fields_from_args(args)
            catalog = fields.pop("catalog_id", None)
            copied = dict(reply)
            if fields:
                labels = {
                    "external_card_id": "Card ID",
                    "name": "Name",
                    "anime": "Anime",
                    "rarity": "Rarity",
                    "value": "Value",
                }
                if any(key.endswith("_aliases") for key in fields):
                    raise ValueError("Set aliases later using /edit.")
                copied["caption"] = "\n".join(
                    f"{labels[key]}: {value}" for key, value in fields.items()
                )
            result = await self.repo.collect(copied, self.bot_id, catalog=catalog, force=True)
            await self.repo.audit(actor, "add", result.get("card_id") or "unparsed")
            await self.tg.send(
                chat_id,
                f"Collector result: {result['status']}\n" + "\n".join(result.get("issues", [])),
            )
        elif command == "/edit":
            if len(args) < 3:
                raise ValueError('/edit <catalog> <card-id> name="..." rarity="..."')
            card_id = card_key(args[0], args[1])
            before = await self.repo.db.cards.find_one({"_id": card_id, "status": "active"})
            if not before:
                raise ValueError("Active card not found.")
            fields = fields_from_args(args[2:])
            if "external_card_id" in fields or "catalog_id" in fields:
                raise ValueError(
                    "Card identity cannot be edited. Add the corrected card and delete the old one."
                )
            for field in ("name", "anime"):
                if field in fields:
                    fields[f"{field}_normalized"] = normalize(fields[field])
            fields["updated_at"] = now()
            await self.repo.audit(actor, "edit", card_id, before)
            await self.repo.db.cards.update_one({"_id": card_id}, {"$set": fields})
            await self.repo.bump_generation()
            await self.lookup.clear()
            await self.tg.send(chat_id, "Card updated.")
        elif command == "/delete":
            if len(args) != 2:
                raise ValueError("/delete <catalog> <card-id>")
            card = await self.repo.db.cards.find_one({"_id": card_key(*args), "status": "active"})
            if not card:
                raise ValueError("Active card not found.")
            await self.confirm(
                message,
                "delete",
                {"card_id": card["_id"]},
                f"Delete {card['catalog_id']}/{card['external_card_id']}: {card['name']}?",
            )
        elif command == "/sources":
            await self.sources(message, args, tail)
        elif command == "/collector":
            if len(args) not in {1, 2} or args[0] not in {"on", "off"}:
                raise ValueError("/collector on|off [channel-id]")
            if len(args) == 2:
                result = await self.repo.db.source_channels.update_one(
                    {"_id": int(args[1])}, {"$set": {"enabled": args[0] == "on"}}
                )
                if not result.matched_count:
                    raise ValueError("Add the channel with /sources add first.")
            else:
                await self.repo.db.settings.update_one(
                    {"_id": "collector"}, {"$set": {"enabled": args[0] == "on"}}
                )
            await self.repo.audit(actor, "collector", " ".join(args))
            await self.tg.send(chat_id, "Collector setting saved.")
        elif command == "/review":
            records = (
                await self.repo.db.source_messages.find({"parse_status": {"$ne": "ready"}})
                .sort("updated_at", -1)
                .limit(10)
                .to_list()
            )
            lines = [
                f"{r['channel_id']} / {r['message_id']} — {r['parse_status']}\n{r['caption_raw'][:220]}\n{', '.join(r['issues'])}"
                for r in records
            ]
            await self.tg.send(chat_id, "\n\n".join(lines) or "Review queue is empty.")
        elif command == "/resolve":
            if len(args) != 2:
                raise ValueError("/resolve <channel-id> <message-id>")
            source = await self.repo.db.source_messages.find_one(
                {"channel_id": int(args[0]), "message_id": int(args[1])}
            )
            if not source or not source.get("card_id"):
                raise ValueError(
                    "Source has no parsed card. Re-add its photo with corrected metadata using /add."
                )
            card = await self.repo.db.cards.find_one({"_id": source["card_id"], "status": "active"})
            if not card:
                raise ValueError("No active card. Add or correct the card first.")
            await self.repo.audit(actor, "resolve", str(source["_id"]))
            for aid in source.get("asset_ids", [source["asset_id"]]):
                await self.repo.db.card_assets.update_one(
                    {"card_id": card["_id"], "asset_id": aid},
                    {
                        "$set": {"verified": True},
                        "$addToSet": {"sources": f"{source['channel_id']}:{source['message_id']}"},
                    },
                    upsert=True,
                )
            await self.repo.db.source_messages.update_one(
                {"_id": source["_id"]},
                {"$set": {"parse_status": "ready", "issues": [], "resolved_by": actor}},
            )
            await self.repo.bump_generation()
            await self.tg.send(chat_id, "Source image approved for the existing card metadata.")
        elif command == "/reindex":
            if args == ["all"]:
                payload = {"all": True, "chat_id": chat_id}
            elif len(args) == 2:
                payload = {"card_id": card_key(*args), "chat_id": chat_id}
            else:
                raise ValueError("/reindex all OR /reindex <catalog> <card-id>")
            jid = await self.repo.enqueue("reindex", payload)
            await self.repo.audit(actor, "reindex", jid)
            await self.tg.send(chat_id, f"Reindex scheduling job: {jid}")
        elif command == "/import":
            document = message.get("reply_to_message", {}).get("document")
            if not document:
                raise ValueError("Reply to a JSONL or CSV document with /import.")
            data = await self.tg.download(document["file_id"], MAX_IMPORT_BYTES)
            records = await asyncio.to_thread(read_import, data, document.get("file_name", ""))
            await self.confirm(
                message,
                "import",
                {"records": records},
                f"Validated {len(records)} records. Import merges identical cards and skips metadata conflicts. Import?",
            )
        elif command == "/export":
            if len(args) > 1:
                raise ValueError("/export [catalog]")
            jid = await self.repo.enqueue(
                "export", {"catalog": args[0] if args else None, "chat_id": chat_id}
            )
            await self.tg.send(chat_id, f"Export queued: {jid}")
        elif command == "/broadcast":
            if not tail.strip() or len(tail) > 3500:
                raise ValueError("/broadcast <text up to 3500 characters>")
            count = await self.repo.db.users.count_documents({"subscribed": True})
            await self.confirm(
                message,
                "broadcast",
                {"text": tail.strip()},
                f"Send to {count} subscribers?\n\n{tail.strip()}",
            )
        elif command == "/jobs":
            await self.jobs(message, args)
        else:
            await self.tg.send(chat_id, "Unknown command. Use /admin for commands.")

    async def sources(self, message, args, tail):
        chat_id = message["chat"]["id"]
        if not args:
            records = await self.repo.db.source_channels.find().limit(100).to_list()
            await self.tg.send(
                chat_id,
                "\n".join(
                    f"{r['_id']} catalog={r['catalog_id']} enabled={r['enabled']}" for r in records
                )
                or "No sources. /sources add <channel-id> <catalog>",
            )
            return
        operation = args[0]
        if operation == "add" and len(args) == 3:
            channel_id = int(args[1])
            if channel_id >= 0 or not 1 <= len(args[2]) <= 100:
                raise ValueError(
                    "Use a negative channel ID and a catalog name up to 100 characters."
                )
            await self.repo.db.source_channels.update_one(
                {"_id": channel_id},
                {
                    "$set": {"enabled": True, "catalog_id": args[2]},
                    "$setOnInsert": {"aliases": {}},
                },
                upsert=True,
            )
        elif operation in {"on", "off", "remove"} and len(args) == 2:
            if operation == "remove":
                await self.repo.db.source_channels.delete_one({"_id": int(args[1])})
            else:
                result = await self.repo.db.source_channels.update_one(
                    {"_id": int(args[1])}, {"$set": {"enabled": operation == "on"}}
                )
                if not result.matched_count:
                    raise ValueError("Unknown source.")
        elif operation == "aliases" and len(args) >= 3:
            raw_json = tail.split(maxsplit=2)[2]
            aliases = json.loads(raw_json)
            if (
                not isinstance(aliases, dict)
                or len(aliases) > 30
                or any(
                    not isinstance(k, str) or len(k) > 80 or v not in {*REQUIRED, "value"}
                    for k, v in aliases.items()
                )
            ):
                raise ValueError(
                    "Invalid alias map. Values: name, anime, rarity, value, external_card_id."
                )
            result = await self.repo.db.source_channels.update_one(
                {"_id": int(args[1])}, {"$set": {"aliases": aliases}}
            )
            if not result.matched_count:
                raise ValueError("Unknown source.")
        else:
            raise ValueError("See /admin for /sources syntax.")
        await self.repo.audit(message["from"]["id"], "sources", " ".join(args[:3]))
        await self.tg.send(chat_id, "Source settings saved.")

    async def jobs(self, message, args):
        if not args:
            jobs = (
                await self.repo.db.jobs.find({"kind": {"$ne": "update"}})
                .sort("created_at", -1)
                .limit(15)
                .to_list()
            )
            await self.tg.send(
                message["chat"]["id"],
                "\n".join(
                    f"{j['_id']} | {j['kind']} | {j['status']} | attempts={j['attempts']}"
                    for j in jobs
                )
                or "No jobs.",
            )
            return
        if len(args) != 2 or args[0] not in {"retry", "cancel", "pause", "resume"}:
            raise ValueError("/jobs retry|cancel|pause|resume <job-id>")
        action, jid = args
        current = await self.repo.db.jobs.find_one({"_id": jid})
        if not current:
            raise ValueError("Job not found.")
        if action == "retry" and current["status"] != "failed":
            raise ValueError("Only failed jobs can be retried.")
        if action == "resume" and current["status"] != "paused":
            raise ValueError("Only paused jobs can be resumed.")
        if action in {"pause", "cancel"} and current["kind"] not in {"broadcast", "import"}:
            raise ValueError("Pause/cancel is available for import and broadcast jobs.")
        fields = {
            "status": {
                "retry": "pending",
                "resume": "pending",
                "pause": "paused",
                "cancel": "cancelled",
            }[action]
        }
        if action in {"retry", "resume"}:
            fields.update(attempts=0, next_attempt_at=now())
        await self.repo.db.jobs.update_one(
            {"_id": jid}, {"$set": fields, "$unset": {"expires_at": ""}}
        )
        await self.repo.audit(message["from"]["id"], f"job_{action}", jid)
        await self.tg.send(
            message["chat"]["id"], f"Job {action} saved. An in-flight send may still finish."
        )

    async def export_job(self, payload):
        count = 0
        async for part in export_parts(self.repo, payload.get("catalog")):
            count += 1
            await self.tg.document(payload["chat_id"], f"waifu-cards-{count:04d}.jsonl", part)
        await self.tg.send(
            payload["chat_id"],
            f"Export finished: {count} part(s). Image binaries are not included.",
        )
