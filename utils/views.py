import re

import discord

from database import get_guild_config, remove_temp_ban


async def is_admin(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    if interaction.guild:
        cfg = await get_guild_config(interaction.guild.id)
        admin_role_id = cfg.get("admin_role_id", 0)
        if admin_role_id and any(r.id == admin_role_id for r in member.roles):
            return True
    return False


class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Mark Reviewed",
        style=discord.ButtonStyle.secondary,
        custom_id="mod:mark_reviewed",
    )
    async def review(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_admin(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        button.disabled = True
        button.label = f"Reviewed by {interaction.user}"
        await interaction.response.edit_message(view=self)


class UndoBanButton(discord.ui.DynamicItem[discord.ui.Button], template=r"mod:undo_ban:(?P<uid>\d+)"):
    def __init__(self, user_id: int):
        super().__init__(
            discord.ui.Button(
                label="Undo Ban",
                style=discord.ButtonStyle.danger,
                custom_id=f"mod:undo_ban:{user_id}",
            )
        )
        self.user_id = user_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match,
    ) -> "UndoBanButton":
        return cls(int(match.group("uid")))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await is_admin(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        try:
            await interaction.guild.unban(
                discord.Object(id=self.user_id),
                reason=f"Undo ban by {interaction.user}",
            )
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"Failed: {e}", ephemeral=True)
        # Clean up any temp-ban DB record so the background task doesn't
        # try to unban an already-unbanned user and leave a stale row.
        if interaction.guild:
            await remove_temp_ban(self.user_id, interaction.guild.id)
        done = discord.ui.View(timeout=None)
        done.add_item(discord.ui.Button(
            label="Ban Reverted", style=discord.ButtonStyle.danger, disabled=True,
        ))
        await interaction.response.edit_message(view=done)


class UndoBanView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.add_item(UndoBanButton(user_id))
