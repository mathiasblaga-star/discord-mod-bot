import discord
from discord.ext import commands

from database import (
    log_infraction, count_infractions_of_type,
    get_guild_config, get_slur_list,
)
from utils.fuzzy_match import contains_slur
from utils.embeds import infraction_embed
from utils.actions import try_delete, apply_mute, notify_member
from utils.views import ReviewView, UndoBanView


class SlurCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        cfg = await get_guild_config(message.guild.id)

        if isinstance(message.author, discord.Member):
            m = message.author
            if m.guild_permissions.administrator:
                return
            admin_role_id = cfg["admin_role_id"]
            if admin_role_id and any(r.id == admin_role_id for r in m.roles):
                return

        slurs = await get_slur_list(message.guild.id)
        matched = contains_slur(message.content, slurs, cfg["fuzzy_threshold"])
        if not matched:
            return

        await try_delete(message)

        prior = await count_infractions_of_type(message.author.id, message.guild.id, "slur")
        member = message.author
        guild = message.guild
        admin_role_id = cfg["admin_role_id"]
        ping_admin = f"<@&{admin_role_id}>" if admin_role_id else None

        if prior == 0:
            await notify_member(
                member,
                f"Muted for {cfg['slur_mute_duration'] // 60} minutes",
                f"Use of a prohibited slur ({matched!r})",
                guild.name,
            )
            await apply_mute(member, cfg["slur_mute_duration"],
                             "Slur use (1st offence)", muted_role_id=cfg["muted_role_id"])
            action = f"Muted {cfg['slur_mute_duration'] // 60}m"
            severity, color = "Medium", discord.Color.orange()
            channel_id, ping, view = cfg["mod_log_channel_id"], None, ReviewView()
        elif prior == 1:
            await notify_member(
                member, "Kicked",
                "Repeated use of prohibited slurs (offence #2)",
                guild.name,
            )
            try:
                await member.kick(reason="Slur use (2nd offence)")
                action = "Kicked"
            except discord.HTTPException:
                action = "Kick failed"
            severity, color = "Severe", discord.Color.red()
            channel_id, ping, view = cfg["mod_alerts_channel_id"], ping_admin, ReviewView()
        else:
            await notify_member(
                member, "Permanently Banned",
                f"Repeated use of prohibited slurs (offence #{prior + 1})",
                guild.name,
            )
            try:
                await guild.ban(member, reason="Slur use (3rd offence)")
                action = "Permanently banned"
            except discord.HTTPException:
                action = "Ban failed"
            severity, color = "Severe", discord.Color.dark_red()
            channel_id, ping, view = cfg["mod_alerts_channel_id"], ping_admin, UndoBanView(member.id)

        await log_infraction(
            member.id, guild.id, "slur", severity, message.content, action,
            redact=True,
        )

        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel:
                embed = infraction_embed(member, f"Slur Detected ({matched!r})",
                                         severity, message.content, action, color)
                try:
                    await channel.send(content=ping, embed=embed, view=view)
                except discord.HTTPException:
                    pass


async def setup(bot):
    await bot.add_cog(SlurCog(bot))
