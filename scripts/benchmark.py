"""Disposable synthetic scale check. Requires TEST_MONGO_URI; never uses app DB."""

import asyncio
import json
import os
import time
import uuid

import numpy as np
from pymongo import InsertOne

from waifu_bot.config import Settings
from waifu_bot.database import Repository
from waifu_bot.images import HashIndex


async def main():
    count = int(os.getenv("BENCHMARK_CARDS", "100000"))
    settings = Settings(
        token="123:synthetic",
        admin_ids=frozenset({42}),
        mongo_uri=os.environ["TEST_MONGO_URI"],
        mongo_database=f"waifu_benchmark_{uuid.uuid4().hex}",
    )
    repo = Repository(settings)
    try:
        await repo.initialize()
        for start in range(0, count, 5000):
            indexes = range(start, min(start + 5000, count))
            await repo.db.cards.bulk_write(
                [
                    InsertOne(
                        {
                            "_id": f"card-{i}",
                            "catalog_id": "synthetic",
                            "external_card_id": str(i),
                            "name": f"Character {i}",
                            "name_normalized": f"character {i}",
                            "anime": "Synthetic",
                            "rarity": "Test",
                            "status": "active",
                        }
                    )
                    for i in indexes
                ],
                ordered=False,
            )
            await repo.db.telegram_files.bulk_write(
                [InsertOne({"_id": f"unique-{i}", "asset_id": f"asset-{i}"}) for i in indexes],
                ordered=False,
            )
            await repo.db.card_assets.bulk_write(
                [
                    InsertOne({"card_id": f"card-{i}", "asset_id": f"asset-{i}", "verified": True})
                    for i in indexes
                ],
                ordered=False,
            )
        samples = []
        for i in np.random.default_rng(42).integers(0, count, size=100):
            start = time.perf_counter()
            result = await repo.exact([f"unique-{i}"])
            samples.append((time.perf_counter() - start) * 1000)
            assert result[0]["external_card_id"] == str(i)
        plans = {}
        for collection, condition in [
            ("telegram_files", {"_id": "unique-500"}),
            ("card_assets", {"asset_id": "asset-500", "verified": True}),
            ("cards", {"_id": "card-500", "status": "active"}),
        ]:
            plan = await repo.db.command(
                "explain", {"find": collection, "filter": condition}, verbosity="executionStats"
            )
            plans[collection] = {
                "keys_examined": plan["executionStats"]["totalKeysExamined"],
                "documents_examined": plan["executionStats"]["totalDocsExamined"],
                "winning_plan": plan["queryPlanner"]["winningPlan"],
            }
            assert plan["executionStats"]["totalDocsExamined"] <= 1
        rng = np.random.default_rng(7)
        hashes = rng.integers(0, 256, size=(count, 16), dtype=np.uint8)
        index = HashIndex()
        index.replace(
            [
                {"_id": str(i), "phash": row[:8].tobytes().hex(), "dhash": row[8:].tobytes().hex()}
                for i, row in enumerate(hashes)
            ]
        )
        hash_times = []
        target = hashes[count // 2]
        for _ in range(20):
            start = time.perf_counter()
            nearest = index.nearest([(target[:8].tobytes().hex(), target[8:].tobytes().hex())])
            hash_times.append((time.perf_counter() - start) * 1000)
            assert nearest[0] == (str(count // 2), 0)
        print(
            json.dumps(
                {
                    "synthetic_cards": count,
                    "exact_lookup_samples": len(samples),
                    "exact_lookup_ms_p50": round(float(np.percentile(samples, 50)), 3),
                    "exact_lookup_ms_p95": round(float(np.percentile(samples, 95)), 3),
                    "hash_scan_ms_p50": round(float(np.percentile(hash_times, 50)), 3),
                    "hash_scan_ms_p95": round(float(np.percentile(hash_times, 95)), 3),
                    "hash_array_bytes": index.phashes.nbytes + index.dhashes.nbytes,
                    "query_plans": plans,
                    "note": "Local synthetic benchmark, excludes Telegram network and image decoding; not a production SLA.",
                },
                indent=2,
            )
        )
    finally:
        await repo.client.drop_database(settings.mongo_database)
        await repo.close()


if __name__ == "__main__":
    asyncio.run(main())
