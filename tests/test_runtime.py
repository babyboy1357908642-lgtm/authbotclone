import asyncio
import contextlib
import json

import httpx
import pytest

from waifu_bot.health import check
from waifu_bot.lookup import Lookup
from waifu_bot.runtime import Application
from waifu_bot.telegram import Telegram

pytestmark = pytest.mark.integration


async def test_poll_queue_collect_and_exact_reply(repo, settings, message, image_bytes):
    sent = []
    updates_sent = False
    updates = [
        {"update_id": 500, "channel_post": message},
        {
            "update_id": 501,
            "message": {
                "message_id": 20,
                "chat": {"id": 99, "type": "private"},
                "from": {"id": 99},
                "photo": message["photo"],
            },
        },
    ]

    async def handler(request):
        nonlocal updates_sent
        method = request.url.path.split("/")[-1]
        if method == "getWebhookInfo":
            result = {"url": ""}
        elif method == "getUpdates":
            await asyncio.sleep(0.02)
            result = [] if updates_sent else updates
            updates_sent = True
        elif method == "sendMessage":
            payload = json.loads(request.content)
            sent.append(payload)
            result = {"message_id": len(sent)}
        elif method == "getFile":
            result = {"file_path": "photos/card.png"}
        elif request.method == "GET":
            return httpx.Response(200, content=image_bytes)
        else:
            raise AssertionError(f"Unexpected Telegram method: {method}")
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        telegram = Telegram(settings.token, client=client)
        app = Application(settings, repo, telegram, Lookup(settings, repo, telegram), 123)
        task = asyncio.create_task(app.run())
        try:
            async with asyncio.timeout(8):
                while not sent:
                    if task.done():
                        await task
                    await asyncio.sleep(0.02)
            assert "CARD FOUND — Exact Telegram file" in sent[0]["text"]
            assert "Name: Yoru" in sent[0]["text"]
            assert (await repo.db.settings.find_one({"_id": "offset:123"}))["value"] == 502
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert not await repo.db.instances.find_one({"_id": 123})


async def test_webhook_rejection_releases_instance_lease(repo, settings, telegram):
    telegram.call.return_value = {"url": "https://example.invalid/webhook"}
    app = Application(settings, repo, telegram, Lookup(settings, repo, telegram), 123)
    with pytest.raises(RuntimeError, match="webhook"):
        await app.run()
    assert not await repo.db.instances.find_one({"_id": 123})


async def test_health_requires_live_instance(repo, monkeypatch):
    monkeypatch.setattr("waifu_bot.health.Settings.from_env", lambda: repo.settings)
    assert await check() == 1
    await repo.acquire_instance(123, "test")
    assert await check() == 0


async def test_whitespace_and_nonadmin_command_are_safe(repo, settings, telegram):
    app = Application(settings, repo, telegram, Lookup(settings, repo, telegram), 123)
    message = {
        "message_id": 1,
        "chat": {"id": 99, "type": "private"},
        "from": {"id": 99},
        "text": "   ",
    }
    await app.handle_update({"update_id": 1, "message": message})
    await app.handle_update({"update_id": 2, "message": {**message, "text": "/delete owo 5672"}})
    assert "authorized" in telegram.send.call_args.args[1]
