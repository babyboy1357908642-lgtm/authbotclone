import io
import os
import uuid
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from PIL import Image, ImageDraw

from waifu_bot.config import Settings
from waifu_bot.database import Repository

OWO = "OwO! Check out this character! Chainsaw man 5672: Yoru [🪶] (🟡 𝙍𝘼𝙍𝙄𝙏𝙔: Legendary) 🪶𝑨𝒎𝒆𝒓𝒊𝒄𝒂𝒏 𝑵𝒂𝒕𝒊𝒗𝒆🪶"


@pytest.fixture
def settings():
    return Settings(
        token="123:synthetic-test-token",
        admin_ids=frozenset({42}),
        channel_ids=frozenset({-100123}),
        default_catalog="owo",
    )


@pytest.fixture
def message():
    return {
        "chat": {"id": -100123, "type": "channel"},
        "message_id": 10,
        "date": 1700000000,
        "caption": OWO,
        "photo": [
            {
                "file_id": "small-file",
                "file_unique_id": "small-unique",
                "width": 100,
                "height": 100,
            },
            {
                "file_id": "large-file",
                "file_unique_id": "large-unique",
                "width": 600,
                "height": 800,
            },
        ],
    }


@pytest.fixture
def image_bytes():
    image = Image.new("RGB", (600, 800), "#bccedb")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 50, 450, 630), fill="#664455")
    draw.ellipse((110, 80, 420, 390), fill="#edc28b")
    draw.polygon([(270, 410), (60, 750), (500, 660)], fill="#248fbd")
    draw.text((120, 680), "SYNTHETIC CARD 5672", fill="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def telegram(image_bytes):
    tg = AsyncMock()
    tg.download.return_value = image_bytes
    return tg


@pytest_asyncio.fixture
async def repo(settings):
    uri = os.getenv("TEST_MONGO_URI")
    if not uri:
        pytest.skip("Set TEST_MONGO_URI for real MongoDB integration tests.")
    config = replace(settings, mongo_uri=uri, mongo_database=f"waifu_test_{uuid.uuid4().hex}")
    repository = Repository(config)
    await repository.initialize()
    try:
        yield repository
    finally:
        await repository.client.drop_database(config.mongo_database)
        await repository.close()
