import re
from dataclasses import dataclass
from typing import Literal

import discord


Mode = Literal["compact", "expanded"]

DEFAULT_AVATAR_URL = "https://got-global-avatar.akamaized.net/avatar-dev/2023/07/17/1020.png"
CARD_COLOR = discord.Color.from_rgb(29, 185, 84)
TG_BADGE_PATTERN = re.compile(r"\bTG\s*([1-8])\b", re.IGNORECASE)
DEFAULT_ALLIANCE_NAMES = {0: "DMD", 1: "MVP", 2: "HEX", 3: "OTHER"}


@dataclass(frozen=True)
class LevelDisplay:
    label: str
    is_tg: bool = False


def parse_level_display(player: dict) -> LevelDisplay:
    # Retained for compatibility with any callers outside these player cards.
    rendered = str(
        player.get("levelRenderedDetailed")
        or player.get("levelRendered")
        or player.get("level")
        or ""
    ).strip()
    match = TG_BADGE_PATTERN.search(rendered)
    if match:
        return LevelDisplay(label=f"TG{match.group(1)}", is_tg=True)

    level = _safe_int(player.get("level"))
    if level is None:
        level = _safe_int(player.get("levelRendered"))
    if level is not None and 1 <= level <= 19:
        return LevelDisplay(label=f"TC {level}")
    if rendered:
        return LevelDisplay(label=f"TC {rendered}" if rendered.isdigit() else rendered)
    return LevelDisplay(label="Unknown")


def _alliance_for_player(player: dict, alliance_names: dict[int, str] | None) -> tuple[int, str]:
    raw_id = player.get("allianceId")
    try:
        alliance_id = int(raw_id) if raw_id is not None else 3
    except (TypeError, ValueError):
        alliance_id = 3
    if alliance_id not in DEFAULT_ALLIANCE_NAMES:
        alliance_id = 3
    names = alliance_names if alliance_names is not None else DEFAULT_ALLIANCE_NAMES
    name = str(names.get(alliance_id, DEFAULT_ALLIANCE_NAMES[alliance_id]))
    return alliance_id, name


def build_player_embed(
    player: dict,
    *,
    mode: Mode = "compact",
    title_prefix: str | None = None,
    alliance_names: dict[int, str] | None = None,
) -> discord.Embed:
    player_id = str(player.get("playerId", "?"))
    name = player.get("playerName") or player.get("name") or "Unknown Player"
    kingdom = player.get("kingdom", "?")
    auto_state = "On" if player.get("autoRedeem", True) else "Off"
    avatar_url = player.get("profilePhoto") or DEFAULT_AVATAR_URL
    alliance_id, alliance_name = _alliance_for_player(player, alliance_names)

    title = f"[{alliance_name}] {name} (#{player_id})"
    if alliance_id == 0:
        title += " 🍅"
    if title_prefix:
        title = f"{title_prefix} {title}"

    embed = discord.Embed(
        title=title,
        description=f":crossed_swords: K{kingdom}",
        color=CARD_COLOR if player.get("autoRedeem", True) else discord.Color.dark_grey(),
    )
    embed.set_thumbnail(url=avatar_url)
    embed.set_footer(text=f"Auto redeem: {auto_state}")

    if mode == "expanded":
        redeemed = player.get("redeemedCodes", [])
        last = redeemed[-1] if redeemed else {}
        embed.add_field(name="Player ID", value=f"`{player_id}`", inline=True)
        embed.add_field(name="Kingdom", value=f"`K{kingdom}`", inline=True)
        embed.add_field(name="Alliance", value=f"`{alliance_name}`", inline=True)
        embed.add_field(name="Auto Redeem", value=f"`{auto_state}`", inline=True)
        embed.add_field(name="Redeemed Codes", value=f"`{len(redeemed)}`", inline=True)
        embed.add_field(name="Registered", value=f"`{player.get('registeredAt', 'Unknown')}`", inline=False)
        embed.add_field(name="Last Code", value=f"`{last.get('code', 'None')}`", inline=True)
        embed.add_field(name="Last Redeem", value=f"`{last.get('redeemedAt', 'None')}`", inline=True)

    return embed


def build_player_embeds(
    players: list[dict], *, mode: Mode = "compact", alliance_names: dict[int, str] | None = None
) -> list[discord.Embed]:
    return [build_player_embed(player, mode=mode, alliance_names=alliance_names) for player in players[:10]]


def _safe_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
