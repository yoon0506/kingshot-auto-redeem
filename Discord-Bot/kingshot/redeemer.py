import asyncio

import logging

import random
import time

from dataclasses import dataclass
from datetime import datetime, timezone



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
            if result == "failed":
                updated = await self.repository.get_player(pair[0])
                error = ((updated or {}).get("failedCodes") or {}).get(job.code, {})
                if isinstance(error, dict) and self._is_timeout_retry(error.get("message")):
                    timeout_retries.append((pair[0], job.code, time.monotonic()))
                    logger.info("Deferred timeout job to end of cycle: player %s, code %s", pair[0], job.code)

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

                attempts = rate_limit_attempts.get(pair, 0) + 1

                rate_limit_attempts[pair] = attempts

                if attempts < self.config.get("max_rate_limit_retries_per_cycle", 3):

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



        preferred = [

            job

            for job in candidates

            if str(job.player["playerId"]) != str(last_player_id)

        ]

        job = random.choice(preferred or candidates)

        return job, not preferred and last_player_id is not None, len(candidates), len(players)



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
                    base = max(60, int(self.config.get("timeout_retry_cooldown_seconds", 600)))
                    cap = max(base, int(self.config.get("timeout_retry_max_cooldown_seconds", 3600)))
                    cooldown = min(cap, base * (2 ** min(attempts - 1, 10)))
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
