"""500-user offline queue stress tests for DMD Gift Bot.

Uses existing test_dmd_safety.py fakes and actual kingshot.redeemer.AutoRedeemer.
No network, Discord login, or production data read/write.
Run inside Discord-Bot/: python -m unittest discover -s tests -p 'test_dmd_*.py' -v
"""
import asyncio
import copy
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kingshot.redeemer import AutoRedeemer
from test_dmd_safety import FakeRepository, FakeApi, CODE, response

N = 500


class MultiCodeApi(FakeApi):
    def __init__(self, scripted, codes):
        super().__init__(scripted)
        self.codes = codes

    async def fetch_active_gift_codes(self):
        return list(self.codes)

    async def redeem_gift_code(self, pid, code, *, kingdom):
        self.calls.append((pid, code, kingdom))
        outcomes = self.scripted.get((pid, code), ["success"])
        kind = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        return response(kind)


class Queue500Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dmd-queue-500-")
        self.addCleanup(self.temp.cleanup)
        self.ids = [str(900100000 + idx) for idx in range(N)]
        self.repo = FakeRepository(self.temp.name, self.ids)
        self.api = FakeApi({})
        self.config = dict(
            min_delay_seconds=10, max_delay_seconds=20,
            failure_cooldown_seconds=180, rate_limit_cooldown_seconds=600,
            timeout_retry_cooldown_seconds=600,
            timeout_retry_max_cooldown_seconds=3600,
            same_cycle_timeout_retry_seconds=60,
            code_error_stop_threshold=3, code_error_pause_minutes=60,
            max_rate_limit_retries_per_cycle=3,
        )
        self.worker = AutoRedeemer(self.repo, self.api, self.config)
        self.worker._delay_between_jobs = AsyncMock()
        self.worker._same_player_pause = AsyncMock()
        self.sleeper = patch("kingshot.redeemer.asyncio.sleep", new_callable=AsyncMock)
        self.mock_sleep = self.sleeper.start()
        self.addCleanup(self.sleeper.stop)

    async def test_500_mixed_success_timeout_and_uid_error(self):
        """50 transient timeouts go to queue tail, 20 invalid UIDs quarantined, 430 direct success."""
        timeout_ids = set(self.ids[:50])
        invalid_ids = set(self.ids[50:70])
        self.api.scripted = {
            **{pid: ["timeout", "success"] for pid in timeout_ids},
            **{pid: ["uid"] for pid in invalid_ids},
        }
        results = await self.worker.run_once()
        self.assertEqual(results["success"], N - len(invalid_ids))
        self.assertEqual(results["failed"], len(invalid_ids))
        self.assertEqual(len(self.api.calls), N + len(timeout_ids))
        counts = Counter(pid for pid, _, _ in self.api.calls)
        self.assertTrue(all(counts[pid] == 2 for pid in timeout_ids))
        self.assertTrue(all(counts[pid] == 1 for pid in set(self.ids) - timeout_ids))
        # All primary attempts must be completed before any deferred attempt.
        self.assertEqual(len({pid for pid, _, _ in self.api.calls[:N]}), N)
        self.assertEqual(set(pid for pid, _, _ in self.api.calls[N:]), timeout_ids)
        self.assertTrue(all(not self.repo.players[pid]["autoRedeem"] for pid in invalid_ids))
        self.assertEqual(sum(bool(p["redeemedCodes"]) for p in self.repo.players.values()), N - len(invalid_ids))
        self.assertEqual(sum(p["uidReviewRequired"] for p in self.repo.players.values() if "uidReviewRequired" in p), len(invalid_ids))
        # No completed or quarantined account should be called again next cycle.
        previous = len(self.api.calls)
        await self.worker.run_once()
        self.assertEqual(len(self.api.calls), previous)

    async def test_500_all_timeout_bounded_to_two_attempts(self):
        """Worst-case timeout storm makes at most 1 primary + 1 deferred request per UID."""
        self.api.scripted = {pid: ["timeout"] for pid in self.ids}
        results = await self.worker.run_once()
        self.assertEqual(results["failed"], N)
        self.assertEqual(results["success"], 0)
        self.assertEqual(len(self.api.calls), 2 * N)
        counts = Counter(pid for pid, _, _ in self.api.calls)
        self.assertTrue(all(counts[pid] == 2 for pid in self.ids))
        self.assertEqual(len({pid for pid, _, _ in self.api.calls[:N]}), N)
        self.assertEqual(len({pid for pid, _, _ in self.api.calls[N:]}), N)
        # Persistent timeout backoff should suppress immediate re-run.
        count_before = len(self.api.calls)
        await self.worker.run_once()
        self.assertEqual(len(self.api.calls), count_before)

    async def test_500_repeated_code_errors_suspend_only_bad_code(self):
        """Three identical application errors stop BAD code, not the separate GOOD code."""
        bad_code, good_code = "BAD500", "GOOD500"
        self.repo.manual_codes = [bad_code, good_code]
        self.api = MultiCodeApi({(pid, bad_code): ["problem"] for pid in self.ids}, [bad_code, good_code])
        self.worker.api = self.api
        result = await self.worker.run_once()
        by_code = Counter(code for _, code, _ in self.api.calls)
        self.assertEqual(by_code[bad_code], 3)
        self.assertEqual(by_code[good_code], N)
        self.assertEqual(result["success"], N)
        self.assertEqual(result["failed"], 3)
        self.assertEqual(self.worker._load_safety_states()[bad_code]["status"], "cooldown")
        # Subsequent automatic cycle must not re-hit the bad code.
        self.api.calls.clear()
        await self.worker.run_once()
        self.assertEqual(len(self.api.calls), 0)

    async def test_500_safety_cooldown_probe_block_and_restart(self):
        self.api.scripted = {pid: ["problem"] for pid in self.ids}
        await self.worker.run_once()
        self.assertEqual(len(self.api.calls), 3)
        states = self.worker._load_safety_states()
        states[CODE]["until"] = "2000-01-01T00:00:00+00:00"
        self.worker._save_safety_states(states)
        self.api.calls.clear()
        await self.worker.run_once()
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.worker._load_safety_states()[CODE]["status"], "blocked")
        other = AutoRedeemer(self.repo, self.api, self.config)
        other._delay_between_jobs = AsyncMock()
        other._same_player_pause = AsyncMock()
        before = len(self.api.calls)
        await other.run_once()
        self.assertEqual(len(self.api.calls), before)
        self.assertTrue(Path(self.temp.name, "data", "dmd_code_safety.json").exists())

    async def test_500_429_does_not_spin_forever(self):
        """A permanently rate-limited UID must not cause unlimited retries."""
        pid = self.ids[0]
        self.api.scripted = {pid: ["rate_limited"]}
        result = await self.worker.run_once()
        # Other 499 should be completed; 429 retries bounded by configured limit.
        self.assertEqual(result["success"], N - 1)
        self.assertLessEqual(sum(p == pid for p, _, _ in self.api.calls), 3)
        self.assertEqual(sum(bool(p["redeemedCodes"]) for p in self.repo.players.values()), N - 1)
        self.assertTrue(self.mock_sleep.await_count >= 1, "Rate limit must retain a cooldown")


if __name__ == "__main__":
    unittest.main()
