import time
import datetime
from collections import defaultdict, deque

import discord
from discord.ext import commands, tasks

from database import (
    log_infraction, get_guild_config,
    add_neutralised, is_neutralised, cleanup_neutralised,
)
from utils.actions import apply_lockdown, notify_member

_ACTIONS = {
    discord.AuditLogAction.channel_delete: ("channel_delete", "nuke_channel_delete_limit"),
    discord.AuditLogAction.channel_create: ("channel_create", "nuke_channel_create_limit"),
    discord.AuditLogAction.role_create:    ("role_create",    "nuke_role_create_limit"),
    discord.AuditLogAction.ban:            ("ban",            "nuke_ban_limit"),
}


class NukeProtectionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._events: dict = defaultdict(lambda: defaultdict(lambda: deque(maxlen=50)))
        self.cleanup_neutralised_task.start()

    def cog_unload(self):
        self.cleanup_neutralised_task.cancel()

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        actor = entry.user
        if not actor or actor.bot:
            return
        if self.bot.user and actor.id == self.bot.user.id:
            return

        mapping = _ACTIONS.get(entry.action)
        if not mapping:
            return
        kind, limit_key = mapping

        cfg = await get_guild_config(entry.guild.id)
        limit = cfg[limit_key]
        window = cfg["nuke_time_window"]

        now = time.time()
        bucket = self._events[actor.id][kind]
        bucket.append(now)
        recent = [t for t in bucket if now - t <= window]

        if len(recent) >= limit and not await is_neutralised(actor.id, entry.guild.id):
            await add_neutralised(actor.id, entry.guild.id)
            await self._neutralise(entry.guild, actor, kind, recent, cfg)

    async def _neutralise(self, guild: discord.Guild, user, kind: str, events: list, cfg: dict):
        member = guild.get_member(user.id)
        actions: list[str] = []

        if member:
            try:
                strippable = [
                    r for r in member.roles
                    if not r.is_default() and r < guild.me.top_role and not r.managed
                ]
                if strippable:
                    await member.remove_roles(*strippable, reason="Nuke attempt — auto role strip")
                actions.append(f"Stripped {len(strippable)} role(s)")
            except discord.HTTPException as e:
                actions.append(f"Role strip failed: {e}")

            try:
                until = discord.utils.utcnow() + datetime.timedelta(days=28)
                await member.timeout(until, reason="Nuke attempt — locked out")
                actions.append("Timed out 28 days")
            except discord.HTTPException as e:
                actions.append(f"Timeout failed: {e}")
        else:
            actions.append("Actor not in member cache — manual review required")

        if member is not None:
            await notify_member(
                member,
                "Timed out (28 days) and roles removed",
                "Automated nuke/raid attempt detected on this server",
                guild.name,
            )

        await log_infraction(
            user.id, guild.id, f"nuke_{kind}", "Severe",
            f"events_in_window={len(events)}",
            "; ".join(actions) or "no automatic action",
        )

        alerts_id = cfg["mod_alerts_channel_id"]
        admin_role_id = cfg["admin_role_id"]
        if alerts_id:
            channel = guild.get_channel(alerts_id)
            if channel:
                ping = f"<@&{admin_role_id}>" if admin_role_id else "@here"
                timeline = "\n".join(time.strftime("%H:%M:%S", time.gmtime(t)) for t in events)
                embed = discord.Embed(
                    title="SERVER NUKE ATTEMPT DETECTED",
                    description=(
                        f"**Actor:** {user} (`{user.id}`)\n"
                        f"**Action type:** `{kind}`\n"
                        f"**Events in {cfg['nuke_time_window']}s window:** {len(events)}\n\n"
                        f"**Timeline (UTC):**\n```\n{timeline}\n```\n"
                        f"**Auto-actions:**\n"
                        + "\n".join(f"- {a}" for a in actions)
                        + "\n\nInitiating lockdown — use `/lockdown off` to lift."
                    ),
                    color=discord.Color.dark_red(),
                    timestamp=discord.utils.utcnow(),
                )
                try:
                    await channel.send(content=ping, embed=embed)
                except discord.HTTPException:
                    pass

        await apply_lockdown(guild, True, reason="Nuke attempt — automatic lockdown")

    @tasks.loop(hours=1)
    async def cleanup_neutralised_task(self):
        await cleanup_neutralised(86400)

    @cleanup_neutralised_task.before_loop
    async def _wait_ready_cleanup(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(NukeProtectionCog(bot))
