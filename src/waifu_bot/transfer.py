import csv
import io
import json
import re

from pymongo import UpdateOne

from .database import now, stable_id
from .parser import REQUIRED, normalize, parse_value

MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_EXPORT_PART_BYTES = 4 * 1024 * 1024


def text(value, name: str, maximum=500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a nonempty string up to {maximum} characters.")
    return value.strip()


def validate_record(raw: dict) -> dict:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("Each JSONL record must have schema_version=1.")
    card = raw.get("card")
    if not isinstance(card, dict):
        raise ValueError("Missing card object.")
    clean = {key: text(card.get(key), key) for key in REQUIRED}
    clean["catalog_id"] = text(card.get("catalog_id"), "catalog_id", 100)
    value = card.get("value")
    clean["value"] = parse_value(str(value)) if value is not None and value != "" else None
    if card.get("theme"):
        clean["theme"] = text(card["theme"], "theme")
    for field in ("name_aliases", "anime_aliases"):
        aliases = card.get(field, [])
        if not isinstance(aliases, list) or len(aliases) > 30:
            raise ValueError(f"{field} must be a list of at most 30 names.")
        clean[field] = [normalize(text(alias, field, 150)) for alias in aliases]
    assets = raw.get("assets", [])
    if not isinstance(assets, list) or len(assets) > 1000:
        raise ValueError("assets must be a list of at most 1000 records per card.")
    validated_assets = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("Invalid asset.")
        aid = text(asset.get("asset_id"), "asset_id", 64)
        if not re.fullmatch(r"[0-9a-f]{64}", aid):
            raise ValueError("Invalid asset_id.")
        files = asset.get("telegram_files", [])
        if not isinstance(files, list) or len(files) > 50:
            raise ValueError("Invalid telegram_files list.")
        clean_files = []
        for file in files:
            if not isinstance(file, dict):
                raise ValueError("Invalid Telegram file.")
            uid = text(file.get("file_unique_id"), "file_unique_id", 256)
            bot_files = file.get("bot_files", {})
            if not isinstance(bot_files, dict) or len(bot_files) > 20:
                raise ValueError("Invalid bot_files.")
            refs = {}
            for bot_id, file_id in bot_files.items():
                if not re.fullmatch(r"[0-9]{1,20}", bot_id):
                    raise ValueError("bot_files keys must be numeric bot IDs.")
                refs[bot_id] = text(file_id, "file_id", 512)
            clean_files.append({"file_unique_id": uid, "bot_files": refs})
        validated_assets.append({"asset_id": aid, "telegram_files": clean_files})
    return {"schema_version": 1, "card": clean, "assets": validated_assets}


def read_import(data: bytes, filename: str) -> list[dict]:
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError("Import limit is 5 MiB. Split larger imports into separate files.")
    content = data.decode("utf-8-sig")
    if filename.lower().endswith(".csv"):
        rows = [{"schema_version": 1, "card": row} for row in csv.DictReader(io.StringIO(content))]
    elif filename.lower().endswith((".jsonl", ".ndjson")):
        rows = [json.loads(line) for line in content.splitlines() if line.strip()]
    else:
        raise ValueError("Use a UTF-8 .jsonl, .ndjson or metadata .csv file.")
    if not rows or len(rows) > 10_000:
        raise ValueError("Import must contain between 1 and 10,000 records.")
    records, keys = [], set()
    for line, raw in enumerate(rows, 1):
        try:
            record = validate_record(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Record {line}: {exc}") from exc
        identity = (record["card"]["catalog_id"], record["card"]["external_card_id"])
        if identity in keys:
            raise ValueError(f"Record {line}: duplicate catalog/card ID within import.")
        keys.add(identity)
        records.append(record)
    return records


async def stage_import(repo, records: list[dict], import_id: str):
    for start in range(0, len(records), 200):
        await repo.db.import_rows.bulk_write(
            [
                UpdateOne(
                    {"_id": f"{import_id}:{index}"},
                    {
                        "$setOnInsert": {
                            "import_id": import_id,
                            "position": index,
                            "record": record,
                        }
                    },
                    upsert=True,
                )
                for index, record in enumerate(records[start : start + 200], start)
            ],
            ordered=False,
        )
    await repo.db.import_rows.create_index([("import_id", 1), ("position", 1)], unique=True)


async def import_record(repo, record: dict, bot_id: int):
    card = record["card"]
    fields = {key: card[key] for key in (*REQUIRED, "value")}
    if card.get("theme"):
        fields["theme"] = card["theme"]
    card_id, status = await repo.upsert_card(card["catalog_id"], fields)
    if status == "conflict":
        return status
    await repo.db.cards.update_one(
        {"_id": card_id},
        {
            "$addToSet": {
                "name_aliases": {"$each": card["name_aliases"]},
                "anime_aliases": {"$each": card["anime_aliases"]},
            }
        },
    )
    for asset in record["assets"]:
        aid = asset["asset_id"]
        await repo.db.assets.update_one(
            {"_id": aid},
            {
                "$setOnInsert": {
                    "index_status": "pending",
                    "created_at": now(),
                    "updated_at": now(),
                }
            },
            upsert=True,
        )
        mapped_ids = {aid}
        for file in asset["telegram_files"]:
            update = {"$setOnInsert": {"asset_id": aid}}
            if file["bot_files"]:
                update["$set"] = {
                    f"bot_files.{key}": value for key, value in file["bot_files"].items()
                }
            await repo.db.telegram_files.update_one(
                {"_id": file["file_unique_id"]}, update, upsert=True
            )
            existing = await repo.db.telegram_files.find_one({"_id": file["file_unique_id"]})
            mapped_ids.add(existing["asset_id"])
        await repo.db.card_assets.bulk_write(
            [
                UpdateOne(
                    {"card_id": card_id, "asset_id": mapped},
                    {"$set": {"verified": True, "manual": True}},
                    upsert=True,
                )
                for mapped in mapped_ids
            ],
            ordered=False,
        )
        for mapped in mapped_ids:
            await repo.enqueue("index", {"asset_id": mapped}, key=f"index:{mapped}", reset=True)
    await repo.bump_generation()
    return "ready"


async def export_parts(repo, catalog: str | None = None):
    condition = {"status": "active"}
    if catalog:
        condition["catalog_id"] = catalog
    buffer = bytearray()
    async for card in repo.db.cards.find(condition).sort("_id", 1):
        clean = {
            key: card.get(key)
            for key in (*REQUIRED, "catalog_id", "value", "theme", "name_aliases", "anime_aliases")
        }
        assets = []
        async for link in repo.db.card_assets.find({"card_id": card["_id"], "verified": True}):
            files = []
            async for file in repo.db.telegram_files.find({"asset_id": link["asset_id"]}):
                files.append(
                    {"file_unique_id": file["_id"], "bot_files": file.get("bot_files", {})}
                )
            assets.append({"asset_id": link["asset_id"], "telegram_files": files})
        record = {"schema_version": 1, "card": clean, "assets": assets}
        # Every exported record must be accepted by the importer.
        validate_record(record)
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode()
        if len(line) > MAX_EXPORT_PART_BYTES:
            raise ValueError(
                "One card is too large for portable export; use mongodump for a full backup."
            )
        if len(buffer) + len(line) > MAX_EXPORT_PART_BYTES:
            yield bytes(buffer)
            buffer.clear()
        buffer.extend(line)
    if buffer:
        yield bytes(buffer)


def card_key(catalog: str, external_id: str):
    return stable_id(catalog, external_id)
