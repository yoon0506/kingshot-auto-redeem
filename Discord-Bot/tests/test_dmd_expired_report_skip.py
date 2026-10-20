"""Offline regression tests for final expired report exclusions."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import KingshotBot


class FakeStore:
    def __init__(self, recent, messages):
        self.recent = recent
        self.messages = messages

    def recent_codes(self):
        return self.recent

    def load_messages(self):
        return self.messages

    def source_snapshot(self):
        return ('snapshot',)


class ExpiredReportSkipTests(unittest.TestCase):
    def run_loop(self, recent, messages):
        seen = []

        async def ready():
            pass

        async def publish(code):
            seen.append(code)
            return f'Report posted/updated for {code}.'

        obj = SimpleNamespace(
            wait_until_ready=ready,
            report_store=FakeStore(recent, messages),
            report_channel_id=123,
            _code_safety_snapshot=lambda: None,
            _report_last_snapshot=None,
            publish_redeem_report=publish,
        )
        asyncio.run(KingshotBot.report_loop.coro(obj))
        return seen

    def test_final_expired_report_is_not_rechecked_even_if_recent(self):
        final = {'message_id': 100, 'expiry_signature': 'expired', 'frozen_stats': {'total': 20}}
        self.assertEqual(self.run_loop(['EXPIRED', 'ACTIVE'], {'123:EXPIRED': final}), ['ACTIVE'])

    def test_api_expired_but_discord_not_updated_is_rechecked(self):
        old = {'message_id': 100, 'expiry_signature': 'unknown', 'frozen_stats': {'total': 20}}
        self.assertEqual(self.run_loop([], {'123:EXPIRED': old}), ['EXPIRED'])

    def test_missing_frozen_stats_is_rechecked(self):
        old = {'message_id': 100, 'expiry_signature': 'expired'}
        self.assertEqual(self.run_loop([], {'123:EXPIRED': old}), ['EXPIRED'])

    def test_nonexpired_posted_report_continues_refresh(self):
        old = {'message_id': 100, 'expiry_signature': 'active'}
        self.assertEqual(self.run_loop([], {'123:ACTIVE': old}), ['ACTIVE'])

    def test_other_channel_does_not_appear(self):
        self.assertEqual(self.run_loop([], {'456:OTHER': {'message_id': 100}}), [])


if __name__ == '__main__':
    unittest.main()
