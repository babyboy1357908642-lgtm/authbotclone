import asyncio
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from .images import HashIndex, variants


@dataclass
class Result:
    kind: str
    cards: list[dict] = field(default_factory=list)
    note: str = ""
    distances: dict[str, int] = field(default_factory=dict)


class Lookup:
    def __init__(self, settings, repository, telegram):
        self.settings, self.repo, self.telegram = settings, repository, telegram
        self.cache = OrderedDict()
        self.generation = -1
        self.hash_generation = -1
        self.index = HashIndex()
        self.refresh_lock = asyncio.Lock()
        self.flight_lock = asyncio.Lock()
        self.flights: dict[str, asyncio.Task] = {}

    async def clear(self):
        self.cache.clear()
        self.generation = -1
        self.hash_generation = -1

    async def query(self, files: list[dict]) -> Result:
        generation = await self.repo.generation()
        if generation != self.generation:
            self.cache.clear()
            self.generation = generation
        key = tuple(sorted(item["file_unique_id"] for item in files))
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            self.cache.move_to_end(key)
            return cached[1]
        exact = await self.repo.exact(list(key))
        if exact:
            result = Result("exact" if len(exact) == 1 else "ambiguous", exact)
            self.cache[key] = (time.monotonic() + self.settings.cache_ttl_seconds, result)
            self.cache.move_to_end(key)
            while len(self.cache) > self.settings.cache_max_entries:
                self.cache.popitem(last=False)
            return result
        largest = max(
            files, key=lambda f: (f.get("width", 0) * f.get("height", 0), f.get("file_size", 0))
        )
        unique_id = largest["file_unique_id"]
        async with self.flight_lock:
            task = self.flights.get(unique_id)
            if task is None:
                task = asyncio.create_task(self._fallback(largest, generation))
                self.flights[unique_id] = task
        try:
            return await task
        finally:
            async with self.flight_lock:
                if self.flights.get(unique_id) is task:
                    self.flights.pop(unique_id, None)

    async def _fallback(self, file: dict, generation: int):
        data = await self.telegram.download(
            file["file_id"], self.settings.max_download_mb * 1024 * 1024
        )
        byte_matches = await self.repo.db.assets.find(
            {"sha256": hashlib.sha256(data).hexdigest()}
        ).to_list()
        cards = await self.repo.cards_for_assets([record["_id"] for record in byte_matches])
        if cards:
            return Result("exact_bytes" if len(cards) == 1 else "ambiguous", cards)
        queries = await asyncio.to_thread(variants, data, self.settings.max_image_pixels)
        async with self.refresh_lock:
            if self.hash_generation != generation:
                records = await self.repo.db.assets.find(
                    {"index_status": "ready", "hash_version": 1},
                    {"phash": 1, "dhash": 1},
                ).to_list()
                replacement = HashIndex()
                await asyncio.to_thread(replacement.replace, records)
                self.index = replacement
                self.hash_generation = generation
        ranked = await asyncio.to_thread(self.index.nearest, queries, 50)
        distance_map, by_id = {}, {}
        # Candidate fetch is one join for the bounded nearest set, not a DB query per card/hash.
        distances_by_asset = dict(ranked)
        links = await self.repo.db.card_assets.find(
            {
                "asset_id": {"$in": list(distances_by_asset)},
                "verified": True,
            }
        ).to_list()
        for link in links:
            cid = link["card_id"]
            distance_map[cid] = min(distance_map.get(cid, 65), distances_by_asset[link["asset_id"]])
        async for card in self.repo.db.cards.find(
            {"_id": {"$in": list(distance_map)}, "status": "active"}
        ):
            by_id[card["_id"]] = card
        ordered = sorted(by_id, key=lambda cid: (distance_map[cid], cid))
        if ordered and distance_map[ordered[0]] <= self.settings.hash_distance:
            best = distance_map[ordered[0]]
            gap = distance_map[ordered[1]] - best if len(ordered) > 1 else 65
            if gap >= self.settings.hash_margin:
                return Result("similar", [by_id[ordered[0]]], distances={ordered[0]: best})
            near = [
                by_id[cid]
                for cid in ordered
                if distance_map[cid] < best + self.settings.hash_margin
            ]
            return Result(
                "ambiguous", near[:10], "Several card editions have similar images.", distance_map
            )
        return Result("not_found", note="Send the original image or crop tightly around the card.")


def render(result: Result) -> str:
    headings = {
        "exact": "🎴 CARD FOUND — Exact Telegram file",
        "exact_bytes": "🎴 CARD FOUND — Exact image bytes",
        "similar": "🎴 SIMILAR CARD FOUND",
        "ambiguous": "🔎 MULTIPLE POSSIBLE CARDS",
        "possible": "🔎 MATCHING CARDS",
        "not_found": "❌ CARD NOT FOUND",
    }
    lines = [headings[result.kind]]
    for card in result.cards[:10]:
        lines += [
            "",
            f"Name: {card['name']}",
            f"Anime: {card['anime']}",
            f"Rarity: {card['rarity']}",
            f"Card ID: {card['external_card_id']}",
            f"Catalog: {card['catalog_id']}",
        ]
        if card.get("value") is not None:
            lines.append(f"Value: {card['value']:,}")
        if card.get("theme"):
            lines.append(f"Theme: {card['theme']}")
        if card["_id"] in result.distances:
            lines.append(f"Hash distance: {result.distances[card['_id']]} / 64 (lower is closer)")
    if result.note:
        lines += ["", result.note]
    if result.kind == "similar":
        lines += ["", "Image similarity is a suggestion; please verify the edition."]
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3900] + "\n… Refine with /find <name or card ID>."
