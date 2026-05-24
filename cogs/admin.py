import csv
import datetime
import io
import json
import logging
import time
from pathlib import Path

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import DB_PATH
from database import (
    get_infractions, remove_mute, get_expired_mutes, get_guild_config,
    log_infraction, add_temp_ban, get_expired_temp_bans, remove_temp_ban,
    remove_neutralised,
    add_pardon, get_pardons, count_pardons,
    delete_infractions_for, get_all_infractions,
)
from utils.actions import apply_lockdown, apply_mute
from utils.embeds import infraction_embed
from utils.views import UndoBanView


log = logging.getLogger("bot")


async def _do_backup() -> Path:
    """Snapshot the SQLite DB to data/backups/, prune to the 5 most recent."""
    backups_dir = Path(DB_PATH).parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = backups_dir / f"backup_{ts}.db"

    async with aiosqlite.connect(DB_PATH) as src:
        async with aiosqlite.connect(str(backup_path)) as dst:
            await src.backup(dst)

    files = sorted(
        backups_dir.glob("backup_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[5:]:
        try:
            old.unlink()
        except OSError:
            pass

    return backup_path


_REASON_MAX = 512
_DISCORD_USER_ID_MAX = 9_999_999_999_999_999_999
_COOLDOWN_SECONDS = 3.0
_cmd_cooldowns: dict[int, dict[str, float]] = {}


def check_cooldown(user_id: int, cmd: str, seconds: float) -> float | None:
    """Return remaining seconds if still cooling down, else None (and record use)."""
    now = time.time()
    user = _cmd_cooldowns.setdefault(user_id, {})
    last = user.get(cmd, 0.0)
    elapsed = now - last
    if elapsed < seconds:
        return seconds - elapsed
    user[cmd] = now
    return None


def _truncate_reason(reason: str) -> str:
    """Keep the reason at or below Discord's 512-char audit-log limit."""
    if len(reason) > _REASON_MAX:
        return reason[:_REASON_MAX - 3] + "..."
    return reason


async def _enforce_cooldown(interaction: discord.Interaction, cmd: str) -> bool:
    """Returns True if the command should proceed, False if blocked by cooldown."""
    remaining = check_cooldown(interaction.user.id, cmd, _COOLDOWN_SECONDS)
    if remaining is None:
        return True
    await interaction.response.send_message(
        f"Please wait {remaining:.1f}s before using this command again.",
        ephemeral=True,
    )
    return False


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if isinstance(member, discord.Member):
            if member.guild_permissions.administrator:
                return True
            if interaction.guild:
                cfg = await get_guild_config(interaction.guild.id)
                admin_role_id = cfg.get("admin_role_id", 0)
                if admin_role_id and any(r.id == admin_role_id for r in member.roles):
                    return True
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return False
    return app_commands.check(predicate)


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.unmute_task.start()
        self.check_temp_bans_task.start()
        self.auto_backup_task.start()

    def cog_unload(self):
        self.unmute_task.cancel()
        self.check_temp_bans_task.cancel()
        self.auto_backup_task.cancel()

    # ---------- existing commands ------------------------------------------

    @app_commands.command(name="history", description="View a user's moderation history.")
    @app_commands.describe(user="The user to look up")
    @admin_only()
    async def history(self, interaction: discord.Interaction, user: discord.User):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "history"):
            return
        rows = await get_infractions(user.id, interaction.guild.id)
        pardons = await count_pardons(user.id, interaction.guild.id)
        if not rows:
            msg = f"No infractions on record for **{user}**."
            if pardons > 0:
                msg += f"\nThis user has been pardoned {pardons} time(s)."
            return await interaction.response.send_message(msg, ephemeral=True)
        embed = discord.Embed(title=f"Moderation history — {user}", color=discord.Color.blurple())
        for offence, severity, content, action, ts in rows[:20]:
            when = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            preview = (content or "").replace("\n", " ")
            if len(preview) > 200:
                preview = preview[:197] + "..."
            embed.add_field(
                name=f"[{severity}] {offence} — {when}",
                value=f"**Action:** {action}\n**Content:** {preview or '—'}",
                inline=False,
            )
        footer_lines = []
        if len(rows) > 20:
            footer_lines.append(f"Showing 20 of {len(rows)} records.")
        footer_lines.append(f"This user has been pardoned {pardons} time(s).")
        footer_lines.append("Use /pardon to clear this record.")
        embed.set_footer(text="\n".join(footer_lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="unban", description="Unban a user by ID.")
    @app_commands.describe(user_id="The numeric user ID to unban")
    @admin_only()
    async def unban(self, interaction: discord.Interaction, user_id: str):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "unban"):
            return
        try:
            uid = int(user_id)
        except ValueError:
            return await interaction.response.send_message("Invalid user ID.", ephemeral=True)
        if uid <= 0 or uid > _DISCORD_USER_ID_MAX:
            return await interaction.response.send_message(
                "Invalid user ID range.", ephemeral=True,
            )
        try:
            await interaction.guild.unban(
                discord.Object(id=uid),
                reason=f"Unban via /unban by {interaction.user}",
            )
        except discord.NotFound:
            return await interaction.response.send_message(f"User `{uid}` is not banned.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"Failed: {e}", ephemeral=True)
        await remove_temp_ban(uid, interaction.guild.id)
        await interaction.response.send_message(f"Unbanned `{uid}`.", ephemeral=True)

    @app_commands.command(name="unmute", description="Remove a user's mute.")
    @app_commands.describe(member="The member to unmute")
    @admin_only()
    async def unmute(self, interaction: discord.Interaction, member: discord.Member):
        if not await _enforce_cooldown(interaction, "unmute"):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await member.timeout(None, reason=f"Unmute by {interaction.user}")
        except discord.HTTPException:
            pass
        cfg = await get_guild_config(member.guild.id)
        muted_role_id = cfg["muted_role_id"]
        if muted_role_id:
            role = member.guild.get_role(muted_role_id)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason=f"Unmute by {interaction.user}")
                except discord.HTTPException:
                    pass
        await remove_mute(member.id, member.guild.id)
        await interaction.followup.send(f"Unmuted {member.mention}.", ephemeral=True)

    @app_commands.command(name="lockdown", description="Toggle server-wide lockdown.")
    @app_commands.choices(state=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    @admin_only()
    async def lockdown(self, interaction: discord.Interaction, state: app_commands.Choice[str]):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "lockdown"):
            return
        on = state.value == "on"
        await interaction.response.defer(ephemeral=True)
        affected = await apply_lockdown(
            interaction.guild, on,
            reason=f"Lockdown {'on' if on else 'off'} by {interaction.user}",
        )
        await interaction.followup.send(
            f"Lockdown {'enabled' if on else 'lifted'} on {affected} channel(s).",
            ephemeral=True,
        )

    # ---------- manual moderation -----------------------------------------

    @app_commands.command(name="warn", description="Warn a member without other action.")
    @app_commands.describe(member="The member to warn", reason="Reason for the warning")
    @admin_only()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "warn"):
            return
        reason = _truncate_reason(reason)
        await interaction.response.defer(ephemeral=True)
        try:
            await member.send(
                f"You have received a warning in **{interaction.guild.name}**: {reason}"
            )
        except discord.HTTPException:
            pass
        await log_infraction(
            member.id, interaction.guild.id, "warn", "Low", reason, "Warned",
        )
        await self._post_modlog(
            interaction.guild, member, "Warning", "Low", reason,
            f"Warned by {interaction.user}", discord.Color.yellow(),
        )
        await interaction.followup.send(
            f"Warned {member.mention}.", ephemeral=True,
        )

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(member="The member to kick", reason="Reason for the kick")
    @admin_only()
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "kick"):
            return
        reason = _truncate_reason(reason)
        await interaction.response.defer(ephemeral=True)
        try:
            await member.send(
                f"You have been kicked from **{interaction.guild.name}**: {reason}"
            )
        except discord.HTTPException:
            pass
        try:
            await member.kick(reason=reason)
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Failed to kick: {e}", ephemeral=True)
        await log_infraction(
            member.id, interaction.guild.id, "manual_kick", "Medium", reason, "Kicked",
        )
        await self._post_modlog(
            interaction.guild, member, "Manual Kick", "Medium", reason,
            f"Kicked by {interaction.user}", discord.Color.orange(),
        )
        await interaction.followup.send(f"Kicked {member.mention}.", ephemeral=True)

    @app_commands.command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(
        member="The member to ban",
        reason="Reason for the ban",
        delete_days="Days of message history to delete (0–7)",
    )
    @admin_only()
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str,
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "ban"):
            return
        reason = _truncate_reason(reason)
        await interaction.response.defer(ephemeral=True)
        try:
            await member.send(
                f"You have been banned from **{interaction.guild.name}**: {reason}"
            )
        except discord.HTTPException:
            pass
        try:
            await interaction.guild.ban(
                member, reason=reason, delete_message_days=delete_days,
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Failed to ban: {e}", ephemeral=True)
        await log_infraction(
            member.id, interaction.guild.id, "manual_ban", "Severe", reason, "Banned",
        )
        await self._post_modlog(
            interaction.guild, member, "Manual Ban", "Severe", reason,
            f"Banned by {interaction.user}", discord.Color.dark_red(),
            view=UndoBanView(member.id),
        )
        await interaction.followup.send(f"Banned {member.mention}.", ephemeral=True)

    @app_commands.command(name="tempban", description="Temporarily ban a member.")
    @app_commands.describe(
        member="The member to ban",
        duration_minutes="Ban duration in minutes",
        reason="Reason for the ban",
    )
    @admin_only()
    async def tempban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration_minutes: app_commands.Range[int, 1, 525600],
        reason: str,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "tempban"):
            return
        reason = _truncate_reason(reason)
        await interaction.response.defer(ephemeral=True)
        try:
            await member.send(
                f"You have been temporarily banned from **{interaction.guild.name}** "
                f"for {duration_minutes} minutes: {reason}"
            )
        except discord.HTTPException:
            pass
        try:
            await interaction.guild.ban(
                member, reason=f"Temp ban ({duration_minutes}m): {reason}",
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Failed to ban: {e}", ephemeral=True)
        unban_at = int(time.time()) + duration_minutes * 60
        await add_temp_ban(member.id, interaction.guild.id, unban_at)
        action = f"Banned by {interaction.user} for {duration_minutes}m"
        await log_infraction(
            member.id, interaction.guild.id, "temp_ban", "Severe", reason, action,
        )
        await self._post_modlog(
            interaction.guild, member, "Temporary Ban", "Severe", reason,
            action, discord.Color.dark_red(),
        )
        await interaction.followup.send(
            f"Banned {member.mention} for {duration_minutes} minutes.",
            ephemeral=True,
        )

    @app_commands.command(name="mute", description="Mute a member for a duration.")
    @app_commands.describe(
        member="The member to mute",
        duration_minutes="Mute duration in minutes",
        reason="Reason for the mute",
    )
    @admin_only()
    async def mute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration_minutes: app_commands.Range[int, 1, 40320],
        reason: str,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "mute"):
            return
        reason = _truncate_reason(reason)
        await interaction.response.defer(ephemeral=True)
        cfg = await get_guild_config(interaction.guild.id)
        await apply_mute(
            member, duration_minutes * 60, reason,
            muted_role_id=cfg["muted_role_id"],
        )
        try:
            await member.send(
                f"You have been muted in **{interaction.guild.name}** "
                f"for {duration_minutes} minutes: {reason}"
            )
        except discord.HTTPException:
            pass
        action = f"Muted by {interaction.user} for {duration_minutes}m"
        await log_infraction(
            member.id, interaction.guild.id, "manual_mute", "Low", reason, action,
        )
        await self._post_modlog(
            interaction.guild, member, "Manual Mute", "Low", reason,
            action, discord.Color.yellow(),
        )
        await interaction.followup.send(
            f"Muted {member.mention} for {duration_minutes} minutes.",
            ephemeral=True,
        )

    @app_commands.command(name="purge", description="Bulk-delete recent messages.")
    @app_commands.describe(
        count="Number of messages to delete (1–100)",
        member="Only delete messages from this member (optional)",
    )
    @admin_only()
    async def purge(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 100],
        member: discord.Member = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "purge"):
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "Purge only works in text channels.", ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)

        if member:
            n = 0

            def _check(m: discord.Message) -> bool:
                nonlocal n
                if m.author.id == member.id and n < count:
                    n += 1
                    return True
                return False

            try:
                deleted = await channel.purge(limit=min(count * 3, 200), check=_check)
            except discord.HTTPException:
                deleted = []
        else:
            try:
                deleted = await channel.purge(limit=count)
            except discord.HTTPException:
                deleted = []

        cfg = await get_guild_config(interaction.guild.id)
        mod_log_id = cfg["mod_log_channel_id"]
        if mod_log_id:
            log_channel = interaction.guild.get_channel(mod_log_id)
            if log_channel:
                target = f" from {member.mention}" if member else ""
                embed = discord.Embed(
                    title="Purge",
                    description=(
                        f"{len(deleted)} message(s) deleted in {channel.mention}{target} "
                        f"by {interaction.user.mention}"
                    ),
                    color=discord.Color.blurple(),
                    timestamp=discord.utils.utcnow(),
                )
                try:
                    await log_channel.send(embed=embed)
                except discord.HTTPException:
                    pass

        await interaction.followup.send(
            f"Deleted {len(deleted)} message(s).", ephemeral=True,
        )

    @app_commands.command(name="slowmode", description="Set slowmode in the current channel.")
    @app_commands.describe(seconds="Slowmode delay in seconds (0–21600)")
    @admin_only()
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
    ):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "slowmode"):
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "Slowmode only works in text channels.", ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        try:
            await channel.edit(
                slowmode_delay=seconds,
                reason=f"Slowmode set by {interaction.user}",
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(
                f"Failed: {e}", ephemeral=True,
            )

        cfg = await get_guild_config(interaction.guild.id)
        mod_log_id = cfg["mod_log_channel_id"]
        if mod_log_id:
            log_channel = interaction.guild.get_channel(mod_log_id)
            if log_channel:
                embed = discord.Embed(
                    title="Slowmode changed",
                    description=f"{channel.mention} → {seconds}s by {interaction.user.mention}",
                    color=discord.Color.blurple(),
                    timestamp=discord.utils.utcnow(),
                )
                try:
                    await log_channel.send(embed=embed)
                except discord.HTTPException:
                    pass

        await interaction.followup.send(
            f"Slowmode set to {seconds} seconds in {channel.mention}.",
            ephemeral=True,
        )

    # ---------- export / backup / pardon ---------------------------------

    @app_commands.command(name="export", description="Export this guild's infractions as CSV or JSON.")
    @app_commands.describe(format="Output format (default: csv)")
    @app_commands.choices(format=[
        app_commands.Choice(name="csv", value="csv"),
        app_commands.Choice(name="json", value="json"),
    ])
    @admin_only()
    async def export(
        self,
        interaction: discord.Interaction,
        format: app_commands.Choice[str] = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "export"):
            return
        fmt = format.value if format else "csv"
        await interaction.response.defer(ephemeral=True)

        rows = await get_all_infractions(interaction.guild.id)
        if not rows:
            return await interaction.followup.send(
                "No infractions on record for this guild.", ephemeral=True,
            )

        date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                "id", "user_id", "guild_id", "offence_type", "severity",
                "content", "action_taken", "timestamp",
            ])
            for id_, uid, gid, otype, sev, content, action, ts in rows:
                iso = datetime.datetime.fromtimestamp(
                    ts, tz=datetime.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                writer.writerow([id_, uid, gid, otype, sev, content or "", action or "", iso])
            data = buf.getvalue().encode("utf-8")
            filename = f"infractions_{interaction.guild.id}_{date_str}.csv"
        else:
            records = []
            for id_, uid, gid, otype, sev, content, action, ts in rows:
                iso = datetime.datetime.fromtimestamp(
                    ts, tz=datetime.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                records.append({
                    "id": id_, "user_id": uid, "guild_id": gid,
                    "offence_type": otype, "severity": sev,
                    "content": content, "action_taken": action,
                    "timestamp": iso,
                })
            data = json.dumps(records, indent=2).encode("utf-8")
            filename = f"infractions_{interaction.guild.id}_{date_str}.json"

        file = discord.File(io.BytesIO(data), filename=filename)
        await interaction.followup.send("Export ready.", file=file, ephemeral=True)

    @app_commands.command(name="backup", description="(Owner) Create + download a full database backup.")
    async def backup(self, interaction: discord.Interaction):
        if not await self.bot.is_owner(interaction.user):
            return await interaction.response.send_message("Owner only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "backup"):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            backup_path = await _do_backup()
        except Exception as e:
            return await interaction.followup.send(
                f"Backup failed: {e}", ephemeral=True,
            )
        try:
            file = discord.File(str(backup_path), filename=backup_path.name)
            await interaction.followup.send("Backup ready.", file=file, ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Backup saved to `{backup_path}` but upload failed: {e}",
                ephemeral=True,
            )

    @app_commands.command(name="pardon", description="Clear all moderation records for a member.")
    @app_commands.describe(member="The member to pardon", reason="Reason for the pardon")
    @admin_only()
    async def pardon(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "pardon"):
            return
        reason = _truncate_reason(reason)
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        deleted = await delete_infractions_for(member.id, guild.id)

        try:
            await member.timeout(None, reason=f"Pardon by {interaction.user}")
        except discord.HTTPException:
            pass
        await remove_mute(member.id, guild.id)
        await remove_temp_ban(member.id, guild.id)
        await remove_neutralised(member.id, guild.id)

        await add_pardon(member.id, guild.id, interaction.user.id, reason)

        try:
            await member.send(
                f"You have been pardoned in **{guild.name}**. "
                f"All moderation records against you have been cleared. "
                f"Reason: {reason}"
            )
        except discord.HTTPException:
            pass

        cfg = await get_guild_config(guild.id)
        mod_log_id = cfg["mod_log_channel_id"]
        if mod_log_id:
            channel = guild.get_channel(mod_log_id)
            if channel:
                embed = discord.Embed(
                    title="Member Pardoned",
                    color=discord.Color.green(),
                    timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="Member", value=f"{member} (`{member.id}`)", inline=False)
                embed.add_field(name="Pardoned By", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
                embed.add_field(name="Reason", value=reason or "—", inline=False)
                embed.add_field(name="Records Cleared", value=str(deleted), inline=False)
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    pass

        await interaction.followup.send(
            f"Pardoned {member.mention}. {deleted} infraction(s) cleared.",
            ephemeral=True,
        )

    @app_commands.command(name="pardonhistory", description="View a member's pardon history.")
    @app_commands.describe(member="The member to look up")
    @admin_only()
    async def pardonhistory(self, interaction: discord.Interaction, member: discord.Member):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not await _enforce_cooldown(interaction, "pardonhistory"):
            return
        rows = await get_pardons(member.id, interaction.guild.id)
        if not rows:
            return await interaction.response.send_message(
                f"No pardons on record for **{member}**.", ephemeral=True,
            )
        embed = discord.Embed(
            title=f"Pardon history — {member}",
            color=discord.Color.green(),
        )
        for pardoned_by, p_reason, ts in rows[:25]:
            when = datetime.datetime.fromtimestamp(
                ts, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
            embed.add_field(
                name=when,
                value=f"**By:** <@{pardoned_by}>\n**Reason:** {p_reason or '—'}",
                inline=False,
            )
        if len(rows) > 25:
            embed.set_footer(text=f"Showing 25 of {len(rows)} records.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- background tasks ------------------------------------------

    @tasks.loop(seconds=30)
    async def unmute_task(self):
        for user_id, guild_id in await get_expired_mutes():
            guild = self.bot.get_guild(guild_id)
            if not guild:
                await remove_mute(user_id, guild_id)
                continue
            member = guild.get_member(user_id)
            if member:
                try:
                    await member.timeout(None, reason="Mute expired")
                except discord.HTTPException:
                    pass
                cfg = await get_guild_config(guild_id)
                muted_role_id = cfg["muted_role_id"]
                if muted_role_id:
                    role = guild.get_role(muted_role_id)
                    if role and role in member.roles:
                        try:
                            await member.remove_roles(role, reason="Mute expired")
                        except discord.HTTPException:
                            pass
            await remove_mute(user_id, guild_id)

    @unmute_task.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=60)
    async def check_temp_bans_task(self):
        for user_id, guild_id in await get_expired_temp_bans():
            guild = self.bot.get_guild(guild_id)
            if not guild:
                await remove_temp_ban(user_id, guild_id)
                continue
            try:
                await guild.unban(
                    discord.Object(id=user_id),
                    reason="Temporary ban expired",
                )
            except discord.HTTPException:
                pass
            await remove_temp_ban(user_id, guild_id)

            cfg = await get_guild_config(guild_id)
            mod_log_id = cfg["mod_log_channel_id"]
            if mod_log_id:
                log_channel = guild.get_channel(mod_log_id)
                if log_channel:
                    embed = discord.Embed(
                        title="Temporary ban expired",
                        description=f"User `{user_id}` has been unbanned (temp ban expired).",
                        color=discord.Color.blurple(),
                        timestamp=discord.utils.utcnow(),
                    )
                    try:
                        await log_channel.send(embed=embed)
                    except discord.HTTPException:
                        pass

    @check_temp_bans_task.before_loop
    async def _wait_ready_tempban(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def auto_backup_task(self):
        try:
            backup_path = await _do_backup()
            log.info("Auto-backup completed: %s", backup_path)
        except Exception:
            log.exception("Auto-backup failed")

    @auto_backup_task.before_loop
    async def _wait_ready_backup(self):
        await self.bot.wait_until_ready()

    # ---------- helpers ---------------------------------------------------

    async def _post_modlog(
        self,
        guild: discord.Guild,
        member,
        offence: str,
        severity: str,
        content: str,
        action: str,
        color: discord.Color,
        view: discord.ui.View | None = None,
    ) -> None:
        cfg = await get_guild_config(guild.id)
        channel_id = cfg["mod_log_channel_id"]
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return
        embed = infraction_embed(member, offence, severity, content, action, color)
        try:
            if view is not None:
                await channel.send(embed=embed, view=view)
            else:
                await channel.send(embed=embed)
        except discord.HTTPException:
            pass


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
