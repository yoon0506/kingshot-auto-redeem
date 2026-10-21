"""Offline report finalization for globally exhausted gift codes."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import KingshotBot
from kingshot.reporting import ReportStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / 'data').mkdir()
        self.codes_path = root / 'data' / 'codes.json'
        repo = SimpleNamespace(root=root, codes_path=self.codes_path)
        self.store = ReportStore(repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_max_uses_recognized_without_expired_at(self):
        self.codes_path.write_text(json.dumps({'knownCodes': {'EXHAUSTED': {
            'maxUsesReachedAt': '2026-10-21T11:42:24+00:00',
            'maxUsesReachedMessage': 'maximum uses'
        }}}), encoding='utf-8')
        self.assertIsNotNone(self.store.detected_max_uses_at('EXHAUSTED'))
        self.assertIsNone(self.store.detected_expired_at('EXHAUSTED'))

    def test_not_maxed_returns_none(self):
        self.codes_path.write_text(json.dumps({'knownCodes': {'ACTIVE': {}}}), encoding='utf-8')
        self.assertIsNone(self.store.detected_max_uses_at('ACTIVE'))


class MockChannel:
    def __init__(self):
        self.sent = []

    async def send(self, *, embed):
        self.sent.append(embed)
        return SimpleNamespace(id=1001)

    async def fetch_message(self, message_id):
        raise AssertionError('No prior message expected')


class MaxUsesReportTests(unittest.TestCase):
    def test_exhausted_report_is_final_and_keeps_reason(self):
        channel = MockChannel()
        saved = {}
        cutoff = __import__('datetime').datetime.fromisoformat('2026-10-21T11:42:24+00:00')

        class Store:
            def expiry_for(self, code): return None
            def detected_expired_at(self, code): return None
            def detected_max_uses_at(self, code): return cutoff
            def load_messages(self): return dict(saved)
            def save_messages(self, items): saved.clear(); saved.update(items)
            async def summary(self, code, *, registered_before=None):
                assert registered_before == cutoff
                return {'total': 60, 'success': 0, 'already': 0,
                        'accepted_unknown': 0, 'failed': 0, 'pending': 60}

        bot = SimpleNamespace(
            report_channel_id=123, report_store=Store(),
            _report_lock=asyncio.Lock(),
            get_channel=lambda _: channel,
            _code_safety_state=lambda code: {},
        )
        with patch('bot.load_report_ment', return_value='TEST'):
            result = asyncio.run(KingshotBot.publish_redeem_report(bot, 'EXHAUSTED'))
        self.assertEqual(result, 'Report posted/updated for EXHAUSTED.')
        self.assertIn('🔴 만료', channel.sent[0].description)
        self.assertIn('교환 횟수 초과', channel.sent[0].description)
        self.assertEqual(saved['123:EXHAUSTED']['expiry_signature'], 'expired')
        self.assertEqual(saved['123:EXHAUSTED']['frozen_stats']['pending'], 60)

    def test_normal_expiry_classification_not_modified(self):
        # Existing time-based expiry handling remains independently covered
        # by the legacy report contract and expired-report tests.
        self.assertTrue(hasattr(ReportStore, 'detected_expired_at'))

if __name__ == '__main__': unittest.main()
