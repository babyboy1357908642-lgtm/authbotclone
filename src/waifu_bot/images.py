import hashlib
import io
import warnings
from dataclasses import dataclass

import imagehash
import numpy as np
from PIL import Image, ImageChops, ImageOps


@dataclass(frozen=True)
class Fingerprint:
    sha256: str
    phash: str
    dhash: str
    width: int
    height: int


def open_image(data: bytes, max_pixels: int) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Only JPEG, PNG and WebP images are supported.")
            if source.width * source.height > max_pixels:
                raise ValueError("Decoded image exceeds the pixel limit.")
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGB")
    return image


def fingerprint(data: bytes, max_pixels: int) -> Fingerprint:
    image = open_image(data, max_pixels)
    return Fingerprint(
        hashlib.sha256(data).hexdigest(),
        str(imagehash.phash(image)),
        str(imagehash.dhash(image)),
        image.width,
        image.height,
    )


def variants(data: bytes, max_pixels: int) -> list[tuple[str, str]]:
    image = open_image(data, max_pixels)
    result = [(str(imagehash.phash(image)), str(imagehash.dhash(image)))]
    # Only remove a uniform outside frame. Telegram UI/collages need user cropping.
    background = Image.new("RGB", image.size, image.getpixel((0, 0)))
    diff = (
        ImageChops.difference(image, background).convert("L").point(lambda p: 255 if p > 18 else 0)
    )
    box = diff.getbbox()
    if box and box != (0, 0, image.width, image.height):
        crop = image.crop(box)
        if crop.width * crop.height >= image.width * image.height * 0.35:
            result.append((str(imagehash.phash(crop)), str(imagehash.dhash(crop))))
    return result


POPCOUNT = np.array([i.bit_count() for i in range(256)], dtype=np.uint8)


class HashIndex:
    """Compact in-memory 64-bit hash arrays. No MongoDB full scan per query."""

    def __init__(self):
        self.ids: list[str] = []
        self.phashes = np.empty((0, 8), dtype=np.uint8)
        self.dhashes = np.empty((0, 8), dtype=np.uint8)

    def replace(self, records: list[dict]):
        ids, phashes, dhashes = [], [], []
        for record in records:
            try:
                p, d = bytes.fromhex(record["phash"]), bytes.fromhex(record["dhash"])
                if len(p) != 8 or len(d) != 8:
                    continue
            except (ValueError, KeyError):
                continue
            ids.append(record["_id"])
            phashes.append(list(p))
            dhashes.append(list(d))
        self.ids = ids
        self.phashes = np.array(phashes, dtype=np.uint8).reshape(-1, 8)
        self.dhashes = np.array(dhashes, dtype=np.uint8).reshape(-1, 8)

    def nearest(self, queries: list[tuple[str, str]], limit=30) -> list[tuple[str, int]]:
        if not self.ids:
            return []
        best = np.full(len(self.ids), 65, dtype=np.int16)
        for phash, dhash in queries:
            p = np.frombuffer(bytes.fromhex(phash), dtype=np.uint8)
            d = np.frombuffer(bytes.fromhex(dhash), dtype=np.uint8)
            pd = POPCOUNT[np.bitwise_xor(self.phashes, p)].sum(axis=1)
            dd = POPCOUNT[np.bitwise_xor(self.dhashes, d)].sum(axis=1)
            # Require both fingerprints to agree, rather than averaging away a bad match.
            best = np.minimum(best, np.maximum(pd, dd))
        count = min(limit, len(best))
        selected = np.argpartition(best, count - 1)[:count]
        selected = selected[np.argsort(best[selected], kind="stable")]
        return [(self.ids[i], int(best[i])) for i in selected]
