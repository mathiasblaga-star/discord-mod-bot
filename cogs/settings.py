import discord
from discord import app_commands
from discord.ext import commands

from database import (
    GUILD_CONFIG_DEFAULTS,
    get_guild_config, set_guild_config, reset_guild_config,
    get_slur_list, add_slur, remove_slur,
)

_KEYS = list(GUILD_CONFIG_DEFAULTS.keys())
_KEY_CHOICES = [app_commands.Choice(name=k, value=k) for k in _KEYS]


def _admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if isinstance(member, discord.Member) and member.guild_permissions.administrator:
            return True
        await interaction.response.send_message(
            "Administrator permission required.", ephemeral=True
        )
        return False
    return app_commands.check(predicate)


class SettingsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    config_group = app_commands.Group(
        name="config",
        description="Configure the moderation bot for this server.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    @config_group.command(name="view", description="Show all current settings.")
    @_admin_only()
    async def view(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        cfg = await get_guild_config(interaction.guild.id)
        slurs = await get_slur_list(interaction.guild.id)
        embed = discord.Embed(
            title=f"Configuration — {interaction.guild.name}",
            color=discord.Color.blurple(),
        )
        for k in _KEYS:
            embed.add_field(name=k, value=str(cfg.get(k, 0)), inline=True)
        embed.add_field(
            name="slur_list",
            value=", ".join(slurs) if slurs else "(empty)",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config_group.command(name="set", description="Update a single configuration value.")
    @app_commands.describe(key="Setting to change", value="New integer value")
    @app_commands.choices(key=_KEY_CHOICES)
    @_admin_only()
    async def set_cmd(
        self,
        interaction: discord.Interaction,
        key: app_commands.Choice[str],
        value: str,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        try:
            v = int(value)
        except ValueError:
            return await interaction.response.send_message(
                "Value must be an integer.", ephemeral=True
            )
        await set_guild_config(interaction.guild.id, **{key.value: v})
        await interaction.response.send_message(
            f"Set `{key.value}` to `{v}`.", ephemeral=True
        )

    @config_group.command(name="reset", description="Reset all settings to defaults.")
    @_admin_only()
    async def reset(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        await reset_guild_config(interaction.guild.id)
        await interaction.response.send_message(
            "Settings reset to defaults.", ephemeral=True
        )

    @config_group.command(name="addslur", description="Add a slur to the filter list.")
    @app_commands.describe(word="Canonical slur to add (stored lowercase, matched via leetspeak/fuzzy)")
    @_admin_only()
    async def addslur(self, interaction: discord.Interaction, word: str):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        added = await add_slur(interaction.guild.id, word)
        msg = f"Added `{word.lower()}` to slur list." if added else f"`{word.lower()}` was already in the list."
        await interaction.response.send_message(msg, ephemeral=True)

    @config_group.command(name="removeslur", description="Remove a slur from the filter list.")
    @app_commands.describe(word="Slur to remove")
    @_admin_only()
    async def removeslur(self, interaction: discord.Interaction, word: str):
        if not interaction.guild:
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        removed = await remove_slur(interaction.guild.id, word)
        msg = f"Removed `{word.lower()}`." if removed else f"`{word.lower()}` was not in the list."
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(SettingsCog(bot))
