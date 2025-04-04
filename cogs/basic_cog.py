import discord
import settings
import asyncio
from discord.ext import commands
from discord import app_commands
from settings import logger

class BasicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="sync", description="Sync the commands with the server")
    async def sync(self, interaction: discord.Interaction):
        """Sync the commands with the server"""
        logger.info(f"Command /sync has been invoked by {interaction.user}")
        # Check if the user is the owner of the bot
        if interaction.user.id != settings.BOT_OWNER_ID:
            await interaction.response.send_message("You are not allowed to run this command.", ephemeral=True)
            return
        await interaction.response.send_message("Syncing commands with the server...", ephemeral=True)
        await self.bot.tree.sync()
        await interaction.edit_original_response(content="Commands have been synced with the server.")
        

async def setup(bot: commands.Bot):
    await bot.add_cog(BasicCog(bot))