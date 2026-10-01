import asyncio
import copy
import io
import json
from datetime import timedelta

import pytest
from PIL import Image

from waifu_bot.admin import Admin
from waifu_bot.database import now
from waifu_bot.lookup import Lookup
from waifu_bot.runtime import Application, JobStopped
from waifu_bot.transfer import export_parts, import_record, read_import, stage_import

pytestmark = pytest.mark.integration


async def test_collector_idempotence_and_all_photo_sizes(repo, message):
    first = await repo.collect(message, 123)
    second = await repo.collect(message, 123)
    assert first["status"] == second["status"] == "ready"
    assert await repo.db.cards.count_documents({}) == 1
    assert await repo.db.assets.count_documents({}) == 1
    assert await repo.db.telegram_files.count_documents({}) == 2
    assert await repo.db.source_messages.count_documents({}) == 1
    assert (await repo.exact(["small-unique"]))[0]["name"] == "Yoru"
    stored = await repo.db.source_messages.find_one({})
    assert stored["caption_raw"] == message["caption"]


async def test_disallowed_and_disabled_channel(repo, message):
    stranger = {**message, "chat": {"id": -999, "type": "channel"}}
    assert (await repo.collect(stranger, 123))["status"] == "ignored"
    await repo.db.settings.update_one({"_id": "collector"}, {"$set": {"enabled": False}})
    assert (await repo.collect(message, 123))["status"] == "ignored"
    assert await repo.db.cards.count_documents({}) == 0


async def test_catalog_isolation_and_shared_image_ambiguity(repo, message, settings, telegram):
    await repo.collect(message, 123)
    await repo.collect({**message, "message_id": 11}, 123, catalog="other")
    lookup = Lookup(settings, repo, telegram)
    result = await lookup.query(message["photo"])
    assert result.kind == "ambiguous" and len(result.cards) == 2
    telegram.download.assert_not_awaited()


async def test_exact_never_downloads_and_cache_invalidates(repo, message, settings, telegram):
    collected = await repo.collect(message, 123)
    lookup = Lookup(settings, repo, telegram)
    result = await lookup.query(message["photo"])
    assert result.kind == "exact"
    telegram.download.assert_not_awaited()
    await repo.db.cards.update_one(
        {"_id": collected["card_id"]}, {"$set": {"name": "Updated Yoru"}}
    )
    await repo.bump_generation()
    assert (await lookup.query(message["photo"])).cards[0]["name"] == "Updated Yoru"
    telegram.download.assert_not_awaited()


async def test_metadata_conflict_does_not_poison_card(repo, message):
    await repo.collect(message, 123)
    changed = copy.deepcopy(message)
    changed["message_id"] = 11
    changed["caption"] = changed["caption"].replace("Yoru", "Wrong Name")
    changed["photo"] = [{"file_id": "new", "file_unique_id": "new", "width": 600, "height": 800}]
    assert (await repo.collect(changed, 123))["status"] == "conflict"
    assert (await repo.db.cards.find_one({}))["name"] == "Yoru"
    assert not await repo.exact(["new"])


async def test_editing_source_identity_removes_stale_link(repo, message):
    await repo.collect(message, 123)
    edited = {**message, "caption": message["caption"].replace("5672: Yoru", "9999: Asa Mitaka")}
    await repo.collect(edited, 123)
    cards = await repo.exact(["large-unique"])
    assert [card["name"] for card in cards] == ["Asa Mitaka"]


async def test_other_source_keeps_valid_old_link(repo, message):
    await repo.collect(message, 123)
    await repo.collect({**message, "message_id": 11}, 123)
    await repo.collect(
        {**message, "caption": message["caption"].replace("5672: Yoru", "99: Asa")}, 123
    )
    assert len(await repo.exact(["large-unique"])) == 2


async def test_reference_index_and_resized_lookup(repo, message, settings, telegram, image_bytes):
    await repo.collect(message, 123)
    asset = await repo.db.assets.find_one({})
    lookup = Lookup(settings, repo, telegram)
    app = Application(settings, repo, telegram, lookup, 123)
    await app.index_asset({"asset_id": asset["_id"]})
    image = Image.open(io.BytesIO(image_bytes)).resize((300, 400))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=70)
    telegram.download.return_value = output.getvalue()
    result = await lookup.query(
        [{"file_id": "resized", "file_unique_id": "resized", "width": 300, "height": 400}]
    )
    assert result.kind == "similar"
    assert result.cards[0]["name"] == "Yoru"
    assert await repo.db.telegram_files.count_documents({"_id": "resized"}) == 0


async def test_parallel_identical_lookup_shares_download(repo, message, settings, telegram):
    async def download(*args):
        await asyncio.sleep(0.03)
        return b"invalid"

    telegram.download.side_effect = download
    lookup = Lookup(settings, repo, telegram)
    results = await asyncio.gather(
        lookup.query(message["photo"]), lookup.query(message["photo"]), return_exceptions=True
    )
    assert all(isinstance(item, OSError) for item in results)
    assert telegram.download.await_count == 1
    assert not lookup.flights


async def test_import_export_roundtrip_and_duplicate_protection(repo, message):
    await repo.collect(message, 123)
    exported = b"".join([part async for part in export_parts(repo)])
    records = read_import(exported, "cards.jsonl")
    assert records[0]["card"]["theme"] == "American Native"
    await repo.db.cards.delete_many({})
    await repo.db.card_assets.delete_many({})
    for _ in range(2):
        assert await import_record(repo, records[0], 123) == "ready"
    assert await repo.db.cards.count_documents({}) == 1
    assert len(await repo.exact(["large-unique"])) == 1


async def test_job_claim_lease_and_pause_are_respected(repo):
    key = await repo.enqueue("broadcast", {"text": "x"}, key="broadcast:test")
    first, second = await asyncio.gather(
        repo.claim_job(["broadcast"], "a"), repo.claim_job(["broadcast"], "b")
    )
    assert (first is None) != (second is None)
    job = first or second
    await repo.db.jobs.update_one({"_id": key}, {"$set": {"status": "paused"}})
    await repo.finish_job(key, job["owner"])
    assert (await repo.db.jobs.find_one({"_id": key}))["status"] == "paused"
    await repo.db.jobs.update_one(
        {"_id": key}, {"$set": {"status": "running", "lease_until": now() - timedelta(seconds=1)}}
    )
    assert (await repo.claim_job(["broadcast"], "new"))["owner"] == "new"


async def test_instance_lease_excludes_second_process(repo):
    assert await repo.acquire_instance(123, "one")
    assert not await repo.acquire_instance(123, "two")
    assert await repo.acquire_instance(123, "one")


async def test_broadcast_preview_preserves_apostrophes(repo, settings, telegram):
    admin = Admin(settings, repo, telegram, Lookup(settings, repo, telegram), 123)
    await admin.handle(
        {
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "/broadcast Don't miss today's cards!",
        }
    )
    pending = next(iter(admin.pending.values()))
    assert pending["payload"]["text"] == "Don't miss today's cards!"


async def test_admin_callback_rechecks_actor_and_delete_invalidates(
    repo, message, settings, telegram
):
    collected = await repo.collect(message, 123)
    lookup = Lookup(settings, repo, telegram)
    admin = Admin(settings, repo, telegram, lookup, 123)
    private = {
        "from": {"id": 42},
        "chat": {"id": 42, "type": "private"},
        "text": "/delete owo 5672",
    }
    await admin.handle(private)
    token = next(iter(admin.pending))
    query = {"id": "callback", "data": f"confirm:{token}", "from": {"id": 99}, "message": private}
    await admin.callback(query)
    assert (await repo.db.cards.find_one({"_id": collected["card_id"]}))["status"] == "active"
    await admin.callback({**query, "from": {"id": 42}})
    assert (await repo.db.cards.find_one({"_id": collected["card_id"]}))["status"] == "deleted"
    assert not await repo.exact(["large-unique"])


async def test_import_job_resumes_after_checkpoint(repo, message, settings, telegram):
    await repo.collect(message, 123)
    records = read_import(b"".join([p async for p in export_parts(repo)]), "x.jsonl")
    second = copy.deepcopy(records[0])
    second["card"]["external_card_id"] = "999"
    await stage_import(repo, records + [second], "batch")
    jid = await repo.enqueue(
        "import", {"import_id": "batch", "position": 0, "chat_id": 42, "ready": 1, "conflicts": 0}
    )
    job = await repo.claim_job(["import"], "worker")
    app = Application(settings, repo, telegram, Lookup(settings, repo, telegram), 123)
    await app.execute(job, "worker")
    finished = await repo.db.jobs.find_one({"_id": jid})
    assert finished["payload"]["position"] == 1
    assert finished["payload"]["ready"] == 2
    assert await repo.db.cards.count_documents({}) == 2


async def test_broadcast_only_opted_in_and_paused_stops(repo, settings, telegram):
    await repo.db.users.insert_many(
        [{"_id": 1, "subscribed": True}, {"_id": 2, "subscribed": False}]
    )
    jid = await repo.enqueue(
        "broadcast", {"text": "hello", "chat_id": 42, "last_user": 0, "sent": 0, "failed": 0}
    )
    job = await repo.claim_job(["broadcast"], "worker")
    app = Application(settings, repo, telegram, Lookup(settings, repo, telegram), 123)
    await app.broadcast(job, "worker")
    telegram.broadcast_one.assert_awaited_once_with(1, "hello")
    await repo.db.jobs.update_one({"_id": jid}, {"$set": {"status": "paused"}})
    with pytest.raises(JobStopped):
        await app.check_active(job, "worker")


def test_import_rejects_malformed_and_duplicate_records():
    raw = {
        "schema_version": 1,
        "card": {
            "catalog_id": "owo",
            "external_card_id": "1",
            "name": "Yoru",
            "anime": "Chainsaw man",
            "rarity": "Legendary",
        },
    }
    with pytest.raises(ValueError, match="duplicate"):
        read_import((json.dumps(raw) + "\n" + json.dumps(raw)).encode(), "x.jsonl")
    raw["assets"] = [
        {
            "asset_id": "a" * 64,
            "telegram_files": [{"file_unique_id": "u", "bot_files": {"$bad.key": "x"}}],
        }
    ]
    with pytest.raises(ValueError, match="numeric"):
        read_import(json.dumps(raw).encode(), "x.jsonl")
