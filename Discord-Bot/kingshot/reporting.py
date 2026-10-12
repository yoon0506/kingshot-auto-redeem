"""Read-only, aggregate redemption reporting for DMD.

No player IDs or credentials are posted in channel messages.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any


def _utc(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (TypeError, ValueError, AttributeError):
        return None


class ReportStore:
    def __init__(self, repository):
        self.repo = repository
        self.message_path = repository.root / 'data' / 'dmd_report_messages.json'

    def recent_codes(self, *, hours: int = 48) -> list[str]:
        try:
            contents = json.loads(self.repo.codes_path.read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError, OSError):
            return []
        now = datetime.now(timezone.utc)
        recent = []
        for code, meta in contents.get('knownCodes', {}).items():
            if not isinstance(meta, dict):
                continue
            first_seen = _utc(meta.get('firstSeenAt', ''))
            if first_seen and now - timedelta(hours=hours) <= first_seen <= now + timedelta(minutes=5):
                recent.append((first_seen, str(code)))
        return [code for _, code in sorted(recent, reverse=True)]

    async def summary(self, code: str) -> dict[str, int]:
        records = await self.repo.list_auto_redeem_players()
        code = str(code)
        redeemed = {}
        for player in records:
            pid = str(player.get('playerId'))
            redeemed[pid] = {
                'claimed': any(item.get('code') == code for item in player.get('redeemedCodes', [])),
                'failed': code in player.get('failedCodes', {}),
            }

        # Prefer repository event log for success vs already; old records may
        # lack the event and are counted as accepted (unclassified).
        outcomes = {}
        try:
            with self.repo.logs_path.open('r', encoding='utf-8') as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get('code') != code:
                        continue
                    if event.get('event') == 'code_redeemed':
                        outcomes[str(event.get('playerId'))] = 'already' if event.get('already') else 'success'
        except (FileNotFoundError, OSError):
            pass

        result = {'total': len(records), 'success': 0, 'already': 0, 'accepted_unknown': 0,
                  'failed': 0, 'pending': 0}
        for pid, status in redeemed.items():
            if status['claimed']:
                result[outcomes.get(pid, 'accepted_unknown')] += 1
            elif status['failed']:
                result['failed'] += 1
            else:
                result['pending'] += 1
        return result

    def load_messages(self) -> dict[str, Any]:
        try:
            data = json.loads(self.message_path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def save_messages(self, items: dict[str, Any]) -> None:
        # Atomic update; no Discord tokens, form keys, or player IDs are stored.
        import os
        import tempfile
        self.message_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.message_path.parent, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(items, out, ensure_ascii=False, indent=2)
            os.replace(tmp, self.message_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
