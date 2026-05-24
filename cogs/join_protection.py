import time
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import get_guild_config, log_infraction
from utils.embeds import infraction_embed
from cogs.admin import admin_only

RAID_AUTO_LIFT_SECONDS = 300  # 5 minutes of no joins → lift raid mode


class JoinProtectionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._join_times: dict[int, deque] = defaultdict(lambda: deque(maxlen=100))
        self._raid_active: set[int] = set()
        self._original_verification: dict[int, discord.VerificationLevel] = {}
        self.clear_raid_task.start()

    def cog_unload(self):
        self.clear_raid_task.cancel()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        guild = member.guild
        cfg = await get_guild_config(guild.id)

        now = time.time()
        times = self._join_times[guild.id]
        times.append(now)

        window = cfg["join_raid_window"]
        limit = cfg["join_raid_limit"]
        recent_count = sum(1 for t in times if now - t <= window)

        if recent_count >= limit and guild.id not in self._raid_active:
            self._raid_active.add(guild.id)
            await self._activate_raid_mode(guild, recent_count, cfg)

        await self._check_account_age(member, cfg)

    # ---------------- raid mode ------------------------------------------

    async def _activate_raid_mode(self, guild: discord.Guild, count: int, cfg: dict):
        self._original_verification[guild.id] = guild.verification_level

        try:
            await guild.edit(
                verification_level=discord.VerificationLevel.high,
                reason="Raid detected — auto-raising verification level",
            )
        except discord.HTTPException:
            pass

        await log_infraction(
            0, guild.id, "raid_detected", "Severe",
            f"joins_in_window={count}",
            "Raised verification level",
        )

        alerts_id = cfg["mod_alerts_channel_id"]
        admin_role_id = cfg["admin_role_id"]
        if alerts_id:
            channel = guild.get_channel(alerts_id)
            if channel:
                ping = f"<@&{admin_role_id}>" if admin_role_id else None
                embed = discord.Embed(
                    title="RAID DETECTED",
                    description=(
                        f"**Joins in {cfg['join_raid_window']}s window:** {count}\n"
                        f"**Auto-action:** Verification level raised to High\n\n"
                        f"Use `/raid off` to lift raid mode and restore "
                        f"verification level."
                    ),
                    color=discord.Color.dark_red(),
                    timestamp=discord.utils.utcnow(),
                )
                try:
                    await channel.send(content=ping, embed=embed)
                except discord.HTTPException:
                    pass

    async def _deactivate_raid_mode(self, guild: discord.Guild, *, auto: bool):
        self._raid_active.discard(guild.id)
        original = self._original_verification.pop(
            guild.id, discord.VerificationLevel.medium,
        )
        try:
            await guild.edit(
                verification_level=original,
                reason="Raid mode lifted — restoring verification level",
            )
        except discord.HTTPException:
            pass

        cfg = await get_guild_config(guild.id)
        alerts_id = cfg["mod_alerts_channel_id"]
        if alerts_id:
            channel = guild.get_channel(alerts_id)
            if channel:
                title = "Raid mode auto-lifted" if auto else "Raid mode lifted"
                embed = discord.Embed(
                    title=title,
                    description=f"Verification level restored to **{original.name}**.",
                    color=discord.Color.green(),
                    timestamp=discord.utils.utcnow(),
                )
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    pass

    # ---------------- account age gate -----------------------------------

    async def _check_account_age(self, member: discord.Member, cfg: dict | None = None):
        if cfg is None:
            cfg = await get_guild_config(member.guild.id)

        min_days = cfg["min_account_age_days"]
        if min_days == 0:
            return

        age = (discord.utils.utcnow() - member.created_at).days
        if age >= min_days:
            return

        try:
            await member.send(
                f"Your account is too new to join **{member.guild.name}**. "
                f"Accounts must be at least {min_days} days old. "
                f"Please try again later."
            )
        except discord.HTTPException:
            pass

        try:
            await member.kick(
                reason=f"Account too new ({age}d < {min_days}d required)"
            )
        except discord.HTTPException:
            return

        await log_infraction(
            member.id, member.guild.id, "account_age_kick", "Low",
            f"account_age_days={age}",
            f"Kicked (account too new, <{min_days}d)",
        )

        mod_log_id = cfg["mod_log_channel_id"]
        if mod_log_id:
            channel = member.guild.get_channel(mod_log_id)
            if channel:
                embed = infraction_embed(
                    member,
                    "Account Age Kick",
                    "Low",
                    f"Account age: {age} days (minimum: {min_days})",
                    "Kicked (account too new)",
                    discord.Color.yellow(),
                )
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    pass

    # ---------------- slash command --------------------------------------

    @app_commands.command(
        name="raid",
        description="Toggle raid mode (raises server verification level).",
    )
    @app_commands.choices(state=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    @admin_only()
    async def raid(
        self,
        interaction: discord.Interaction,
        state: app_commands.Choice[str],
    ):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        if state.value == "on":
            if guild.id in self._raid_active:
                return await interaction.followup.send(
                    "Raid mode is already active.", ephemeral=True
                )
            self._raid_active.add(guild.id)
            cfg = await get_guild_config(guild.id)
            now = time.time()
            count = sum(
                1 for t in self._join_times.get(guild.id, ())
                if now - t <= cfg["join_raid_window"]
            )
            await self._activate_raid_mode(guild, count, cfg)
            await interaction.followup.send("Raid mode activated.", ephemeral=True)
        else:
            if guild.id not in self._raid_active:
                return await interaction.followup.send(
                    "Raid mode is not active.", ephemeral=True
                )
            await self._deactivate_raid_mode(guild, auto=False)
            await interaction.followup.send("Raid mode lifted.", ephemeral=True)

    # ---------------- background: auto-lift raid mode --------------------

    @tasks.loop(minutes=5)
    async def clear_raid_task(self):
        now = time.time()
        for guild_id in list(self._raid_active):
            times = self._join_times.get(guild_id)
            last = times[-1] if times else 0
            if now - last <= RAID_AUTO_LIFT_SECONDS:
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                self._raid_active.discard(guild_id)
                self._original_verification.pop(guild_id, None)
                continue
            await self._deactivate_raid_mode(guild, auto=True)

    @clear_raid_task.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(JoinProtectionCog(bot))
