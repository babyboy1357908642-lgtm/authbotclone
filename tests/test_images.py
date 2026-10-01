import io

import pytest
from PIL import Image

from waifu_bot.images import HashIndex, fingerprint, open_image, variants


def test_resize_and_compression_remain_close(image_bytes):
    reference = fingerprint(image_bytes, 1_000_000)
    image = Image.open(io.BytesIO(image_bytes)).resize((300, 400))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=65)
    index = HashIndex()
    index.replace(
        [
            {"_id": "card-a", "phash": reference.phash, "dhash": reference.dhash},
            {"_id": "other", "phash": "0000000000000000", "dhash": "ffffffffffffffff"},
        ]
    )
    results = index.nearest(variants(output.getvalue(), 1_000_000))
    assert results[0][0] == "card-a"
    assert results[0][1] <= 8


def test_uniform_border_crop(image_bytes):
    reference = fingerprint(image_bytes, 1_000_000)
    image = Image.open(io.BytesIO(image_bytes))
    framed = Image.new("RGB", (800, 1000), "black")
    framed.paste(image, (100, 100))
    output = io.BytesIO()
    framed.save(output, "PNG")
    options = variants(output.getvalue(), 1_000_000)
    assert (reference.phash, reference.dhash) in options


def test_reject_pixel_bombs_and_invalid_bytes(image_bytes):
    with pytest.raises(ValueError, match="pixel limit"):
        open_image(image_bytes, 10)
    with pytest.raises(OSError):
        open_image(b"not an image", 1_000_000)
