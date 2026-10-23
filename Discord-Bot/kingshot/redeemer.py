import asyncio

import logging

import random
import time
import json
import os
import tempfile

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta



from .api import KingshotApi

from .repository import JsonRepository





logger = logging.getLogger("kingshot-bot.redeemer")





@dataclass(frozen=True)

class RedeemJob:

    player: dict

    code: str





class AutoRedeemer:

    def __init__(self, repository: JsonRepository, api: KingshotApi, config: dict) -> None:

        self.repository = repository

        self.api = api

        self.config = config

        self._run_lock = asyncio.Lock()
        self._code_safety_file = self.repository.codes_path.parent / "dmd_code_safety.json"



    async def run_once(self) -> dict[str, int]:

        if self._run_lock.locked():

            logger.info("Auto redeem cycle skipped because another cycle is still running")

            return self._empty_stats()



        async with self._run_lock:

            return await self._run_once_locked()



    async def _run_once_locked(self) -> dict[str, int]:

        stats = self._empty_stats()

        active_codes = await self.api.fetch_active_gift_codes()
        # Include admin-supplied codes even if the upstream listing is delayed or unavailable.
        manual_codes = await self.repository.list_manual_codes()
        active_codes = list(dict.fromkeys([*active_codes, *manual_codes]))

        if not active_codes:

            logger.info("No active gift codes found")

            return stats



        new_codes = await self.repository.update_code_cache(active_codes)

        unredeemable_codes = await self.repository.list_unredeemable_codes()

        active_codes = [code for code in active_codes if code not in unredeemable_codes]

        if unredeemable_codes:

            logger.info(

                "Ignoring %s known unredeemable code(s): %s",

                len(unredeemable_codes),

                ", ".join(sorted(unredeemable_codes)),

            )

        if not active_codes:

            logger.info("No redeemable active gift codes found after filtering unredeemable codes")

            return stats

        # Persistent state machine: initial three-error pause, one limited probe,
        # then permanent automatic block if the SAME error occurs again.
        safety_states = self._load_safety_states()
        if not self._code_safety_file.exists():
            legacy = await self._recent_problem_codes(active_codes)
            for code, (message, until) in legacy.items():
                safety_states[code] = {"status": "cooldown", "reason": message,
                                       "until": until.isoformat()}
            self._save_safety_states(safety_states)
        now = datetime.now(timezone.utc)
        probe_codes: set[str] = set()
        blocked_codes = []
        for code in active_codes:
            state = safety_states.get(code, {})
            status = state.get("status")
            if status == "blocked":
                blocked_codes.append(code)
                logger.warning("DMD CODE SAFETY: code %s permanently blocked for automatic redemption; admin /redeemcode may probe again", code)
            elif status == "cooldown":
                try:
                    until = datetime.fromisoformat(state["until"])
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                except (ValueError, KeyError, TypeError):
                    until = now
                if now < until:
                    blocked_codes.append(code)
                    logger.warning("DMD CODE SAFETY: code %s paused until %s UTC", code, until.isoformat())
                else:
                    probe_codes.add(code)
                    logger.warning("DMD CODE SAFETY: code %s cooldown finished; probing one player only", code)
            elif status == "probe":
                probe_codes.add(code)
                logger.warning("DMD CODE SAFETY: manually retrying code %s with one-player probe", code)
        active_codes = [code for code in active_codes if code not in blocked_codes]
        if not active_codes:
            logger.warning("DMD CODE SAFETY: All available codes are blocked or cooling down; no requests sent.")
            return stats

        # Consecutive identical errors per code in THIS cycle. Successful
        # responses reset the streak. Only 3 distinct UIDs trigger a pause.
        error_streaks: dict[str, tuple[str, set[str]]] = {}



        attempted_pairs: set[tuple[str, str]] = set()

        rate_limit_attempts: dict[tuple[str, str], int] = {}
        timeout_retries: list[tuple[str, str, float]] = []

        last_player_id: str | None = None

        pending_count, player_count = await self._count_missing_jobs(active_codes, attempted_pairs)



        logger.info(

            "Processing %s redeem job(s) for %s player(s); %s new active code(s)",

            pending_count,

            player_count,

            len(new_codes),

        )

        if pending_count == 0:

            logger.info("No unredeemed active gift codes found for registered players")

            return stats



        processed = 0

        while True:

            job, repeats_last_player, pending_count, player_count = await self._next_redeem_job(

                attempted_pairs,

                active_codes,

                last_player_id,

            )

            if job is None:

                break



            pair = (str(job.player["playerId"]), job.code)

            if repeats_last_player:

                await self._same_player_pause(pair[0])



            attempted_pairs.add(pair)

            processed += 1

            logger.info(

                "Redeem job %s started: player %s (%s), code %s | pending after this: %s",

                processed,

                job.player.get("playerName", "Unknown"),

                pair[0],

                job.code,

                max(pending_count - 1, 0),

            )

            result = await self._redeem_for_player(job.player, job.code)

            stats[result] += 1
            was_probe = job.code in probe_codes
            if was_probe:
                updated_probe = await self.repository.get_player(pair[0])
                probe_entry = ((updated_probe or {}).get("failedCodes") or {}).get(job.code, {})
                probe_message = probe_entry.get("message", "") if isinstance(probe_entry, dict) else ""
                previous_reason = str(safety_states.get(job.code, {}).get("reason", ""))
                if result in ("success", "already"):
                    safety_states.pop(job.code, None)
                    logger.info("DMD CODE SAFETY: code %s probe succeeded; normal automatic processing restored", job.code)
                elif result == "failed" and self._code_problem_error(probe_message) == previous_reason:
                    safety_states[job.code] = {"status": "blocked", "reason": previous_reason,
                                               "blockedAt": datetime.now(timezone.utc).isoformat()}
                    logger.error("DMD CODE SAFETY: code %s permanently blocked after repeat '%s'; admin /redeemcode can override", job.code, previous_reason)
                    active_codes = [c for c in active_codes if c != job.code]
                else:
                    # Transient or different error is inconclusive; do not permanently block.
                    until = datetime.now(timezone.utc) + timedelta(minutes=max(5, int(self.config.get("code_error_pause_minutes", 60))))
                    safety_states[job.code] = {"status": "cooldown", "reason": previous_reason,
                                               "until": until.isoformat()}
                    logger.warning("DMD CODE SAFETY: code %s inconclusive probe (%s); paused again until %s", job.code, result, until.isoformat())
                    active_codes = [c for c in active_codes if c != job.code]
                self._save_safety_states(safety_states)
                probe_codes.discard(job.code)
                error_streaks.pop(job.code, None)
            elif result in ("success", "already"):
                error_streaks.pop(job.code, None)
            if result == "failed" and not was_probe:
                updated = await self.repository.get_player(pair[0])
                error = ((updated or {}).get("failedCodes") or {}).get(job.code, {})
                message = error.get("message") if isinstance(error, dict) else None
                if self._is_timeout_retry(message):
                    error_streaks.pop(job.code, None)
                    timeout_retries.append((pair[0], job.code, time.monotonic()))
                    logger.info("Deferred timeout job to end of cycle: player %s, code %s", pair[0], job.code)
                else:
                    problem = self._code_problem_error(message)
                    if problem:
                        prior_problem, affected = error_streaks.get(job.code, (problem, set()))
                        if prior_problem != problem:
                            affected = set()
                        affected.add(pair[0])
                        error_streaks[job.code] = (problem, affected)
                        if len(affected) >= max(2, int(self.config.get("code_error_stop_threshold", 3))):
                            logger.error(
                                "DMD CODE SAFETY TRIGGERED: code %s returned '%s' for %s distinct "
                                "players in a row. Suspending this code for the current cycle; "
                                "persisted failures will suppress it for %s minutes after restart. "
                                "Other codes continue.",
                                job.code, problem, len(affected),
                                max(5, int(self.config.get("code_error_pause_minutes", 60))),
                            )
                            active_codes = [c for c in active_codes if c != job.code]
                            until = datetime.now(timezone.utc) + timedelta(minutes=max(5, int(self.config.get("code_error_pause_minutes", 60))))
                            safety_states[job.code] = {"status": "cooldown", "reason": problem,
                                                       "until": until.isoformat()}
                            self._save_safety_states(safety_states)
                    else:
                        error_streaks.pop(job.code, None)

            logger.info(

                "Redeem job %s finished: %s | totals success=%s already=%s rate_limited=%s expired=%s max_uses=%s failed=%s skipped=%s",

                processed,

                result,

                stats["success"],

                stats["already"],

                stats["rate_limited"],

                stats["expired"],

                stats["max_uses"],

                stats["failed"],

                stats["skipped"],

            )



            if result == "rate_limited":
                error_streaks.pop(job.code, None)

                attempts = rate_limit_attempts.get(pair, 0) + 1

                rate_limit_attempts[pair] = attempts

                if job.code not in safety_states or safety_states.get(job.code, {}).get("status") not in {"cooldown", "blocked"}:
                    may_retry_rate_limit = True
                else:
                    may_retry_rate_limit = False
                if may_retry_rate_limit and attempts < self.config.get("max_rate_limit_retries_per_cycle", 3):

                    attempted_pairs.discard(pair)

                    logger.info(

                        "Rate-limited job will be retried later in this cycle: player %s, code %s (retry %s/%s)",

                        pair[0],

                        job.code,

                        attempts,

                        self.config.get("max_rate_limit_retries_per_cycle", 3),

                    )

                else:

                    logger.warning(

                        "Rate-limited job reached retry limit for this cycle: player %s, code %s",

                        pair[0],

                        job.code,

                    )



            if result == "expired":

                active_codes = [code for code in active_codes if code != job.code]

                logger.info("Expired code %s removed from the current cycle", job.code)



            if result == "max_uses":

                active_codes = [code for code in active_codes if code != job.code]

                logger.info("Maxed-out code %s removed from the current cycle", job.code)



            last_player_id = pair[0]

            pending_count, player_count = await self._count_missing_jobs(active_codes, attempted_pairs)

            if pending_count:

                logger.info(

                    "%s pending redeem job(s) remain across %s active player(s)",

                    pending_count,

                    player_count,

                )

                await self._delay_between_jobs()



        # A second pass is only for TIMEOUT RETRY responses seen in this cycle.
        # The per-pair records already persist through mark_failed(), so a restart
        # is safe; any unfinished retry will be eligible in a future normal cycle.
        retry_wait = max(60.0, float(self.config.get("same_cycle_timeout_retry_seconds", 60)))
        if timeout_retries:
            logger.info("Starting end-of-cycle timeout retries: %s job(s)", len(timeout_retries))
        # DMD retries first; within each alliance priority preserve arrival order.
        retry_players = await self.repository.list_auto_redeem_players()
        dmd_player_ids = {
            str(player.get("playerId")) for player in retry_players
            if self._is_dmd_player(player)
        }
        timeout_retries.sort(key=lambda item: 0 if item[0] in dmd_player_ids else 1)
        for index, (player_id, code, failed_at) in enumerate(timeout_retries, start=1):
            if code not in active_codes:
                logger.info("Skipping timeout retry for %s: code %s no longer active", player_id, code)
                continue
            current = await self.repository.get_player(player_id)
            if not current or not current.get("autoRedeem", True) or current.get("withdrawn", False):
                logger.info("Skipping timeout retry for %s: account unavailable or disabled", player_id)
                continue
            if any(item.get("code") == code for item in current.get("redeemedCodes", [])):
                continue
            remaining = retry_wait - (time.monotonic() - failed_at)
            if remaining > 0:
                logger.info("Timeout retry for player %s waiting %.1fs for minimum cooldown", player_id, remaining)
                await asyncio.sleep(remaining)
            # Re-read to avoid undoing any manual disable / successful claim while waiting.
            current = await self.repository.get_player(player_id)
            if not current or not current.get("autoRedeem", True) or current.get("withdrawn", False):
                continue
            if any(item.get("code") == code for item in current.get("redeemedCodes", [])):
                continue
            logger.info("Timeout retry %s/%s started: player %s, code %s", index, len(timeout_retries), player_id, code)
            outcome = await self._redeem_for_player(current, code)
            if outcome in ("success", "already"):
                stats["failed"] -= 1
                stats[outcome] += 1
            elif outcome == "expired":
                stats["failed"] -= 1
                stats["expired"] += 1
                active_codes = [c for c in active_codes if c != code]
            elif outcome == "max_uses":
                stats["failed"] -= 1
                stats["max_uses"] += 1
                active_codes = [c for c in active_codes if c != code]
            elif outcome == "rate_limited":
                stats["failed"] -= 1
                stats["rate_limited"] += 1
            # A second timeout remains one unresolved failed player, not two failures.
            logger.info("Timeout retry %s/%s finished: %s", index, len(timeout_retries), outcome)
            if index < len(timeout_retries):
                await self._delay_between_jobs()

        await self.repository.log_event("auto_redeem_cycle_finished", **stats)

        logger.info(

            "Auto redeem cycle finished: success=%s already=%s rate_limited=%s expired=%s max_uses=%s failed=%s skipped=%s",

            stats["success"],

            stats["already"],

            stats["rate_limited"],

            stats["expired"],

            stats["max_uses"],

            stats["failed"],

            stats["skipped"],

        )

        return stats



    @staticmethod

    def _empty_stats() -> dict[str, int]:

        return {"success": 0, "already": 0, "rate_limited": 0, "expired": 0, "max_uses": 0, "failed": 0, "skipped": 0}



    async def _next_redeem_job(

        self,

        attempted_pairs: set[tuple[str, str]],

        active_codes: list[str],

        last_player_id: str | None,

    ) -> tuple[RedeemJob | None, bool, int, int]:

        players = await self.repository.list_auto_redeem_players()

        random.shuffle(players)

        candidates: list[RedeemJob] = []

        for player in players:

            player_id = str(player["playerId"])

            codes_for_player = self._codes_for_player(player, active_codes)

            random.shuffle(codes_for_player)

            for code in codes_for_player:

                pair = (player_id, code)

                if pair in attempted_pairs:

                    continue

                candidates.append(RedeemJob(player=player, code=code))

                break



        if not candidates:

            return None, False, 0, len(players)



        # Complete DMD jobs before moving to other alliances.
        dmd_candidates = [job for job in candidates if self._is_dmd_player(job.player)]
        priority_candidates = dmd_candidates or candidates
        preferred = [
            job for job in priority_candidates
            if str(job.player["playerId"]) != str(last_player_id)
        ]
        job = random.choice(preferred or priority_candidates)
        return job, not preferred and last_player_id is not None, len(candidates), len(players)


    @staticmethod
    def _is_dmd_player(player: dict) -> bool:
        """Legacy players with no allianceId belong to DMD (ID 0)."""
        alliance_id = player.get("allianceId", 0)
        try:
            return int(alliance_id) == 0
        except (TypeError, ValueError):
            return False


    async def _count_missing_jobs(

        self,

        active_codes: list[str],

        attempted_pairs: set[tuple[str, str]],

    ) -> tuple[int, int]:

        players = await self.repository.list_auto_redeem_players()

        count = 0

        for player in players:

            player_id = str(player["playerId"])

            for code in self._codes_for_player(player, active_codes):

                if (player_id, code) not in attempted_pairs:

                    count += 1

        return count, len(players)



    def _codes_for_player(self, player: dict, active_codes: list[str]) -> list[str]:
        redeemed_codes = {item.get("code") for item in player.get("redeemedCodes", [])}
        failed_codes = player.get("failedCodes") or {}
        eligible = []
        for code in active_codes:
            if code in redeemed_codes:
                continue
            failure = failed_codes.get(code)
            if isinstance(failure, dict) and self._is_timeout_retry(failure.get("message")):
                try:
                    last_failed = datetime.fromisoformat(str(failure.get("lastFailedAt", "")))
                    if last_failed.tzinfo is None:
                        last_failed = last_failed.replace(tzinfo=timezone.utc)
                    attempts = max(1, int(failure.get("attempts", 1)))
                    # Timeout-only retry cooldown: 5m, 7m, 10m, then 15m maximum.
                    cooldown = (300, 420, 600, 900)[min(attempts, 4) - 1]
                    if (datetime.now(timezone.utc) - last_failed).total_seconds() < cooldown:
                        continue
                except (TypeError, ValueError, OverflowError):
                    # Invalid timestamp: fall back to the prior retry behavior.
                    pass
            eligible.append(code)
        return eligible

    @staticmethod
    def _is_timeout_retry(message: object) -> bool:
        return "TIMEOUT RETRY" in str(message or "").upper()

    @staticmethod
    def _code_problem_error(message: object) -> str | None:
        """Only recognize repeated, code-level application rejections.

        Do not infer a bad code from UID errors, timeouts, rate limiting, or
        HTTP/network errors. Unknown messages should be reviewed explicitly.
        """
        normalized = str(message or "").upper().strip().rstrip(".")
        # RECHARGE_MONEY ERROR appeared across several different 1850 UIDs
        # for VIP1017. It does NOT prove the code is globally invalid.
        return normalized if normalized in {"RECHARGE_MONEY ERROR"} else None

    def _load_safety_states(self) -> dict:
        try:
            with self._code_safety_file.open(encoding="utf-8") as file:
                payload = json.load(file)
            codes = payload.get("codes") if isinstance(payload, dict) else None
            return codes if isinstance(codes, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_safety_states(self, codes: dict) -> None:
        path = self._code_safety_file
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"schemaVersion": 1, "codes": codes}, handle, ensure_ascii=False, indent=2)
            os.replace(temp, path)
        except Exception:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    async def manual_retry_code(self, code: str) -> bool:
        """Admin action: permit one probe for a paused/blocked code, never reset redeemed history."""
        states = self._load_safety_states()
        prior = states.get(code)
        if not isinstance(prior, dict) or prior.get("status") not in {"blocked", "cooldown", "probe"}:
            return False
        states[code] = {"status": "probe", "reason": str(prior.get("reason", "")),
                        "requestedAt": datetime.now(timezone.utc).isoformat()}
        self._save_safety_states(states)
        logger.warning("DMD CODE SAFETY: administrator requested one-player probe for code %s", code)
        return True

    async def _recent_problem_codes(self, codes: list[str]) -> dict[str, tuple[str, datetime]]:
        """Reconstruct temporary pauses from durable failedCodes timestamps.

        No additional state file and no permanent expiry flag. When the third
        distinct player's failure becomes 60 minutes old, this code can be
        tested again. The previous attempts survive process restarts.
        """
        threshold = max(2, int(self.config.get("code_error_stop_threshold", 3)))
        pause = timedelta(minutes=max(5, int(self.config.get("code_error_pause_minutes", 60))))
        now = datetime.now(timezone.utc)
        entries: dict[tuple[str, str], list[datetime]] = {}
        for player in await self.repository.list_all_players():
            for code, failure in (player.get("failedCodes") or {}).items():
                if code not in codes or not isinstance(failure, dict):
                    continue
                problem = self._code_problem_error(failure.get("message"))
                if not problem:
                    continue
                try:
                    recorded = datetime.fromisoformat(str(failure.get("lastFailedAt", "")))
                    if recorded.tzinfo is None:
                        recorded = recorded.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                if recorded <= now and now - recorded < pause:
                    entries.setdefault((code, problem), []).append(recorded)
        blocked: dict[str, tuple[str, datetime]] = {}
        for (code, problem), times in entries.items():
            if len(times) >= threshold:
                # Third newest failure must expire before unpausing.
                times.sort(reverse=True)
                until = times[threshold - 1] + pause
                if until > now and (code not in blocked or until > blocked[code][1]):
                    blocked[code] = (problem, until)
        return blocked



    async def _redeem_for_player(self, player: dict, code: str) -> str:

        player_id = str(player["playerId"])

        redeemed_codes = {item.get("code") for item in player.get("redeemedCodes", [])}

        if code in redeemed_codes:

            return "skipped"



        result = await self.api.redeem_gift_code(
            player_id, code, kingdom=player.get("kingdom") or 1850
        )

        if result["success"] or result["already"]:

            await self.repository.mark_redeemed(player_id, code, result)

            await self.repository.log_event(

                "code_redeemed",

                playerId=player_id,

                discordUserId=player.get("discordUserId"),

                code=code,

                already=result["already"],

            )

            return "already" if result["already"] else "success"



        if result.get("rate_limited"):

            await self.repository.mark_failed(player_id, code, result["message"])

            await self.repository.log_event(

                "code_redeem_rate_limited",

                playerId=player_id,

                discordUserId=player.get("discordUserId"),

                code=code,

                message=result["message"],

            )

            delay = self.config.get("rate_limit_cooldown_seconds", self.config["failure_cooldown_seconds"])

            logger.warning(

                "Kingshot rate limited redeem requests for player %s, code %s. Waiting %ss before continuing.",

                player_id,

                code,

                delay,

            )

            await asyncio.sleep(delay)

            return "rate_limited"



        if result.get("expired"):

            await self.repository.mark_code_expired(code, result["message"])

            await self.repository.log_event(

                "code_expired",

                playerId=player_id,

                discordUserId=player.get("discordUserId"),

                code=code,

                message=result["message"],

            )

            logger.warning(

                "Code %s is expired according to Kingshot. Marked globally expired and will not be retried.",

                code,

            )

            return "expired"



        if result.get("max_uses_reached"):

            await self.repository.mark_code_max_uses_reached(code, result["message"])

            await self.repository.log_event(

                "code_max_uses_reached",

                playerId=player_id,

                discordUserId=player.get("discordUserId"),

                code=code,

                message=result["message"],

            )

            logger.warning(

                "Code %s has reached maximum uses according to Kingshot. Marked globally unredeemable and will not be retried.",

                code,

            )

            return "max_uses"



        # USER INFO ERROR / 40020 is ambiguous: wrong kingdom OR invalid UID.
        # Quarantine the UID, never delete it or claim misconduct.
        if "USER INFO ERROR" in str(result.get("message", "")).upper() or str((result.get("raw") or {}).get("err_code", "")) == "40020":
            await self.repository.mark_failed(player_id, code, result.get("message", "USER INFO ERROR"))
            status = await self.repository.quarantine_invalid_uid(player_id, str(result.get("message", "USER INFO ERROR")))
            await self.repository.log_event("uid_quarantined_for_review", playerId=player_id,
                                            code=code, reason="USER INFO ERROR", status=status)
            logger.warning("DMD UID GUARD: player %s disabled for automatic redemption; "
                           "USER INFO ERROR (kingdom mismatch or invalid UID). "
                           "Admin must verify and use /reactivate.", player_id)
            return "failed"

        await self.repository.mark_failed(player_id, code, result["message"])

        await self.repository.log_event(

            "code_redeem_failed",

            playerId=player_id,

            discordUserId=player.get("discordUserId"),

            code=code,

            message=result["message"],

        )

        if self._is_timeout_retry(result.get("message")):
            logger.warning(
                "Timeout response for player %s, code %s: %s. "
                "Saved for deferred retry; continuing with other players at the normal interval.",
                player_id, code, result.get("message"),
            )
            return "failed"

        # A general application-level rejection must not stall everyone else.
        # Only rate_limited responses retain the global cooldown above.
        # Non-transient errors (e.g. RECHARGE_MONEY ERROR / USER INFO ERROR)
        # remain recorded for review instead of hammering the same UID again.
        logger.warning(
            "Redeem failed for player %s, code %s: %s. "
            "Continuing with the next player at the normal interval (no 180s global pause).",
            player_id, code, result.get("message", "Unknown error"),
        )
        return "failed"



    async def _delay_between_jobs(self) -> None:

        delay = random.triangular(

            self.config["min_delay_seconds"],

            self.config["max_delay_seconds"],

            (self.config["min_delay_seconds"] + self.config["max_delay_seconds"]) / 2,

        )

        logger.info("Waiting %.1fs before the next redeem job", delay)

        await asyncio.sleep(delay)



    async def _same_player_pause(self, player_id: str) -> None:

        delay = self.config.get("same_player_cooldown_seconds", 300)

        logger.info(

            "Only player %s has pending redeem work right now. Waiting %ss before trying this player again.",

            player_id,

            delay,

        )

        await asyncio.sleep(delay)
