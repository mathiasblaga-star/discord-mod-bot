import discord
from discord.ext import commands

from database import (
    log_infraction, count_infractions_of_type, get_guild_config,
)
from utils.actions import try_delete, apply_mute, notify_member
from utils.embeds import infraction_embed
from utils.link_scanner import scan_message
from utils.views import ReviewView

PHISHING_MUTE_SEC = 10 * 60
INVITE_ESCALATE_THRESHOLD = 3
INVITE_ESCALATE_MUTE_SEC = 30 * 60


class LinkFilterCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self._process(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content == after.content:
            return
        await self._process(after)

    async def _process(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if (isinstance(message.author, discord.Member)
                and message.author.guild_permissions.manage_messages):
            return

        has_invite, has_phishing = scan_message(message.content)
        if not (has_invite or has_phishing):
            return

        cfg = await get_guild_config(message.guild.id)

        if has_phishing and cfg["block_phishing"]:
            await self._handle_phishing(message, cfg)
        elif has_invite and cfg["block_invites"]:
            await self._handle_invite(message, cfg)

    # ------------------------------------------------------------------ handlers

    async def _handle_phishing(self, message: discord.Message, cfg: dict):
        await try_delete(message)
        await notify_member(
            message.author,
            f"Muted for {PHISHING_MUTE_SEC // 60} minutes",
            "Posting a suspicious/phishing link",
            message.guild.name,
        )
        await apply_mute(
            message.author, PHISHING_MUTE_SEC,
            "Phishing link", muted_role_id=cfg["muted_role_id"],
        )

        await log_infraction(
            message.author.id, message.guild.id,
            "phishing_link", "Severe",
            message.content,
            f"Deleted + muted {PHISHING_MUTE_SEC // 60}m",
        )

        alerts_id = cfg["mod_alerts_channel_id"]
        admin_role_id = cfg["admin_role_id"]
        if alerts_id:
            channel = message.guild.get_channel(alerts_id)
            if channel:
                ping = f"<@&{admin_role_id}>" if admin_role_id else None
                preview = message.content[:300]
                if len(message.content) > 300:
                    preview = preview[:297] + "..."
                embed = infraction_embed(
                    message.author,
                    "Phishing Link Detected",
                    "Severe",
                    preview,
                    f"Deleted + muted {PHISHING_MUTE_SEC // 60}m",
                    discord.Color.red(),
                )
                try:
                    await channel.send(content=ping, embed=embed, view=ReviewView())
                except discord.HTTPException:
                    pass

    async def _handle_invite(self, message: discord.Message, cfg: dict):
        await try_delete(message)
        try:
            await message.channel.send(
                f"{message.author.mention} posting invite links is not allowed.",
                delete_after=8,
            )
        except discord.HTTPException:
            pass

        await log_infraction(
            message.author.id, message.guild.id,
            "invite_link", "Low",
            message.content, "Deleted",
        )

        mod_log_id = cfg["mod_log_channel_id"]
        if mod_log_id:
            channel = message.guild.get_channel(mod_log_id)
            if channel:
                embed = infraction_embed(
                    message.author,
                    "Invite Link",
                    "Low",
                    message.content,
                    "Deleted",
                    discord.Color.yellow(),
                )
                try:
                    await channel.send(embed=embed, view=ReviewView())
                except discord.HTTPException:
                    pass

        count = await count_infractions_of_type(
            message.author.id, message.guild.id, "invite_link",
        )
        if count >= INVITE_ESCALATE_THRESHOLD:
            await notify_member(
                message.author,
                f"Muted for {INVITE_ESCALATE_MUTE_SEC // 60} minutes",
                f"Repeated invite link posting ({count} offences)",
                message.guild.name,
            )
            await apply_mute(
                message.author, INVITE_ESCALATE_MUTE_SEC,
                "Repeated invite link posting",
                muted_role_id=cfg["muted_role_id"],
            )

            alerts_id = cfg["mod_alerts_channel_id"]
            admin_role_id = cfg["admin_role_id"]
            if alerts_id:
                channel = message.guild.get_channel(alerts_id)
                if channel:
                    ping = f"<@&{admin_role_id}>" if admin_role_id else None
                    embed = infraction_embed(
                        message.author,
                        f"Repeated Invite Links ({count}x)",
                        "Medium",
                        message.content,
                        f"Muted {INVITE_ESCALATE_MUTE_SEC // 60}m",
                        discord.Color.orange(),
                    )
                    try:
                        await channel.send(content=ping, embed=embed, view=ReviewView())
                    except discord.HTTPException:
                        pass


async def setup(bot):
    await bot.add_cog(LinkFilterCog(bot))
