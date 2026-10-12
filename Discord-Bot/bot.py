import asyncio

import logging

import os
import re

from pathlib import Path



import discord

from discord import app_commands

from discord.ext import tasks

from dotenv import load_dotenv



from kingshot.api import KingshotApi

from kingshot.config import load_config

from kingshot.redeemer import AutoRedeemer

from kingshot.repository import JsonRepository

from kingshot.ui import build_player_embed, build_player_embeds





ROOT = Path(__file__).resolve().parent

CONFIG = load_config(ROOT / "config.json")



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




    async def close(self) -> None:

        self.auto_redeem_loop.cancel()

        await self.api.close()

        await super().close()



    @tasks.loop(seconds=300)

    async def auto_redeem_loop(self) -> None:

        await self.wait_until_ready()

        await self.redeemer.run_once()



    def wake_auto_redeem(self, reason: str) -> None:

        if not CONFIG["auto_redeem"].get("enabled", True):

            return



        logger.info("Auto redeem wake requested: %s", reason)

        task = asyncio.create_task(self.redeemer.run_once())

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





@register.error
@redeem_code.error
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
