"""DMD / Kingshot API bridge.

Codes are discovered via the configured public code feed; redemption uses the
Century Games gift-code endpoint and requires both UID (fid) and kingdom (kid).
Only use for players who have opted in to gift redemption.
"""
import asyncio
import hashlib
import json
import logging
import time
from typing import Any
from collections import Counter
import aiohttp

logger = logging.getLogger("kingshot-bot.api")
OFFICIAL_REDEEM_URL = "https://kingshot-giftcode.centurygame.com/api/gift_code"
OFFICIAL_ORIGIN = "https://kingshot-giftcode.centurygame.com"
# Public client signature value from the open-source reference implementation.
# It is not a Discord credential; if the operator changes this protocol, update it.
SIGNATURE_SUFFIX = "mN4!pQs6JrYwV9"

class KingshotApi:
    def __init__(self, base_url: str, max_retries: int = 3, backoff_base: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._session: aiohttp.ClientSession | None = None
        self._redeem_metrics: Counter[str] = Counter()
        self._redeem_latency_total_ms = 0.0

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_player_info(self, player_id: str) -> dict[str, Any] | None:
        # Intentionally not used. That third-party API was removed.
        return None

    async def fetch_active_gift_codes(self) -> list[str]:
        # Discovery feed is separate from the official redemption API.
        # If unavailable, do not treat this as proof there are no codes.
        url = self.base_url + "/gift-codes"
        try:
            session = await self.session()
            async with session.get(url, headers={"Accept": "application/json"}) as response:
                if response.status != 200:
                    logger.warning("Code discovery returned HTTP %s", response.status)
                    return []
                data = await response.json(content_type=None)
                if not isinstance(data, dict) or data.get("status") != "success":
                    logger.warning("Code discovery returned an unsuccessful response")
                    return []
                return self._extract_codes(data.get("data", []))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("Code discovery request failed: %s", type(exc).__name__)
            return []

    async def redeem_gift_code(self, player_id: str, gift_code: str, kingdom: int | str = 1850) -> dict[str, Any]:
        """Record privacy-safe request timing without changing redemption semantics."""
        started = time.perf_counter()
        try:
            result = await self._redeem_gift_code_request(player_id, gift_code, kingdom)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._record_redeem_metric("unexpected_exception", elapsed_ms)
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        category = self._redeem_metric_category(result)
        self._record_redeem_metric(category, elapsed_ms)
        return result

    @staticmethod
    def _redeem_metric_category(result: dict[str, Any]) -> str:
        message = str(result.get("message", "")).upper()
        raw = result.get("raw") or {}
        err_code = str(raw.get("err_code", "")) if isinstance(raw, dict) else ""
        if message.startswith("HTTP 429"):
            return "http_429"
        if err_code == "40019" or "TOO FREQUENT" in message:
            return "uid_40019_or_too_frequent"
        if "TIMEOUT RETRY" in message or "TIMEOUTERROR" in message:
            return "timeout"
        if result.get("success"):
            return "success"
        if result.get("already"):
            return "already"
        if result.get("expired"):
            return "expired"
        if result.get("max_uses_reached"):
            return "max_uses"
        if result.get("rate_limited"):
            return "other_rate_limit"
        if "USER INFO ERROR" in message:
            return "invalid_uid_or_kingdom"
        if message.startswith("HTTP 5"):
            return "http_5xx"
        return "other_error"

    def _record_redeem_metric(self, category: str, elapsed_ms: float) -> None:
        # Only category and duration: never log UID, gift code, signing data or secret.
        self._redeem_metrics[category] += 1
        self._redeem_latency_total_ms += elapsed_ms
        total = self._redeem_metrics.total()
        logger.info("DMD API metric: category=%s duration_ms=%.0f", category, elapsed_ms)
        if total % 10 == 0:
            avg = self._redeem_latency_total_ms / total
            logger.info(
                "DMD API metrics summary (since restart): requests=%d avg_ms=%.0f categories=%s",
                total, avg, dict(sorted(self._redeem_metrics.items())),
            )

    async def _redeem_gift_code_request(self, player_id: str, gift_code: str, kingdom: int | str = 1850) -> dict[str, Any]:
        # Use each registered player's kingdom; 1850 remains the legacy default.
        kingdom = str(kingdom).strip()
        if not (player_id.isascii() and player_id.isdigit() and kingdom.isascii() and kingdom.isdigit()
                and 1 <= int(kingdom) <= 99999):
            return self._failure("Invalid UID or kingdom")
        values = {"fid": str(player_id), "cdk": str(gift_code), "kid": kingdom, "time": str(int(time.time()))}
        raw = "&".join(f"{key}={values[key]}" for key in sorted(values))
        signature = hashlib.md5((raw + SIGNATURE_SUFFIX).encode("utf-8")).hexdigest()
        payload = {"sign": signature, **values}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": OFFICIAL_ORIGIN,
            "Referer": OFFICIAL_ORIGIN + "/",
        }
        try:
            session = await self.session()
            async with session.post(OFFICIAL_REDEEM_URL, data=payload, headers=headers) as response:
                if response.status == 429:
                    return self._failure("HTTP 429 rate limited", rate_limited=True)
                if response.status >= 500:
                    logger.warning("Official redeem API HTTP %s", response.status)
                    return self._failure(f"HTTP {response.status}")
                try:
                    result = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    logger.warning("Official redeem API returned non-JSON HTTP %s", response.status)
                    return self._failure(f"Non-JSON HTTP {response.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Official redeem request failed: %s", type(exc).__name__)
            return self._failure(type(exc).__name__)

        if not isinstance(result, dict):
            return self._failure("Invalid JSON response")
        msg = str(result.get("msg") or result.get("message") or "Unknown response").strip().rstrip(".")
        err = str(result.get("err_code") or "")
        success = msg in {"SUCCESS", "SAME TYPE EXCHANGE"}
        already = msg == "RECEIVED" and err == "40008"
        rate_limited = msg == "TOO FREQUENT" or err == "40019"
        expired = msg in {"TIME ERROR", "CDK NOT FOUND"}
        max_uses = msg == "USED"
        if msg == "USER INFO ERROR" or err == "40020":
            msg = f"Wrong kingdom or invalid UID for K{kingdom} (USER INFO ERROR)"
        return {
            "success": success, "already": already,
            "rate_limited": rate_limited, "expired": expired,
            "max_uses_reached": max_uses, "message": msg,
            # Avoid printing request signatures in logs.
            "raw": {"msg": str(result.get("msg", "")), "err_code": err},
        }

    @staticmethod
    def _failure(message: str, rate_limited: bool = False) -> dict[str, Any]:
        return {"success": False, "already": False, "rate_limited": rate_limited,
                "expired": False, "max_uses_reached": False, "message": message, "raw": {}}

    @staticmethod
    def _extract_codes(data: Any) -> list[str]:
        if isinstance(data, dict):
            data = data.get("giftCodes") or data.get("codes") or data.get("activeCodes") or []
        if not isinstance(data, list):
            return []
        codes = []
        for item in data:
            if isinstance(item, str):
                codes.append(item)
            elif isinstance(item, dict):
                code = item.get("code") or item.get("giftCode") or item.get("gift_code")
                if code:
                    codes.append(str(code))
        return codes
