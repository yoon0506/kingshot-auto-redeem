"""Regression tests for compact, atomic JSON writes (offline, no production data)."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kingshot.repository import JsonRepository

STORAGE = {"users_file":"data/users.json","players_file":"data/players.json","codes_file":"data/codes.json","logs_file":"data/logs.jsonl"}

class JsonWriteOptimizationTests(unittest.TestCase):
    def test_unicode_and_nested_data_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "data" / "players.json"
            data = {"schemaVersion":1,"players":{"123":{"playerName":"또냥이 🍅","redeemedCodes":[{"code":"A", "message":"수령 완료"}],"failedCodes":{}}}}
            JsonRepository._save_json(path, data)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), data)
            self.assertIn("또냥이 🍅", path.read_text(encoding="utf-8"))
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_preexisting_json_remains_when_encode_fails(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "data" / "players.json"
            JsonRepository._save_json(path, {"safe":True})
            before = path.read_bytes()
            with self.assertRaises(TypeError):
                JsonRepository._save_json(path, {"not_serializable": object()})
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_500_redeems_persist_and_no_duplicate(self):
        async def exercise(root):
            repo=JsonRepository(root, STORAGE)
            uids=[str(890000000+i) for i in range(500)]
            for i in range(0, 500, 50):
                await repo.bulk_register("987654321",uids[i:i+50])
            for uid in uids:
                await repo.mark_redeemed(uid,"TEST🍅",{"message":"성공"})
            await repo.mark_redeemed(uids[0],"TEST🍅",{"message":"성공"})
            saved=json.loads((root / "data" / "players.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["players"]),500)
            self.assertTrue(all(len(v["redeemedCodes"]) == 1 for v in saved["players"].values()))
            self.assertEqual(saved["players"][uids[0]]["redeemedCodes"][0]["message"],"성공")
        with tempfile.TemporaryDirectory() as d:
            asyncio.run(exercise(Path(d)))

if __name__ == '__main__': unittest.main()
