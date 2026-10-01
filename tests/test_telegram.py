import httpx
import pytest

from waifu_bot.telegram import Telegram, TelegramError


async def test_download_never_exceeds_limit():
    def handler(request):
        if request.url.path.endswith("getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "photos/test.jpg"}}
            )
        return httpx.Response(200, content=b"x" * 40)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        telegram = Telegram("synthetic", client=client)
        with pytest.raises(ValueError, match="download limit"):
            await telegram.download("file", 20)


async def test_telegram_error_preserves_rate_limit_without_token():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too many requests",
                    "parameters": {"retry_after": 12},
                },
            )
        )
    ) as client:
        telegram = Telegram("secret-token", client=client)
        with pytest.raises(TelegramError) as raised:
            await telegram.send(1, "test")
        assert raised.value.retry_after == 12
        assert "secret-token" not in str(raised.value)
