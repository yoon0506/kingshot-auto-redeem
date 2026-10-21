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
        self.expiry_path = repository.root / 'data' / 'dmd_code_expiries.json'

    def source_snapshot(self) -> tuple[tuple[str, int, int], ...]:
        """Cheap metadata snapshot for polling; no UID or token data included."""
        paths = (
            self.repo.players_path, self.repo.logs_path, self.repo.codes_path,
            self.expiry_path, self.message_path,
        )
        result = []
        for path in paths:
            try:
                st = path.stat()
                result.append((str(path), st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                result.append((str(path), -1, -1))
        return tuple(result)

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


    def expiry_for(self, code: str) -> datetime | None:
        """Only known explicit UTC expirations; never guess an expiry date."""
        try:
            manual = json.loads(self.expiry_path.read_text(encoding='utf-8'))
            if str(code) in manual:
                return _utc(manual[str(code)])
        except (FileNotFoundError, ValueError, OSError, TypeError):
            pass
        return None

    def detected_expired_at(self, code: str) -> datetime | None:
        """When the bot marked expiry, not necessarily the actual expiry time."""
        try:
            codes = json.loads(self.repo.codes_path.read_text(encoding='utf-8'))
            entry = codes.get('knownCodes', {}).get(str(code), {})
            return _utc(entry.get('expiredAt')) if isinstance(entry, dict) else None
        except (FileNotFoundError, ValueError, OSError, TypeError):
            return None

    def detected_max_uses_at(self, code: str) -> datetime | None:
        """When Kingshot confirmed the code exhausted its global use limit."""
        try:
            codes = json.loads(self.repo.codes_path.read_text(encoding='utf-8'))
            entry = codes.get('knownCodes', {}).get(str(code), {})
            return _utc(entry.get('maxUsesReachedAt')) if isinstance(entry, dict) else None
        except (FileNotFoundError, ValueError, OSError, TypeError):
            return None

    def set_expiry(self, code: str, expires: datetime) -> None:
        """Atomically store UTC expiration without changing redemption history."""
        try:
            current = json.loads(self.expiry_path.read_text(encoding='utf-8'))
            if not isinstance(current, dict):
                current = {}
        except (FileNotFoundError, ValueError, OSError):
            current = {}
        current[str(code)] = expires.astimezone(timezone.utc).isoformat()
        import os
        import tempfile
        self.expiry_path.parent.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=self.expiry_path.parent, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(current, out, ensure_ascii=False, indent=2)
            os.replace(path, self.expiry_path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    async def summary(self, code: str, *, registered_before: datetime | None = None) -> dict[str, int]:
        # For legacy expired codes, reconstruct the eligible cohort from saved
        # registration timestamps. This is a best-effort historical snapshot.
        if registered_before is None:
            records = await self.repo.list_auto_redeem_players()
        else:
            records = await self.repo.list_all_players()
            records = [p for p in records
                       if (stamp := _utc(p.get('registeredAt', '')))
                       and stamp <= registered_before]
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
