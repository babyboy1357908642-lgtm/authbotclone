import asyncio
import io

import httpx


class TelegramError(Exception):
    def __init__(self, code: int, description: str, retry_after: int = 0):
        super().__init__(f"Telegram {code}: {description}")
        self.code = code
        self.retry_after = retry_after


class Telegram:
    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.files_base = f"https://api.telegram.org/file/bot{token}"
        self.client = client or httpx.AsyncClient(
            timeout=45, limits=httpx.Limits(max_connections=20)
        )

    async def close(self):
        await self.client.aclose()

    async def call(self, method: str, **data):
        response = await self.client.post(f"{self.base}/{method}", json=data)
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(
                body.get("error_code", response.status_code),
                body.get("description", "API error"),
                body.get("parameters", {}).get("retry_after", 0),
            )
        return body["result"]

    async def send(self, chat_id: int, text: str, **kwargs):
        # Plain text: untrusted captions never become HTML/Markdown markup.
        return await self.call("sendMessage", chat_id=chat_id, text=text[:4096], **kwargs)

    async def download(self, file_id: str, max_bytes: int) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > max_bytes:
            raise ValueError("File exceeds the configured download limit.")
        path = info.get("file_path", "")
        if not path or path.startswith(("/", "http")) or ".." in path.split("/"):
            raise ValueError("Invalid Telegram file path.")
        data = bytearray()
        async with self.client.stream("GET", f"{self.files_base}/{path}") as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > max_bytes:
                    raise ValueError("File exceeds the configured download limit.")
                data.extend(chunk)
        return bytes(data)

    async def document(self, chat_id: int, filename: str, data: bytes):
        response = await self.client.post(
            f"{self.base}/sendDocument",
            data={"chat_id": str(chat_id)},
            files={"document": (filename, io.BytesIO(data), "application/x-ndjson")},
        )
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(
                body.get("error_code", 500), body.get("description", "Upload failed")
            )
        return body["result"]

    async def broadcast_one(self, user_id: int, text: str):
        try:
            return await self.send(user_id, text)
        except TelegramError as exc:
            if exc.code != 429:
                raise
            # Return control to the worker on long rate limits; its checkpoint survives restart.
            if exc.retry_after > 30:
                raise
            await asyncio.sleep(max(exc.retry_after, 1))
            return await self.send(user_id, text)


def photo_files(message: dict) -> list[dict]:
    photos = message.get("photo", [])
    if photos:
        return photos
    document = message.get("document", {})
    if document.get("mime_type") in {"image/jpeg", "image/png", "image/webp"}:
        return [document]
    return []
