import discord


def infraction_embed(member, offence, severity, content, action,
                     color=discord.Color.orange()):
    embed = discord.Embed(
        title=f"{severity} severity — {offence}",
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)
    if content:
        snippet = content if len(content) <= 1000 else content[:997] + "..."
        embed.add_field(name="Message", value=snippet, inline=False)
    embed.add_field(name="Action", value=action, inline=False)
    return embed
