"""Offline tests for DMD-before-other-alliance scheduling."""
import unittest
from unittest.mock import patch

from kingshot.redeemer import AutoRedeemer


class StubRepository:
    def __init__(self, players):
        self.players = players
        from pathlib import Path
        self.codes_path = Path('/tmp/test-dmd-priority-unused-codes.json')

    async def list_auto_redeem_players(self):
        return self.players


def player(uid, alliance_id=0, redeemed=()):
    result = {'playerId': str(uid), 'redeemedCodes': [{'code': code} for code in redeemed], 'failedCodes': {}}
    if alliance_id is not None:
        result['allianceId'] = alliance_id
    return result


class DmdPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_dmd_selected_ahead_of_all_other_alliances(self):
        people = [player(1, 1), player(2, 2), player(3, 3), player(4, 0), player(5, 0)]
        redeemer = AutoRedeemer(StubRepository(people), None, {})
        attempted = set()
        order = []
        last = None
        with patch('kingshot.redeemer.random.shuffle', lambda x: None):
            for _ in range(len(people)):
                job, _, _, _ = await redeemer._next_redeem_job(attempted, ['HELLO'], last)
                self.assertIsNotNone(job)
                uid = str(job.player['playerId'])
                order.append(uid)
                attempted.add((uid, job.code))
                last = uid
        self.assertEqual(set(order[:2]), {'4', '5'})
        self.assertEqual(set(order[2:]), {'1', '2', '3'})

    async def test_dmd_priority_even_if_only_dmd_was_previous_player(self):
        redeemer = AutoRedeemer(StubRepository([player(1, 0), player(2, 1)]), None, {})
        job, repeats, _, _ = await redeemer._next_redeem_job(set(), ['HELLO'], '1')
        self.assertEqual(job.player['playerId'], '1')
        self.assertTrue(repeats)

    async def test_missing_alliance_defaults_to_dmd(self):
        redeemer = AutoRedeemer(StubRepository([player(1, 1), player(2, None)]), None, {})
        job, _, _, _ = await redeemer._next_redeem_job(set(), ['HELLO'], None)
        self.assertEqual(job.player['playerId'], '2')

    async def test_unredeemed_other_alliances_continue_after_dmd_done(self):
        redeemer = AutoRedeemer(StubRepository([player(1, 0, redeemed=['HELLO']), player(2, 1)]), None, {})
        job, _, _, _ = await redeemer._next_redeem_job(set(), ['HELLO'], None)
        self.assertEqual(job.player['playerId'], '2')


if __name__ == '__main__':
    unittest.main()
