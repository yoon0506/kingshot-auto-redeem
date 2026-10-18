"""DMD offline regression tests; NO network, real UIDs, or production data access.

Run from Discord-Bot/: python -m unittest discover -s tests -p 'test_dmd_safety.py' -v
"""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kingshot.redeemer import AutoRedeemer

CODE = "TESTCODE26"


def response(kind):
    base = {"success": False, "already": False, "rate_limited": False,
            "expired": False, "max_uses_reached": False, "raw": {}}
    if kind == "success":
        return {**base, "success": True, "message": "SUCCESS"}
    if kind == "already":
        return {**base, "already": True, "message": "RECEIVED"}
    if kind == "timeout":
        return {**base, "message": "TIMEOUT RETRY"}
    if kind == "problem":
        return {**base, "message": "RECHARGE_MONEY ERROR"}
    if kind == "uid":
        return {**base, "message": "Wrong kingdom or invalid UID for K1850 (USER INFO ERROR)",
                "raw": {"err_code": "40020"}}
    if kind == "rate_limited":
        return {**base, "rate_limited": True, "message": "HTTP 429 rate limited"}
    raise ValueError(kind)


class FakeRepository:
    def __init__(self, root, ids):
        self.codes_path = Path(root) / "data" / "codes.json"
        self.players = {pid: {"playerId": pid, "playerName": "Test", "kingdom": 1850,
                              "autoRedeem": True, "withdrawn": False,
                              "redeemedCodes": [], "failedCodes": {}} for pid in ids}
        self.events = []
        self.manual_codes = [CODE]

    async def list_manual_codes(self):
        return self.manual_codes

    async def update_code_cache(self, codes):
        return []

    async def list_unredeemable_codes(self):
        return []

    async def list_auto_redeem_players(self):
        return copy.deepcopy([p for p in self.players.values() if p["autoRedeem"] and not p["withdrawn"]])

    async def get_player(self, pid):
        return copy.deepcopy(self.players.get(pid))

    async def list_all_players(self):
        return copy.deepcopy(list(self.players.values()))

    async def mark_redeemed(self, pid, code, result):
        p = self.players[pid]
        p["redeemedCodes"].append({"code": code})
        p["failedCodes"].pop(code, None)

    async def mark_failed(self, pid, code, message):
        from datetime import datetime, timezone
        entry = self.players[pid]["failedCodes"].get(code, {})
        self.players[pid]["failedCodes"][code] = {
            "message": message, "attempts": entry.get("attempts", 0) + 1,
            "lastFailedAt": datetime.now(timezone.utc).isoformat()}

    async def log_event(self, name, **kwargs):
        self.events.append((name, kwargs))

    async def quarantine_invalid_uid(self, pid, reason):
        self.players[pid]["autoRedeem"] = False
        self.players[pid]["uidReviewRequired"] = True
        return "changed"

    async def mark_code_expired(self, code, message):
        pass

    async def mark_code_max_uses_reached(self, code, message):
        pass


class FakeApi:
    def __init__(self, scripted):
        self.scripted = scripted
        self.calls = []

    async def fetch_active_gift_codes(self):
        return [CODE]

    async def redeem_gift_code(self, pid, code, *, kingdom):
        self.calls.append((pid, code, kingdom))
        results = self.scripted.get(pid, ["success"])
        next_result = results.pop(0) if len(results) > 1 else results[0]
        return response(next_result)


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dmd-offline-")
        self.addCleanup(self.temp.cleanup)
        self.ids = [str(900000000 + i) for i in range(5)]
        self.repo = FakeRepository(self.temp.name, self.ids)
        self.api = FakeApi({})
        self.config = dict(min_delay_seconds=10, max_delay_seconds=20,
                           failure_cooldown_seconds=180, rate_limit_cooldown_seconds=600,
                           timeout_retry_cooldown_seconds=600,
                           timeout_retry_max_cooldown_seconds=3600,
                           same_cycle_timeout_retry_seconds=60,
                           code_error_stop_threshold=3, code_error_pause_minutes=60)
        self.worker = AutoRedeemer(self.repo, self.api, self.config)
        # Bypass waits in tests only; production code/config untouched.
        self.worker._delay_between_jobs = AsyncMock()
        self.worker._same_player_pause = AsyncMock()

    async def test_success_marks_all_without_double_attempt(self):
        results = await self.worker.run_once()
        self.assertEqual(results['success'], 5)
        self.assertEqual(len(self.api.calls), 5)
        self.assertTrue(all(p['redeemedCodes'] for p in self.repo.players.values()))

    async def test_three_identical_errors_pause_only_one_code(self):
        self.api.scripted = {pid: ['problem'] for pid in self.ids}
        results = await self.worker.run_once()
        self.assertEqual(len(self.api.calls), 3)
        self.assertEqual(results['failed'], 3)
        safety = self.worker._load_safety_states()[CODE]
        self.assertEqual(safety['status'], 'cooldown')
        before = len(self.api.calls)
        await self.worker.run_once()
        self.assertEqual(len(self.api.calls), before, 'cooldown must suppress requests')

    async def test_probe_failure_permanently_blocks_across_restart(self):
        self.api.scripted = {pid: ['problem'] for pid in self.ids}
        await self.worker.run_once()
        states = self.worker._load_safety_states()
        states[CODE]['until'] = '2000-01-01T00:00:00+00:00'
        self.worker._save_safety_states(states)
        self.api.calls.clear()
        await self.worker.run_once()
        self.assertEqual(len(self.api.calls), 1, 'expired cooldown must probe ONE uid')
        self.assertEqual(self.worker._load_safety_states()[CODE]['status'], 'blocked')
        new_worker = AutoRedeemer(self.repo, self.api, self.config)
        new_worker._delay_between_jobs = AsyncMock()
        before = len(self.api.calls)
        await new_worker.run_once()
        self.assertEqual(len(self.api.calls), before, 'block must survive new worker instance')

    async def test_manual_probe_unlocks_on_success(self):
        self.api.scripted = {pid: ['problem'] for pid in self.ids}
        await self.worker.run_once()
        states = self.worker._load_safety_states()
        states[CODE] = {"status": "blocked", "reason": "RECHARGE_MONEY ERROR"}
        self.worker._save_safety_states(states)
        self.assertTrue(await self.worker.manual_retry_code(CODE))
        self.api.scripted = {pid: ['success'] for pid in self.ids}
        await self.worker.run_once()
        self.assertNotIn(CODE, self.worker._load_safety_states())

    async def test_uid_error_quarantines_without_affecting_others(self):
        self.api.scripted = {self.ids[0]: ['uid']}
        await self.worker.run_once()
        self.assertFalse(self.repo.players[self.ids[0]]['autoRedeem'])
        self.assertTrue(self.repo.players[self.ids[0]]['uidReviewRequired'])
        self.assertEqual(sum(bool(p['redeemedCodes']) for p in self.repo.players.values()), 4)

    async def test_timeout_is_retried_at_end_once(self):
        self.api.scripted = {self.ids[0]: ['timeout', 'success']}
        # Avoid real 60-second wait while preserving actual retry mechanism.
        with patch('kingshot.redeemer.asyncio.sleep', new_callable=AsyncMock):
            results = await self.worker.run_once()
        calls = [p for p, _, _ in self.api.calls if p == self.ids[0]]
        self.assertEqual(len(calls), 2)
        self.assertEqual(results['failed'], 0)
        self.assertEqual(results['success'], 5)

    async def test_repeated_timeout_does_not_loop_forever(self):
        self.api.scripted = {self.ids[0]: ['timeout']}
        with patch('kingshot.redeemer.asyncio.sleep', new_callable=AsyncMock):
            results = await self.worker.run_once()
        self.assertEqual(sum(p == self.ids[0] for p, _, _ in self.api.calls), 2)
        self.assertEqual(results['failed'], 1)

    async def test_existing_user_data_is_never_loaded(self):
        self.assertTrue(str(self.worker._code_safety_file).startswith(self.temp.name))
        self.assertTrue(all(int(uid) >= 900000000 for uid in self.repo.players))


if __name__ == '__main__':
    unittest.main()
