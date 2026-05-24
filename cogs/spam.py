import time
from collections import defaultdict, deque

import discord
from discord.ext import commands

from database import log_infraction, get_guild_config
from utils.actions import try_delete, apply_mute, notify_member
from utils.embeds import infraction_embed
from utils.views import ReviewView


class SpamCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Keyed by (user_id, guild_id) so counters are isolated per guild.
        self._times: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=20))
        self._content: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=10))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if (isinstance(message.author, discord.Member)
                and message.author.guild_permissions.manage_messages):
            return

        cfg = await get_guild_config(message.guild.id)

        unique_mentions = {u.id for u in message.mentions if u.id != message.author.id}
        if message.mention_everyone or len(unique_mentions) >= cfg["mass_mention_limit"]:
            await self._handle_mass_mention(message, cfg)
            return

        key = (message.author.id, message.guild.id)
        now = time.time()
        times = self._times[key]
        times.append(now)
        if sum(1 for t in times if now - t <= cfg["spam_time_window"]) >= cfg["spam_message_count"]:
            await self._handle_rate_spam(message, cfg)
            times.clear()
            return

        content = self._content[key]
        content.append(message.content)
        if message.content and content.count(message.content) >= cfg["duplicate_message_limit"]:
            await self._handle_duplicate_spam(message, cfg)
            content.clear()

    async def _handle_mass_mention(self, message: discord.Message, cfg: dict):
        await try_delete(message)
        await notify_member(
            message.author, "Kicked",
            "Mass mention / @everyone spam", message.guild.name,
        )
        try:
            await message.author.kick(reason="Mass mention / @everyone spam")
            action = "Kicked"
        except discord.HTTPException:
            action = "Kick failed"
        await log_infraction(message.author.id, message.guild.id,
                             "mass_mention", "Medium", message.content, action)
        await self._post_modlog(message.guild, message.author,
                                "Mass Mentions", "Medium", message.content, action,
                                discord.Color.orange(), cfg)

    async def _handle_rate_spam(self, message: discord.Message, cfg: dict):
        await try_delete(message)
        await notify_member(
            message.author, "Warning issued",
            "Sending messages too quickly", message.guild.name,
        )
        try:
            await message.channel.send(
                f"{message.author.mention} please slow down.", delete_after=5
            )
        except discord.HTTPException:
            pass
        await log_infraction(message.author.id, message.guild.id,
                             "spam_rate", "Low", message.content, "Warned + message deleted")
        await self._post_modlog(message.guild, message.author,
                                "Rate Spam", "Low", message.content, "Warned",
                                discord.Color.yellow(), cfg)

    async def _handle_duplicate_spam(self, message: discord.Message, cfg: dict):
        await try_delete(message)
        await notify_member(
            message.author,
            f"Muted for {cfg['spam_mute_duration'] // 60} minutes",
            "Sending duplicate messages repeatedly",
            message.guild.name,
        )
        await apply_mute(message.author, cfg["spam_mute_duration"],
                         "Duplicate message spam", muted_role_id=cfg["muted_role_id"])
        action = f"Muted {cfg['spam_mute_duration'] // 60}m"
        await log_infraction(message.author.id, message.guild.id,
                             "duplicate_spam", "Medium", message.content, action)
        await self._post_modlog(message.guild, message.author,
                                "Duplicate Spam", "Medium", message.content, action,
                                discord.Color.orange(), cfg)

    async def _post_modlog(self, guild, member, offence, severity, content, action, color, cfg):
        channel_id = cfg["mod_log_channel_id"]
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return
        embed = infraction_embed(member, offence, severity, content, action, color)
        try:
            await channel.send(embed=embed, view=ReviewView())
        except discord.HTTPException:
            pass


async def setup(bot):
    await bot.add_cog(SpamCog(bot))
