"""Regression tests for refresh of older already-published coupon reports."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import KingshotBot


class FakeReportStore:
    def __init__(self, recent, messages):
        self.recent = recent
        self.messages = messages

    def recent_codes(self):
        return self.recent

    def load_messages(self):
        return self.messages

    def source_snapshot(self):
        return ('unchanged-for-test',)


class ReportAutoRefreshTests(unittest.TestCase):
    def run_loop(self, recent, messages):
        processed = []

        async def ready():
            pass

        async def publish(code):
            processed.append(code)
            return f'Report posted/updated for {code}.'

        dummy = SimpleNamespace(
            wait_until_ready=ready,
            report_store=FakeReportStore(recent, messages),
            report_channel_id=123,
            _code_safety_snapshot=lambda: None,
            _report_last_snapshot=None,
            publish_redeem_report=publish,
        )
        asyncio.run(KingshotBot.report_loop.coro(dummy))
        return processed

    def test_old_posted_report_is_refreshed(self):
        self.assertEqual(self.run_loop([], {'123:IG200KS': {'message_id': 456}}), ['IG200KS'])

    def test_recent_and_old_posted_both_refreshed_without_duplicates(self):
        self.assertEqual(self.run_loop(['NEW', 'IG200KS'], {'123:IG200KS': {'message_id': 456}}), ['NEW', 'IG200KS'])

    def test_ignore_unposted_and_other_channel_old_codes(self):
        self.assertEqual(self.run_loop([], {
            'other:OLD': {'message_id': 17},
            '123:UNPOSTED': {},
        }), [])


if __name__ == '__main__':
    unittest.main()
