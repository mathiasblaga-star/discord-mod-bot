import asyncio
import datetime

import discord

from database import add_mute, set_lockdown


async def try_delete(message: discord.Message) -> None:
    try:
        await message.delete()
    except discord.HTTPException:
        pass


async def apply_mute(
    member: discord.Member,
    duration: int,
    reason: str,
    muted_role_id: int = 0,
) -> None:
    try:
        until = discord.utils.utcnow() + datetime.timedelta(seconds=duration)
        await member.timeout(until, reason=reason)
    except discord.HTTPException:
        if muted_role_id:
            role = member.guild.get_role(muted_role_id)
            if role:
                try:
                    await member.add_roles(role, reason=reason)
                except discord.HTTPException:
                    pass
    await add_mute(member.id, member.guild.id, duration)


async def notify_member(
    member: discord.Member,
    action: str,
    reason: str,
    guild_name: str,
) -> None:
    """DM the member with a moderation-action embed. Silent on failure."""
    action_lower = action.lower()
    if "ban" in action_lower or "kick" in action_lower:
        color = discord.Color.red()
    else:
        color = discord.Color.orange()

    embed = discord.Embed(
        title=f"Moderation Action — {guild_name}",
        color=color,
    )
    embed.add_field(name="Action", value=action, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Server", value=guild_name, inline=False)
    embed.set_footer(
        text="If you believe this is an error, contact a server administrator."
    )

    try:
        await member.send(embed=embed)
    except discord.HTTPException:
        pass


async def apply_lockdown(guild: discord.Guild, on: bool, reason: str) -> int:
    await set_lockdown(guild.id, on)

    async def _set(ch: discord.TextChannel) -> bool:
        try:
            ow = ch.overwrites_for(guild.default_role)
            ow.send_messages = False if on else None
            await ch.set_permissions(guild.default_role, overwrite=ow, reason=reason)
            return True
        except discord.HTTPException:
            return False

    results = await asyncio.gather(*(_set(ch) for ch in guild.text_channels), return_exceptions=True)
    return sum(1 for r in results if r is True)
