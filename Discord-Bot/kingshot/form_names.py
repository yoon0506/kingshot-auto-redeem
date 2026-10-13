"""Lightweight nickname updates for existing Kingshot registrations."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .repository import JsonRepository


async def update_player_names(repository: "JsonRepository", names: dict[str, str]) -> int:
    """Update names only; retain owners, redeemed codes and all other fields.

    Empty names are ignored. Writes players.json once only if something changed.
    """
    async with repository._lock:
        payload = repository._load_json(repository.players_path, {"schemaVersion": 1, "players": {}})
        records = payload.get("players", {})
        changed = 0
        for uid, name in names.items():
            name = name.strip()
            if not name:
                continue
            record = records.get(uid)
            if not isinstance(record, dict):
                continue
            if record.get("playerName") != name:
                record["playerName"] = name
                changed += 1
        if changed:
            repository._save_json(repository.players_path, payload)
        return changed
