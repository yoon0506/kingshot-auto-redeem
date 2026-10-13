import asyncio

import json

import os

import tempfile

from dataclasses import dataclass

from pathlib import Path

from typing import Any



from .timeutil import utc_now_iso





@dataclass(frozen=True)

class RegisterDecision:

    allowed: bool

    reason: str = ""





class JsonRepository:

    def __init__(self, root: Path, storage_config: dict[str, str]) -> None:

        self.root = root

        self.users_path = self._path(storage_config["users_file"])

        self.players_path = self._path(storage_config["players_file"])

        self.codes_path = self._path(storage_config["codes_file"])

        self.logs_path = self._path(storage_config["logs_file"])

        self._lock = asyncio.Lock()



    def _path(self, value: str) -> Path:

        path = Path(value)

        if not path.is_absolute():

            path = self.root / path

        return path



    async def can_register(self, discord_user_id: str, player_id: str, max_players: int) -> RegisterDecision:

        async with self._lock:

            users = self._load_json(self.users_path, {"schemaVersion": 1, "users": {}})

            players = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})



            if player_id in players.get("players", {}):

                return RegisterDecision(False, "That player ID is already registered to another Discord user.")



            user = users.get("users", {}).get(discord_user_id, {"playerIds": []})

            if len(user.get("playerIds", [])) >= max_players:

                return RegisterDecision(False, f"You can register up to {max_players} player IDs only.")



            return RegisterDecision(True)



    async def register_player(self, discord_user_id: str, player_info: dict[str, Any]) -> dict[str, Any]:

        async with self._lock:

            users = self._load_json(self.users_path, {"schemaVersion": 1, "users": {}})

            players = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})

            player_id = str(player_info["playerId"])

            now = utc_now_iso()



            users_by_id = users.setdefault("users", {})

            user = users_by_id.setdefault(

                discord_user_id,

                {

                    "discordUserId": discord_user_id,

                    "playerIds": [],

                    "createdAt": now,

                    "updatedAt": now,

                },

            )

            if player_id not in user["playerIds"]:

                user["playerIds"].append(player_id)

            user["updatedAt"] = now



            record = {

                "playerId": player_id,

                "discordUserId": discord_user_id,

                "playerName": player_info.get("name", "Unknown"),

                "kingdom": player_info.get("kingdom"),

                "level": player_info.get("level"),

                "levelRendered": player_info.get("levelRendered", str(player_info.get("level", "?"))),

                "levelRenderedDetailed": player_info.get(

                    "levelRenderedDetailed",

                    player_info.get("levelRendered", str(player_info.get("level", "?"))),

                ),

                "levelImage": _safe_level_image(player_info.get("levelImage")),

                "profilePhoto": player_info.get("profilePhoto"),

                "autoRedeem": True,

                "registeredAt": now,

                "updatedAt": now,

                "redeemedCodes": [],

                "failedCodes": {},

            }

            players.setdefault("players", {})[player_id] = record



            self._save_json(self.users_path, users)

            self._save_json(self.players_path, players)

            return record



    async def bulk_register(self, discord_user_id: str, player_ids: list[str], kingdom: int = 1850) -> dict[str, list[str]]:
        """Admin-only caller. Deduplicate and persist player records without a user quota."""
        async with self._lock:
            users = self._load_json(self.users_path, {"schemaVersion": 1, "users": {}})
            players = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})
            owner = str(discord_user_id)
            now = utc_now_iso()
            user = users.setdefault("users", {}).setdefault(owner, {
                "discordUserId": owner, "playerIds": [], "createdAt": now, "updatedAt": now
            })
            all_players = players.setdefault("players", {})
            added, existing = [], []
            for player_id in dict.fromkeys(player_ids):
                if player_id in all_players:
                    existing.append(player_id)
                    continue
                all_players[player_id] = {
                    "playerId": player_id, "discordUserId": owner,
                    "playerName": f"UID {player_id}", "kingdom": kingdom,
                    "level": None, "levelRendered": "?", "levelRenderedDetailed": "?",
                    "levelImage": None, "profilePhoto": None,
                    "autoRedeem": True, "registeredAt": now, "updatedAt": now,
                    "redeemedCodes": [], "failedCodes": {},
                }
                if player_id not in user["playerIds"]:
                    user["playerIds"].append(player_id)
                added.append(player_id)
            if added:
                user["updatedAt"] = now
                self._save_json(self.players_path, players)
                self._save_json(self.users_path, users)
            return {"added": added, "existing": existing}

    async def set_kingdom(self, player_id: str, kingdom: int) -> tuple[str, int | None]:
        """Update only one registered player's displayed kingdom, atomically."""
        if not (1 <= kingdom <= 99999):
            raise ValueError("Kingdom must be between 1 and 99999")
        async with self._lock:
            data = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})
            player = data.get("players", {}).get(str(player_id))
            if not isinstance(player, dict):
                return "not_found", None
            old = player.get("kingdom")
            if str(old) == str(kingdom):
                return "unchanged", old
            player["kingdom"] = kingdom
            player["updatedAt"] = utc_now_iso()
            self._save_json(self.players_path, data)
            return "changed", old

    async def set_withdrawn(self, player_id: str, withdrawn: bool) -> str:
        """Admin-only state transition; retain existing redemption history."""
        async with self._lock:
            payload = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})
            player = payload.get("players", {}).get(str(player_id))
            if not isinstance(player, dict):
                return "not_found"
            if bool(player.get("withdrawn", False)) == withdrawn:
                return "unchanged"
            player["withdrawn"] = withdrawn
            player["autoRedeem"] = not withdrawn
            player["updatedAt"] = utc_now_iso()
            player["withdrawnAt"] = utc_now_iso() if withdrawn else None
            self._save_json(self.players_path, payload)
            return "changed"

    async def bulk_remove(self, player_ids: list[str]) -> dict[str, list[str]]:
        """Admin-only caller. Delete players and unlink from their real Discord owners."""
        async with self._lock:
            users = self._load_json(self.users_path, {"schemaVersion": 1, "users": {}})
            players = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})
            all_players = players.setdefault("players", {})
            removed, missing = [], []
            now = utc_now_iso()
            for player_id in dict.fromkeys(player_ids):
                record = all_players.pop(player_id, None)
                if record is None:
                    missing.append(player_id)
                    continue
                owner = str(record.get("discordUserId", ""))
                user = users.get("users", {}).get(owner)
                if user:
                    user["playerIds"] = [pid for pid in user.get("playerIds", []) if str(pid) != player_id]
                    user["updatedAt"] = now
                removed.append(player_id)
            if removed:
                self._save_json(self.players_path, players)
                self._save_json(self.users_path, users)
            return {"removed": removed, "missing": missing}

    async def list_players_for_user(self, discord_user_id: str) -> list[dict[str, Any]]:

        async with self._lock:

            users = self._load_json(self.users_path, {"schemaVersion": 1, "users": {}})

            players = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})

            player_ids = users.get("users", {}).get(discord_user_id, {}).get("playerIds", [])

            return [players.get("players", {})[pid] for pid in player_ids if pid in players.get("players", {})]



    async def list_all_players(self) -> list[dict[str, Any]]:

        async with self._lock:

            players = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})

            return list(players.get("players", {}).values())



    async def get_player(self, player_id: str) -> dict[str, Any] | None:

        async with self._lock:

            players = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})

            return players.get("players", {}).get(str(player_id))



    async def remove_player(self, discord_user_id: str, player_id: str) -> None:

        async with self._lock:

            users = self._load_json(self.users_path, {"schemaVersion": 1, "users": {}})

            players = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})

            player_id = str(player_id)



            players.get("players", {}).pop(player_id, None)

            user = users.get("users", {}).get(discord_user_id)

            if user:

                user["playerIds"] = [pid for pid in user.get("playerIds", []) if pid != player_id]

                user["updatedAt"] = utc_now_iso()



            self._save_json(self.users_path, users)

            self._save_json(self.players_path, players)



    async def set_auto_redeem(self, player_id: str, enabled: bool) -> None:

        async with self._lock:

            players = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})

            player = players.get("players", {}).get(str(player_id))

            if player:

                if enabled and player.get("withdrawn", False):
                    raise ValueError("Withdrawn player requires admin /reactivate")
                player["autoRedeem"] = enabled

                player["updatedAt"] = utc_now_iso()

                self._save_json(self.players_path, players)



    async def list_auto_redeem_players(self) -> list[dict[str, Any]]:

        async with self._lock:

            players = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})

            records = list(players.get("players", {}).values())

            return [player for player in records if player.get("autoRedeem", True) and not player.get("withdrawn", False)]



    async def add_manual_code(self, code: str) -> bool:
        """Store a manually supplied gift code in the existing persistent cache."""
        async with self._lock:
            data = self._load_json(self.codes_path, {"schemaVersion": 1, "knownCodes": {}, "lastFetchedAt": None})
            codes = data.setdefault("knownCodes", {})
            now = utc_now_iso()
            record = codes.get(code)
            if isinstance(record, dict):
                if record.get("expiredAt") or record.get("maxUsesReachedAt"):
                    return False
                record["manual"] = True
                record["lastSeenAt"] = now
            else:
                codes[code] = {"code": code, "manual": True, "firstSeenAt": now, "lastSeenAt": now}
            self._save_json(self.codes_path, data)
            return True

    async def list_manual_codes(self) -> list[str]:
        async with self._lock:
            data = self._load_json(self.codes_path, {"schemaVersion": 1, "knownCodes": {}})
            return [
                str(code) for code, item in data.get("knownCodes", {}).items()
                if isinstance(item, dict) and item.get("manual")
                and not item.get("expiredAt") and not item.get("maxUsesReachedAt")
            ]

    async def update_code_cache(self, active_codes: list[str]) -> list[str]:

        async with self._lock:

            data = self._load_json(self.codes_path, {"schemaVersion": 1, "knownCodes": {}, "lastFetchedAt": None})

            known_codes = data.setdefault("knownCodes", {})

            now = utc_now_iso()

            new_codes = [code for code in active_codes if code not in known_codes]



            for code in active_codes:

                known_codes.setdefault(code, {"code": code, "firstSeenAt": now, "lastSeenAt": now})

                known_codes[code]["lastSeenAt"] = now



            data["lastFetchedAt"] = now

            self._save_json(self.codes_path, data)

            return new_codes



    async def list_expired_codes(self) -> set[str]:

        async with self._lock:

            data = self._load_json(self.codes_path, {"schemaVersion": 1, "knownCodes": {}, "lastFetchedAt": None})

            known_codes = data.get("knownCodes", {})

            return {

                str(code)

                for code, record in known_codes.items()

                if isinstance(record, dict) and record.get("expiredAt")

            }



    async def list_unredeemable_codes(self) -> set[str]:

        async with self._lock:

            data = self._load_json(self.codes_path, {"schemaVersion": 1, "knownCodes": {}, "lastFetchedAt": None})

            known_codes = data.get("knownCodes", {})

            return {

                str(code)

                for code, record in known_codes.items()

                if isinstance(record, dict) and (record.get("expiredAt") or record.get("maxUsesReachedAt"))

            }



    async def mark_code_expired(self, code: str, message: str) -> None:

        async with self._lock:

            data = self._load_json(self.codes_path, {"schemaVersion": 1, "knownCodes": {}, "lastFetchedAt": None})

            known_codes = data.setdefault("knownCodes", {})

            now = utc_now_iso()

            record = known_codes.setdefault(str(code), {"code": str(code), "firstSeenAt": now, "lastSeenAt": now})

            record["expiredAt"] = now

            record["expiredMessage"] = message

            self._save_json(self.codes_path, data)



    async def mark_code_max_uses_reached(self, code: str, message: str) -> None:

        async with self._lock:

            data = self._load_json(self.codes_path, {"schemaVersion": 1, "knownCodes": {}, "lastFetchedAt": None})

            known_codes = data.setdefault("knownCodes", {})

            now = utc_now_iso()

            record = known_codes.setdefault(str(code), {"code": str(code), "firstSeenAt": now, "lastSeenAt": now})

            record["maxUsesReachedAt"] = now

            record["maxUsesReachedMessage"] = message

            self._save_json(self.codes_path, data)



    async def mark_redeemed(self, player_id: str, code: str, result: dict[str, Any]) -> None:

        async with self._lock:

            players = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})

            player = players.get("players", {}).get(str(player_id))

            if not player:

                return



            history = player.setdefault("redeemedCodes", [])

            if not any(item.get("code") == code for item in history):

                history.append(

                    {

                        "code": code,

                        "redeemedAt": utc_now_iso(),

                        "message": result.get("message", ""),

                    }

                )

            player.setdefault("failedCodes", {}).pop(code, None)

            player["updatedAt"] = utc_now_iso()

            self._save_json(self.players_path, players)



    async def mark_failed(self, player_id: str, code: str, message: str) -> None:

        async with self._lock:

            players = self._load_json(self.players_path, {"schemaVersion": 1, "players": {}})

            player = players.get("players", {}).get(str(player_id))

            if not player:

                return



            failed = player.setdefault("failedCodes", {})

            entry = failed.setdefault(code, {"attempts": 0})

            entry["attempts"] += 1

            entry["lastFailedAt"] = utc_now_iso()

            entry["message"] = message

            player["updatedAt"] = utc_now_iso()

            self._save_json(self.players_path, players)



    async def log_event(self, event_type: str, **fields: Any) -> None:

        async with self._lock:

            self.logs_path.parent.mkdir(parents=True, exist_ok=True)

            record = {"timestamp": utc_now_iso(), "event": event_type, **fields}

            with self.logs_path.open("a", encoding="utf-8") as file:

                file.write(json.dumps(record, ensure_ascii=False) + "\n")



    @staticmethod

    def _load_json(path: Path, default: Any) -> Any:

        if not path.exists():

            return default

        try:

            with path.open("r", encoding="utf-8") as file:

                return json.load(file)

        except (json.JSONDecodeError, OSError):

            return default



    @staticmethod

    def _save_json(path: Path, data: Any) -> None:

        path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")

        try:

            with os.fdopen(fd, "w", encoding="utf-8") as file:

                json.dump(data, file, indent=2, ensure_ascii=False)

            os.replace(tmp_path, path)

        except Exception:

            try:

                os.unlink(tmp_path)

            except OSError:

                pass

            raise





def _safe_level_image(value: Any) -> Any:

    if isinstance(value, str) and value.startswith(("http://", "https://")):

        return None

    return value
