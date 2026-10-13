import asyncio
import json
import hashlib

import logging

import os
from datetime import datetime, timedelta, timezone
import re

from pathlib import Path



import aiohttp
import discord

from discord import app_commands

from discord.ext import tasks

from dotenv import load_dotenv



from kingshot.api import KingshotApi

from kingshot.config import load_config

from kingshot.redeemer import AutoRedeemer

from kingshot.repository import JsonRepository
from kingshot.reporting import ReportStore
from kingshot.form_names import update_player_names

from kingshot.ui import build_player_embed, build_player_embeds





ROOT = Path(__file__).resolve().parent

CONFIG = load_config(ROOT / "config.json")

# Administrator-editable footer, stored in the persistent data directory.
REPORT_MENT_FILE = ROOT / "data" / "report_ment.json"
DEFAULT_REPORT_MENT = "토마토를 많이 많이 먹으세요! 🍅🍅🍅🍅🍅"


def load_report_ment() -> str:
    try:
        data = json.loads(REPORT_MENT_FILE.read_text(encoding="utf-8"))
        value = data.get("ment") if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip():
            return value
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning("Could not load report ment: %s", exc)
    return DEFAULT_REPORT_MENT


def save_report_ment(value: str) -> None:
    REPORT_MENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = REPORT_MENT_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps({"ment": value}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, REPORT_MENT_FILE)




logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s %(levelname)s %(name)s: %(message)s",

)

logger = logging.getLogger("kingshot-bot")





class _DiscordVoiceWarningFilter(logging.Filter):

    def filter(self, record: logging.LogRecord) -> bool:

        return "voice will NOT be supported" not in record.getMessage()





logging.getLogger("discord.client").addFilter(_DiscordVoiceWarningFilter())



load_dotenv(ROOT / ".env")





def _optional_int_env(name: str) -> int | None:

    value = os.getenv(name, "").strip()

    return int(value) if value.isdigit() else None





def _config_id_set(section: str, key: str) -> set[int]:

    ids = CONFIG.get(section, {}).get(key, [])

    return {int(value) for value in ids if str(value).isdigit()}





class KingshotBot(discord.Client):

    def __init__(self) -> None:

        intents = discord.Intents.default()

        intents.guild_messages = True

        super().__init__(

            intents=intents,

            application_id=_optional_int_env("APP_ID"),

        )

        self.tree = app_commands.CommandTree(self)

        self.delete_message_channel_ids = _config_id_set(

            "moderation",

            "delete_normal_messages_channel_ids",

        )



        self.repository = JsonRepository(ROOT, CONFIG["storage"])

        self.api = KingshotApi(

            base_url=CONFIG["api"]["base_url"],

            max_retries=CONFIG["auto_redeem"]["max_retries"],

            backoff_base=CONFIG["auto_redeem"]["retry_backoff_base"],

        )

        self.redeemer = AutoRedeemer(self.repository, self.api, CONFIG["auto_redeem"])
        self.report_store = ReportStore(self.repository)
        self.report_channel_id = _optional_int_env("DMD_REPORT_CHANNEL_ID")
        self._report_lock = asyncio.Lock()
        self._form_sync_url = os.getenv("DMD_SYNC_URL", "").strip()
        self._form_sync_key = os.getenv("DMD_SYNC_KEY", "").strip()
        # Memory-only caches: safe to rebuild after a restart.
        self._form_last_digest: str | None = None
        self._report_last_snapshot: tuple | None = None
        self._health_form_ok: datetime | None = None
        self._health_redeem_ok: datetime | None = None
        self._health_redeem_error: str | None = None
        self._health_lock = asyncio.Lock()
        self._health_file = ROOT / "data" / "dmd_health_message.json"




   
    async def setup_hook(self) -> None:
        commands = await self.tree.sync()
        logger.info(
            "Synced %d global commands: %s",
            len(commands),
            ", ".join(command.name for command in commands),
        )

        if CONFIG["auto_redeem"].get("enabled", True):
            self.auto_redeem_loop.change_interval(
                seconds=CONFIG["auto_redeem"]["poll_interval_seconds"]
            )
            self.auto_redeem_loop.start()

        if self._form_sync_url and self._form_sync_key:
            self.form_sync_loop.start()
            logger.info("DMD form sync enabled (interval: 5 minutes)")
        else:
            logger.info("DMD form sync disabled: DMD_SYNC_URL or DMD_SYNC_KEY missing")

        if self.report_channel_id:
            self.report_loop.start()
            self.health_loop.start()
            logger.info("DMD redemption reports enabled (interval: 5 minutes)")
            logger.info("DMD healthcheck enabled (interval: 60 minutes)")
        else:
            logger.info("DMD redemption reports disabled: DMD_REPORT_CHANNEL_ID missing")




    async def close(self) -> None:
        if self.health_loop.is_running():
            self.health_loop.cancel()
        if self.report_loop.is_running():
            self.report_loop.cancel()
        if self.form_sync_loop.is_running():
            self.form_sync_loop.cancel()
        if self.auto_redeem_loop.is_running():
            self.auto_redeem_loop.cancel()

        await self.api.close()

        await super().close()



    async def run_redeem_checked(self) -> dict[str, int]:
        try:
            stats = await self.redeemer.run_once()
        except Exception as exc:
            self._health_redeem_error = type(exc).__name__
            raise
        self._health_redeem_ok = datetime.now(timezone.utc)
        self._health_redeem_error = None
        return stats

    @tasks.loop(seconds=300)

    async def auto_redeem_loop(self) -> None:

        await self.wait_until_ready()

        await self.run_redeem_checked()



    @tasks.loop(minutes=5)
    async def form_sync_loop(self) -> None:
        await self.wait_until_ready()
        await self.sync_form_players()

    async def sync_form_players(self) -> None:
        # Google Form sync uses one designated admin as the record owner.
        # The form does not collect Discord user IDs, so self-service commands
        # for these imported UIDs remain restricted to that admin.
        admin_ids = CONFIG.get("admins", {}).get("user_ids", [])
        owner_id = next((str(uid) for uid in admin_ids if str(uid).isdigit()), None)
        if owner_id is None:
            logger.error("Form sync skipped: set an admin Discord user ID in config.json")
            return

        if not (self._form_sync_url.startswith("https://script.google.com/macros/s/")
                and self._form_sync_url.split("?", 1)[0].endswith("/exec")):
            logger.error("Form sync skipped: invalid Google Apps Script /exec URL")
            return

        try:
            timeout = aiohttp.ClientTimeout(total=40)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self._form_sync_url,
                    json={"key": self._form_sync_key},
                    allow_redirects=True,
                ) as response:
                    response.raise_for_status()
                    body = await response.content.read(2_000_001)
                    if len(body) > 2_000_000:
                        raise ValueError("Form response too large")
                    payload = json.loads(body.decode("utf-8"))
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError, ValueError) as exc:
            logger.warning("DMD form sync request failed: %s", type(exc).__name__)
            return

        if not isinstance(payload, dict) or payload.get("success") is not True:
            logger.warning("DMD form sync rejected or returned invalid data")
            return
        players = payload.get("players")
        if not isinstance(players, list) or len(players) > 5000:
            logger.warning("DMD form sync returned an invalid player list")
            return

        # Keep names as well as UIDs: a renamed player must trigger a sync.
        # For duplicate form submissions, the latest non-empty nickname wins.
        names: dict[str, str] = {}
        invalid_count = 0
        for player in players:
            if not isinstance(player, dict) or str(player.get("kingdom")) != "1850":
                invalid_count += 1
                continue
            uid = str(player.get("uid", "")).strip()
            if not (uid.isascii() and uid.isdecimal() and 5 <= len(uid) <= 15):
                invalid_count += 1
                continue
            name = str(player.get("name") or "").strip()
            name = " ".join(name.split())[:80]
            if uid not in names or name:
                names[uid] = name or names.get(uid, "")

        if not names:
            logger.info("DMD form sync: no valid UIDs (invalid=%d)", invalid_count)
            return

        # Include normalized names in the digest, so nickname edits are not missed.
        normalized = json.dumps(sorted(names.items()), ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if digest == self._form_last_digest:
            self._health_form_ok = datetime.now(timezone.utc)
            logger.debug("DMD form sync: UID/nickname inputs unchanged; skipped")
            return

        added_count = 0
        existing_count = 0
        changed_names = 0
        try:
            ids = list(names)
            for start in range(0, len(ids), 50):
                result = await self.repository.bulk_register(owner_id, ids[start:start + 50], kingdom=1850)
                added_count += len(result["added"])
                existing_count += len(result["existing"])
            # Only update playerName on existing records. No redemption history,
            # account ownership, or auto-redeem settings are modified.
            changed_names = await update_player_names(self.repository, names)
            if added_count:
                await self.repository.log_event(
                    "google_form_players_registered", added=added_count,
                    existing=existing_count, invalid=invalid_count,
                )
                self.wake_auto_redeem(f"Google Form sync added {added_count} player(s)")
        except Exception:
            logger.exception("DMD form sync failed while saving players")
            return

        self._form_last_digest = digest
        self._health_form_ok = datetime.now(timezone.utc)
        logger.info(
            "DMD form sync finished: added=%d existing=%d renamed=%d invalid=%d",
            added_count, existing_count, changed_names, invalid_count,
        )

    @tasks.loop(minutes=5)
    async def report_loop(self) -> None:
        await self.wait_until_ready()
        # The active reports depend on the players, redeemed/failed events,
        # code-cache/expiry metadata and message ID store. Check those file
        # fingerprints first to avoid re-reading and aggregating each player.
        snapshot = self.report_store.source_snapshot()
        if snapshot == self._report_last_snapshot:
            logger.debug("DMD report: inputs unchanged; aggregation skipped")
            return
        successful = True
        for code in self.report_store.recent_codes():
            result = await self.publish_redeem_report(code)
            if result not in ("Report unchanged.", f"Report posted/updated for {code}."):
                successful = False
        # Recheck after updating the report message-ID store ourselves.
        # On transient Discord failures don't suppress the next retry.
        if successful:
            self._report_last_snapshot = self.report_store.source_snapshot()

    @staticmethod
    def _health_age(value: datetime | None) -> str:
        if value is None:
            return "아직 성공 확인 없음 (재시작 이후)"
        seconds = max(0, int((datetime.now(timezone.utc) - value).total_seconds()))
        return f"{seconds // 60}분 전" if seconds < 86400 else f"{seconds // 3600}시간 전"

    async def health_embed(self) -> discord.Embed:
        # Read-only data: neither credentials nor individual UIDs are included.
        players = await self.repository.list_all_players()
        active = sum(bool(p.get("autoRedeem", True)) and not p.get("withdrawn", False) for p in players)
        withdrawn = sum(bool(p.get("withdrawn", False)) for p in players)
        now = datetime.now(timezone.utc)
        form_age = ((now - self._health_form_ok).total_seconds() if self._health_form_ok else None)
        redeem_age = ((now - self._health_redeem_ok).total_seconds() if self._health_redeem_ok else None)
        form_ok = bool(self._form_sync_url and self._form_sync_key and self.form_sync_loop.is_running() and form_age is not None and form_age <= 900)
        redeem_enabled = CONFIG["auto_redeem"].get("enabled", True)
        redeem_ok = bool(redeem_enabled and self.auto_redeem_loop.is_running() and redeem_age is not None and redeem_age <= 5400 and not self._health_redeem_error)
        status = "🟢 정상" if form_ok and redeem_ok else "🟠 확인 필요"
        embed = discord.Embed(title="🍅 DMD Gift Bot — Healthcheck", description=f"**상태: {status}**\n왕국 1850 · 자동 상태 보고 (1시간)", color=discord.Color.green() if form_ok and redeem_ok else discord.Color.orange())
        embed.add_field(name="👥 전체 UID", value=str(len(players)), inline=True)
        embed.add_field(name="✅ 자동 수령 ON", value=str(active), inline=True)
        embed.add_field(name="⏸️ 수령 OFF", value=str(len(players)-active), inline=True)
        embed.add_field(name="🚫 철회", value=str(withdrawn), inline=True)
        embed.add_field(name="📋 구글폼 동기화", value=f"{'✅' if form_ok else '⚠️'} {self._health_age(self._health_form_ok)}", inline=False)
        embed.add_field(name="🎁 쿠폰 검사 사이클", value=f"{'✅' if redeem_ok else '⚠️'} {self._health_age(self._health_redeem_ok)}", inline=False)
        if self._health_redeem_error:
            embed.add_field(name="⚠️ 최근 쿠폰 처리 오류", value=self._health_redeem_error, inline=False)
        embed.set_footer(text="봇 프로세스 내부 확인 기준 · 외부 장애 감시는 별도로 필요")
        return embed

    async def publish_health(self, *, force: bool = False) -> str:
        if not self.report_channel_id:
            return "리포트 채널 미설정"
        async with self._health_lock:
            embed = await self.health_embed()
            channel = self.get_channel(self.report_channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(self.report_channel_id)
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    return "헬스체크 채널 접근 실패"
            if not hasattr(channel, "send") or not hasattr(channel, "fetch_message"):
                return "텍스트 채널이 필요합니다"
            message_id = None
            try:
                data = json.loads(self._health_file.read_text(encoding="utf-8"))
                message_id = int(data.get("message_id", 0))
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            msg = None
            if message_id:
                try:
                    msg = await channel.fetch_message(message_id)
                    await msg.edit(embed=embed)
                except discord.NotFound:
                    msg = None
                except (discord.Forbidden, discord.HTTPException):
                    return "기존 헬스체크 메시지 수정 실패"
            if msg is None:
                try:
                    msg = await channel.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException):
                    return "헬스체크 메시지 전송 실패"
            self._health_file.parent.mkdir(parents=True, exist_ok=True)
            temp = self._health_file.with_suffix(".tmp")
            temp.write_text(json.dumps({"message_id": msg.id}), encoding="utf-8")
            os.replace(temp, self._health_file)
            return "헬스체크 리포트 갱신 완료"

    @tasks.loop(hours=1)
    async def health_loop(self) -> None:
        await self.wait_until_ready()
        # Delay first automatic publication until routine checks have started.
        await asyncio.sleep(90)
        result = await self.publish_health()
        if result != "헬스체크 리포트 갱신 완료":
            logger.warning("DMD healthcheck: %s", result)

    async def publish_redeem_report(self, code: str, *, force: bool = False) -> str:
        """Maintain a single private-channel message per code across restarts."""
        if not self.report_channel_id:
            return "DMD_REPORT_CHANNEL_ID is not configured."
        expiry = self.report_store.expiry_for(code)
        detected_expiry = self.report_store.detected_expired_at(code)
        now = datetime.now(timezone.utc)
        async with self._report_lock:
            saved = self.report_store.load_messages()
            key = f"{self.report_channel_id}:{code}"
            record = saved.get(key, {})
            expiry_cutoff = detected_expiry or (expiry if expiry and now >= expiry else None)
            expired = expiry_cutoff is not None
            # A frozen report is immutable in its counters, even if members
            # register afterwards or an administrator forces a refresh.
            frozen = record.get('frozen_stats') if isinstance(record, dict) else None
            valid_keys = ('total', 'success', 'already', 'accepted_unknown', 'failed', 'pending')
            if expired and isinstance(frozen, dict) and all(
                isinstance(frozen.get(k), int) and frozen[k] >= 0 for k in valid_keys
            ):
                stats = {k: frozen[k] for k in valid_keys}
            else:
                stats = await self.report_store.summary(
                    code, registered_before=expiry_cutoff if expired else None
                )
                if expired:
                    # Persist before posting, so retries or restarts cannot
                    # alter the counters while an HTTP request is in flight.
                    record = dict(record) if isinstance(record, dict) else {}
                    record['frozen_stats'] = {k: stats[k] for k in valid_keys}
                    record['frozen_at'] = now.isoformat()
                    saved[key] = record
                    self.report_store.save_messages(saved)
            if stats['total'] == 0:
                return "No eligible player records for this gift code."
            channel = self.get_channel(self.report_channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(self.report_channel_id)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                    logger.warning("Cannot access DMD report channel: %s", type(exc).__name__)
                    return "Cannot access the configured report channel."
            if not hasattr(channel, 'send') or not hasattr(channel, 'fetch_message'):
                return "Report channel must be a text channel."
            signature = "/".join(str(stats[key]) for key in valid_keys)
            expiry_signature = ('expired' if expired else
                                'active' if expiry is not None else 'unknown')
            ment = load_report_ment()
            ment_signature = hashlib.sha256(ment.encode("utf-8")).hexdigest()
            if (not force and record.get('signature') == signature
                    and record.get('expiry_signature') == expiry_signature
                    and record.get('ment_signature') == ment_signature):
                return "Report unchanged."

            finished = stats['total'] - stats['pending']
            # Display redemption progress independently of unknown expiry dates.
            if expired:
                label = "🔴 만료"
            elif stats['pending'] == 0 and stats['failed'] == 0:
                label = "✅ 처리 완료"
            else:
                label = "🟢 진행 중"

            # Don't show an update frequency on archived expired reports.
            progress = (f"왕국 1850 · 최종 처리 {finished}/{stats['total']}명" if expired
                        else f"왕국 1850 · 진행 {finished}/{stats['total']}명")
            if not expired:
                progress += " · 5분마다 확인"

            embed = discord.Embed(
                title=f"🍅 DMD Gift Code — {code}",
                description=f"{progress}\n\n**상태: {label}**",
                color=(discord.Color.dark_grey() if expired
                       else discord.Color.green() if stats['pending'] == 0 and stats['failed'] == 0
                       else discord.Color.orange()),
            )
            embed.add_field(name="✅ 성공 응답", value=str(stats['success']), inline=True)
            embed.add_field(name="🔁 이미 수령", value=str(stats['already']), inline=True)
            embed.add_field(name="❌ 실패/재시도", value=str(stats['failed']), inline=True)
            embed.add_field(name="⏳ 미확인", value=str(stats['pending']), inline=True)
            if stats['accepted_unknown']:
                embed.add_field(name="📋 결과 분류 불명", value=str(stats['accepted_unknown']), inline=True)
            embed.set_footer(
                text="봇의 API 결과 기준 · 실제 우편함 지급 여부는 게임 내에서 확인해 주세요.\n"
                     + ment
            )

            message = None
            if record.get('message_id'):
                try:
                    message = await channel.fetch_message(int(record['message_id']))
                    await message.edit(embed=embed)
                except discord.NotFound:
                    message = None
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning("Report update failed: %s", type(exc).__name__)
                    return "Cannot edit existing report message."
            if message is None:
                try:
                    message = await channel.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning("Report send failed: %s", type(exc).__name__)
                    return "Cannot send to report channel."
            saved[key] = {
                **(record if isinstance(record, dict) else {}),
                'message_id': message.id,
                'signature': signature,
                'expiry_signature': expiry_signature,
                'ment_signature': ment_signature,
            }
            self.report_store.save_messages(saved)
            return f"Report posted/updated for {code}."

    def wake_auto_redeem(self, reason: str) -> None:

        if not CONFIG["auto_redeem"].get("enabled", True):

            return



        logger.info("Auto redeem wake requested: %s", reason)

        task = asyncio.create_task(self.run_redeem_checked())

        task.add_done_callback(self._log_auto_redeem_wake_result)



    @staticmethod

    def _log_auto_redeem_wake_result(task: asyncio.Task[dict[str, int]]) -> None:

        try:

            stats = task.result()

        except Exception:

            logger.exception("Auto redeem wake failed")

            return



        logger.info(

            "Auto redeem wake finished: success=%s already=%s rate_limited=%s expired=%s max_uses=%s failed=%s skipped=%s",

            stats.get("success", 0),

            stats.get("already", 0),

            stats.get("rate_limited", 0),

            stats.get("expired", 0),

            stats.get("max_uses", 0),

            stats.get("failed", 0),

            stats.get("skipped", 0),

        )



    async def on_message(self, message: discord.Message) -> None:

        if message.author.bot:

            return

        if message.channel.id not in self.delete_message_channel_ids:

            return

        if message.type is not discord.MessageType.default:

            return



        try:

            await message.delete()

        except discord.NotFound:

            return

        except discord.Forbidden:

            logger.warning(

                "Missing Manage Messages permission in channel %s; cannot auto-delete messages.",

                message.channel.id,

            )

        except discord.HTTPException as exc:

            logger.warning(

                "Failed to auto-delete message %s in channel %s: %s",

                message.id,

                message.channel.id,

                exc,

            )





bot = KingshotBot()





def _owned_player_or_message(discord_user_id: int, player_id: str, player: dict | None) -> str | None:

    if player is None:

        return "That player ID is not registered."

    if str(player.get("discordUserId")) != str(discord_user_id):

        return "You can only manage player IDs registered by your Discord account."

    return None





def _is_admin(discord_user_id: int) -> bool:

    admin_ids = CONFIG.get("admins", {}).get("user_ids", [])

    return str(discord_user_id) in {str(admin_id) for admin_id in admin_ids}





def _chunks(items: list[dict], size: int) -> list[list[dict]]:

    return [items[index : index + size] for index in range(0, len(items), size)]





@bot.tree.command(name="register", description="Register a Kingshot player ID to your Discord account.")

@app_commands.describe(player_id="Kingshot player ID")

@app_commands.checks.cooldown(1, CONFIG["limits"]["register_cooldown_seconds"], key=lambda i: i.user.id)

async def register(interaction: discord.Interaction, player_id: str) -> None:

    await interaction.response.defer(ephemeral=True, thinking=True)



    player_id = player_id.strip()

    if not player_id.isdigit():

        await interaction.followup.send("Player ID must contain numbers only.", ephemeral=True)

        return



    result = await bot.repository.can_register(

        discord_user_id=str(interaction.user.id),

        player_id=player_id,

        max_players=CONFIG["limits"]["max_players_per_user"],

    )

    if not result.allowed:

        await interaction.followup.send(result.reason, ephemeral=True)

        return



    # The old /api/player-info endpoint was removed. UID is saved without lookup.
    # Its actual eligibility and kingdom are verified by redemption responses.
    info = {"playerId": player_id, "name": f"UID {player_id}", "kingdom": 1850}
    record = await bot.repository.register_player(str(interaction.user.id), info)

    await bot.repository.log_event(

        "player_registered",

        discordUserId=str(interaction.user.id),

        playerId=record["playerId"],

        playerName=record["playerName"],

    )

    bot.wake_auto_redeem(f"player registered: {record['playerId']}")



    await interaction.followup.send(

        content="UID saved for Kingdom 1850 (not yet verified). Auto redeem is enabled; actual eligibility is checked on redemption.",

        embed=build_player_embed(record, title_prefix="Registered"),

        ephemeral=True,

    )





def _parse_bulk_ids(raw: str) -> tuple[list[str], list[str], str | None]:
    """Accept CSV, spaces or newlines; enforce reasonable input size and dedupe."""
    if len(raw) > 1800:
        return [], [], "Too much text. Submit at most 1,800 characters per command."
    tokens = [item for item in re.split(r"[\s,;]+", raw.strip()) if item]
    if not tokens:
        return [], [], "Please provide at least one Kingshot UID."
    if len(tokens) > 50:
        return [], [], "Maximum 50 IDs per command. Split the list into batches."
    unique = list(dict.fromkeys(tokens))
    valid = [item for item in unique if item.isascii() and item.isdigit() and 5 <= len(item) <= 15]
    invalid = [item for item in unique if item not in valid]
    return valid, invalid, None


def _compact_ids(ids: list[str]) -> str:
    shown = ", ".join(ids[:10])
    return f"{shown}{' …' if len(ids) > 10 else ''}" if ids else "—"


@bot.tree.command(name="bulkregister", description="DMD admins: register up to 50 consenting 1850 UIDs.")
@app_commands.describe(ids="Comma, space, or newline separated Kingshot player UIDs (up to 50)")
@app_commands.checks.cooldown(1, CONFIG["limits"]["command_cooldown_seconds"], key=lambda i: i.user.id)
async def bulk_register(interaction: discord.Interaction, ids: str) -> None:
    if not _is_admin(interaction.user.id):
        await interaction.response.send_message("DMD admins only.", ephemeral=True)
        return
    valid, invalid, error = _parse_bulk_ids(ids)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return
    if not valid:
        await interaction.response.send_message("No valid UIDs (5–15 digits) were provided.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await bot.repository.bulk_register(str(interaction.user.id), valid, kingdom=1850)
    await bot.repository.log_event(
        "bulk_players_registered", discordUserId=str(interaction.user.id),
        added=result["added"], existing=result["existing"], invalid=invalid,
    )
    if result["added"]:
        bot.wake_auto_redeem(f"admin bulk registered {len(result['added'])} player(s)")
    await interaction.followup.send(
        f"Kingdom 1850 — Added **{len(result['added'])}**, already registered **{len(result['existing'])}**, invalid **{len(invalid)}**.\n"
        f"Added: {_compact_ids(result['added'])}\nAlready: {_compact_ids(result['existing'])}\nInvalid: {_compact_ids(invalid)}\n"
        "UIDs are stored but not verified until an actual redeem response.",
        ephemeral=True,
    )


@bot.tree.command(name="unregister", description="DMD admins: withdraw one UID without deleting redemption history.")
@app_commands.describe(uid="Kingshot UID to withdraw")
@app_commands.checks.cooldown(1, CONFIG["limits"]["command_cooldown_seconds"], key=lambda i: i.user.id)
async def unregister(interaction: discord.Interaction, uid: str) -> None:
    if not _is_admin(interaction.user.id):
        await interaction.response.send_message("DMD admins only.", ephemeral=True)
        return
    uid = uid.strip()
    if not (uid.isascii() and uid.isdigit() and 5 <= len(uid) <= 15):
        await interaction.response.send_message("UID must contain 5–15 ASCII digits.", ephemeral=True)
        return
    status = await bot.repository.set_withdrawn(uid, True)
    if status == "not_found":
        await interaction.response.send_message(f"UID `{uid}` is not registered.", ephemeral=True)
        return
    if status == "changed":
        await bot.repository.log_event("admin_player_withdrawn", playerId=uid, adminDiscordUserId=str(interaction.user.id))
    await interaction.response.send_message(
        f"🍅 UID `{uid}`: " + ("withdrawn. Auto redeem OFF; history preserved." if status == "changed" else "already withdrawn."),
        ephemeral=True,
    )


@bot.tree.command(name="reactivate", description="DMD admins: reactivate a withdrawn UID and preserve history.")
@app_commands.describe(uid="Previously withdrawn Kingshot UID")
@app_commands.checks.cooldown(1, CONFIG["limits"]["command_cooldown_seconds"], key=lambda i: i.user.id)
async def reactivate(interaction: discord.Interaction, uid: str) -> None:
    if not _is_admin(interaction.user.id):
        await interaction.response.send_message("DMD admins only.", ephemeral=True)
        return
    uid = uid.strip()
    if not (uid.isascii() and uid.isdigit() and 5 <= len(uid) <= 15):
        await interaction.response.send_message("UID must contain 5–15 ASCII digits.", ephemeral=True)
        return
    status = await bot.repository.set_withdrawn(uid, False)
    if status == "not_found":
        await interaction.response.send_message(f"UID `{uid}` is not registered.", ephemeral=True)
        return
    if status == "changed":
        await bot.repository.log_event("admin_player_reactivated", playerId=uid, adminDiscordUserId=str(interaction.user.id))
        bot.wake_auto_redeem(f"admin reactivated player {uid}")
    await interaction.response.send_message(
        f"🍅 UID `{uid}`: " + ("reactivated. Auto redeem ON; history preserved." if status == "changed" else "not withdrawn; no change."),
        ephemeral=True,
    )


@bot.tree.command(name="bulkremove", description="DMD admins: remove up to 50 player UIDs, including other users' records.")
@app_commands.describe(ids="Comma, space, or newline separated UIDs", confirm="Must be True to permanently remove accounts and redemption history")
@app_commands.checks.cooldown(1, CONFIG["limits"]["command_cooldown_seconds"], key=lambda i: i.user.id)
async def bulk_remove(interaction: discord.Interaction, ids: str, confirm: bool = False) -> None:
    if not _is_admin(interaction.user.id):
        await interaction.response.send_message("DMD admins only.", ephemeral=True)
        return
    valid, invalid, error = _parse_bulk_ids(ids)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return
    if not valid:
        await interaction.response.send_message("No valid UIDs were provided.", ephemeral=True)
        return
    if not confirm:
        await interaction.response.send_message(
            f"You entered **{len(valid)}** UID(s). To delete their registration AND redeem history, rerun with `confirm:True`. "
            "Back up `Discord-Bot/data` first.", ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await bot.repository.bulk_remove(valid)
    await bot.repository.log_event(
        "bulk_players_removed", discordUserId=str(interaction.user.id),
        removed=result["removed"], missing=result["missing"], invalid=invalid,
    )
    await interaction.followup.send(
        f"Removed **{len(result['removed'])}**, not registered **{len(result['missing'])}**, invalid **{len(invalid)}**.\n"
        f"Removed: {_compact_ids(result['removed'])}\nNot found: {_compact_ids(result['missing'])}",
        ephemeral=True,
    )


@bot.tree.command(name="redeemcode", description="Admin only: queue a gift code for all registered players.")
@app_commands.describe(code="Kingshot gift code, for example CHALLENGE26")
@app_commands.checks.cooldown(1, CONFIG["limits"]["command_cooldown_seconds"], key=lambda i: i.user.id)
async def redeem_code(interaction: discord.Interaction, code: str) -> None:
    if not _is_admin(interaction.user.id):
        await interaction.response.send_message("This command is for DMD admins only.", ephemeral=True)
        return

    code = code.strip()
    if not (3 <= len(code) <= 64 and code.isascii() and code.isalnum()):
        await interaction.response.send_message(
            "Invalid gift code. Use 3–64 ASCII letters or digits, without spaces.",
            ephemeral=True,
        )
        return

    saved = await bot.repository.add_manual_code(code)
    if not saved:
        await interaction.response.send_message(
            f"Code `{code}` is marked expired or exhausted in the local cache. Not queued.",
            ephemeral=True,
        )
        return

    await bot.repository.log_event(
        "manual_code_queued", discordUserId=str(interaction.user.id), code=code
    )
    bot.wake_auto_redeem(f"admin queued code {code}")
    await interaction.response.send_message(
        f"Queued `{code}` for all registered accounts with auto-redeem enabled. "
        "This does not guarantee a successful claim; check the hosting logs.",
        ephemeral=True,
    )


@bot.tree.command(name="setcodeexpiry", description="DMD admins: set a gift code expiry date in UTC.")
@app_commands.describe(code="Gift code", utc="UTC expiry: YYYY-MM-DD HH:MM (24-hour)")
async def set_code_expiry(interaction: discord.Interaction, code: str, utc: str) -> None:
    if not _is_admin(interaction.user.id):
        await interaction.response.send_message("DMD admins only.", ephemeral=True)
        return
    code = code.strip()
    if not (3 <= len(code) <= 64 and code.isascii() and code.isalnum()):
        await interaction.response.send_message("Invalid gift code.", ephemeral=True)
        return
    try:
        expiry = datetime.strptime(utc.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        await interaction.response.send_message("Use UTC format YYYY-MM-DD HH:MM (e.g. 2026-10-20 00:00).", ephemeral=True)
        return
    bot.report_store.set_expiry(code, expiry)
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await bot.publish_redeem_report(code, force=True)
    kst = expiry.astimezone(timezone(timedelta(hours=9)))
    await interaction.followup.send(
        f"{code}: UTC {expiry:%Y-%m-%d %H:%M} / KST {kst:%Y-%m-%d %H:%M}\n{result}",
        ephemeral=True,
    )


@bot.tree.command(name="redeemreport", description="DMD admins: publish or refresh one coupon progress report.")
@app_commands.describe(code="Gift code to report, e.g. CHALLENGE26")
async def redeem_report(interaction: discord.Interaction, code: str) -> None:
    if not _is_admin(interaction.user.id):
        await interaction.response.send_message("DMD admins only.", ephemeral=True)
        return
    code = code.strip()
    if not (3 <= len(code) <= 64 and code.isascii() and code.isalnum()):
        await interaction.response.send_message("Invalid gift code.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await bot.publish_redeem_report(code, force=True)
    await interaction.followup.send(result, ephemeral=True)


@bot.tree.command(name="editment", description="DMD admins: edit the message under every gift-code report.")
@app_commands.describe(ment="New footer text (maximum 300 characters); use 'reset' to restore the default")
@app_commands.checks.cooldown(1, CONFIG["limits"]["command_cooldown_seconds"], key=lambda i: i.user.id)
async def edit_ment(interaction: discord.Interaction, ment: str) -> None:
    if not _is_admin(interaction.user.id):
        await interaction.response.send_message("DMD admins only.", ephemeral=True)
        return
    new_ment = DEFAULT_REPORT_MENT if ment.strip().lower() == "reset" else ment.strip()
    if not new_ment or len(new_ment) > 300:
        await interaction.response.send_message("Enter 1–300 characters, or `reset`.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        save_report_ment(new_ment)
    except OSError:
        logger.exception("Saving report ment failed")
        await interaction.followup.send("Could not save the message. Check the data directory permissions.", ephemeral=True)
        return

    # Refresh only existing report messages, including archived expired reports.
    # Do not create new coupon reports merely because the footer changed.
    refreshed = 0
    unsuccessful = 0
    if bot.report_channel_id:
        saved = bot.report_store.load_messages()
        prefix = f"{bot.report_channel_id}:"
        codes = [key[len(prefix):] for key in saved if key.startswith(prefix)]
        for code in codes:
            try:
                result = await bot.publish_redeem_report(code, force=True)
                if result.startswith("Report posted/updated"):
                    refreshed += 1
                else:
                    unsuccessful += 1
                    logger.warning("Footer refresh skipped for %s: %s", code, result)
            except Exception:
                unsuccessful += 1
                logger.exception("Footer refresh failed for coupon %s", code)
    # Ensure the next scheduled cycle does not skip any changed records.
    bot._report_last_snapshot = None
    await interaction.followup.send(
        f"✅ 리포트 하단 문구를 저장했어요!\n"
        f"새 문구: {new_ment}\n"
        f"기존 리포트 갱신: {refreshed}개 / 갱신 실패·보류: {unsuccessful}개\n"
        "일부가 갱신되지 않았다면 `/redeemreport`로 다시 갱신할 수 있어요.",
        ephemeral=True,
    )


@bot.tree.command(name="healthcheck", description="DMD admins: show or update the bot health report.")
async def healthcheck(interaction: discord.Interaction) -> None:
    if not _is_admin(interaction.user.id):
        await interaction.response.send_message("DMD admins only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await bot.publish_health(force=True)
    await interaction.followup.send(result, ephemeral=True)


@bot.tree.command(name="list", description="List your registered Kingshot players.")

@app_commands.checks.cooldown(1, CONFIG["limits"]["command_cooldown_seconds"], key=lambda i: i.user.id)

async def list_players(interaction: discord.Interaction) -> None:

    players = await bot.repository.list_players_for_user(str(interaction.user.id))

    if not players:

        await interaction.response.send_message("You do not have registered players yet.", ephemeral=True)

        return



    await interaction.response.send_message(

        content=f"Your registered players: `{len(players)}`",

        embeds=build_player_embeds(players, mode="compact"),

        ephemeral=True,

    )





@bot.tree.command(name="listallplayers", description="Admin only: list every registered Kingshot player.")

@app_commands.checks.cooldown(1, CONFIG["limits"]["command_cooldown_seconds"], key=lambda i: i.user.id)

async def list_all_players(interaction: discord.Interaction) -> None:

    if not _is_admin(interaction.user.id):

        await interaction.response.send_message("You are not allowed to use this command.", ephemeral=True)

        return



    await interaction.response.defer(ephemeral=True, thinking=True)

    players = await bot.repository.list_all_players()

    if not players:

        await interaction.followup.send("No players are registered yet.", ephemeral=True)

        return



    pages = _chunks(players, 10)

    for page_number, page in enumerate(pages, start=1):

        await interaction.followup.send(

            content=f"All registered players: `{len(players)}` | page `{page_number}/{len(pages)}`",

            embeds=build_player_embeds(page, mode="compact"),

            ephemeral=True,

        )





@bot.tree.command(name="remove", description="Remove one of your registered Kingshot players.")

@app_commands.describe(player_id="Kingshot player ID")

@app_commands.checks.cooldown(1, CONFIG["limits"]["command_cooldown_seconds"], key=lambda i: i.user.id)

async def remove(interaction: discord.Interaction, player_id: str) -> None:

    player = await bot.repository.get_player(player_id)

    message = _owned_player_or_message(interaction.user.id, player_id, player)

    if message:

        await interaction.response.send_message(message, ephemeral=True)

        return



    await bot.repository.remove_player(str(interaction.user.id), player_id)

    await bot.repository.log_event("player_removed", discordUserId=str(interaction.user.id), playerId=player_id)

    await interaction.response.send_message(f"Removed player `{player_id}`.", ephemeral=True)





@bot.tree.command(name="stopautoredeem", description="Disable automatic redeeming for one of your players.")

@app_commands.describe(player_id="Kingshot player ID")

@app_commands.checks.cooldown(1, CONFIG["limits"]["command_cooldown_seconds"], key=lambda i: i.user.id)

async def stop_auto_redeem(interaction: discord.Interaction, player_id: str) -> None:

    await _set_auto_redeem(interaction, player_id, False)





@bot.tree.command(name="startautoredeem", description="Enable automatic redeeming for one of your players.")

@app_commands.describe(player_id="Kingshot player ID")

@app_commands.checks.cooldown(1, CONFIG["limits"]["command_cooldown_seconds"], key=lambda i: i.user.id)

async def start_auto_redeem(interaction: discord.Interaction, player_id: str) -> None:

    await _set_auto_redeem(interaction, player_id, True)





async def _set_auto_redeem(interaction: discord.Interaction, player_id: str, enabled: bool) -> None:

    player = await bot.repository.get_player(player_id)

    message = _owned_player_or_message(interaction.user.id, player_id, player)

    if message:

        await interaction.response.send_message(message, ephemeral=True)

        return



    if enabled and player.get("withdrawn", False):
        await interaction.response.send_message("This UID was withdrawn. Ask an admin to use /reactivate.", ephemeral=True)
        return

    await bot.repository.set_auto_redeem(player_id, enabled)

    await bot.repository.log_event(

        "auto_redeem_updated",

        discordUserId=str(interaction.user.id),

        playerId=player_id,

        enabled=enabled,

    )

    state = "enabled" if enabled else "disabled"

    await interaction.response.send_message(f"Auto redeem {state} for `{player_id}`.", ephemeral=True)





@bot.tree.command(name="status", description="Show private auto redeem status for one of your players.")

@app_commands.describe(player_id="Kingshot player ID")

@app_commands.checks.cooldown(1, CONFIG["limits"]["command_cooldown_seconds"], key=lambda i: i.user.id)

async def status(interaction: discord.Interaction, player_id: str) -> None:

    player = await bot.repository.get_player(player_id)

    message = _owned_player_or_message(interaction.user.id, player_id, player)

    if message:

        await interaction.response.send_message(message, ephemeral=True)

        return



    await interaction.response.send_message(

        embed=build_player_embed(player, mode="expanded", title_prefix="Status"),

        ephemeral=True,

    )





@healthcheck.error
@register.error
@redeem_code.error
@redeem_report.error
@edit_ment.error
@set_code_expiry.error
@unregister.error
@reactivate.error
@bulk_register.error
@bulk_remove.error

@list_players.error

@list_all_players.error

@remove.error

@stop_auto_redeem.error

@start_auto_redeem.error

@status.error

async def cooldown_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:

    if isinstance(error, app_commands.CommandOnCooldown):

        await interaction.response.send_message(

            f"Slow down a little. Try again in `{error.retry_after:.0f}s`.",

            ephemeral=True,

        )

        return

    raise error





if __name__ == "__main__":

    token = os.getenv("DISCORD_TOKEN")

    if not token:

        raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and set your Discord bot token.")

    bot.run(token, log_handler=None)
