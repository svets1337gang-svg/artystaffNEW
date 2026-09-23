import discord
from datetime import datetime

class BotStyle:
    PRIMARY_COLOR = discord.Color.from_rgb(72, 126, 174) # Элегантный серо-голубой
    SUCCESS_COLOR = discord.Color.green()
    WARNING_COLOR = discord.Color.gold()
    ERROR_COLOR = discord.Color.red()
    FOOTER_TEXT = "Staff Management System • Professional Edition"

    @staticmethod
    def create_embed(title, description, color=PRIMARY_COLOR, fields=None):
        embed = discord.Embed(
            title=title,
            description=description,
            color=color
        )
        if fields:
            for name, value, inline in fields:
                embed.add_field(name=name, value=value, inline=inline)

        embed.set_footer(text=BotStyle.FOOTER_TEXT)
        embed.timestamp = datetime.utcnow()
        return embed
