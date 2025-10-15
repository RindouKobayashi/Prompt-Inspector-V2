import discord
import random
from discord.ext import commands, tasks
from settings import logger

class PresenceCog(commands.Cog):
    """Handles the bot's auto-rotating presence."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_count = 0
        self.current_status = None
        self.start_time = discord.utils.utcnow()
        self.change_status.start()

    def cog_unload(self):
        self.change_status.cancel()

    def get_statuses(self):
        """Returns dynamic statuses including bot info"""
        all_statuses = [
            (discord.ActivityType.playing, "with the API"),
            (discord.ActivityType.watching, f"{self.guild_count} servers"),
            (discord.ActivityType.listening, "user requests"),
            (discord.ActivityType.competing, "the Turing test"),
            (discord.ActivityType.playing, "Inspector Gadget"),
            (discord.ActivityType.watching, "pixels render"),
            (discord.ActivityType.watching, f"{len(self.bot.users)} users"),
            (discord.ActivityType.listening, "slash commands"),
            (discord.ActivityType.playing, "with prompts"),
            (discord.ActivityType.watching, "for new features"),
            (discord.ActivityType.listening, f"Ping: {round(self.bot.latency * 1000)}ms"),
            (discord.ActivityType.watching, f"Uptime: {round((discord.utils.utcnow() - self.start_time).total_seconds() / 60)} minutes"),
        ]
        # Filter out current status if it exists
        if self.current_status:
            return [s for s in all_statuses if s != self.current_status]
        return all_statuses

    @tasks.loop(seconds=12)
    async def change_status(self):
        """Cycles through dynamic statuses without repeating."""
        try:
            # Check if music is currently playing OR if bot is alone in VC (don't override music Rich Presence)
            should_skip_presence = False

            for cog in self.bot.cogs.values():
                if hasattr(cog, 'now_playing'):
                    # Check if any guild has music playing
                    if any(cog.now_playing.values()):
                        should_skip_presence = True
                        break

                    # Also check if bot is alone in any voice channel (showing disconnect countdown)
                    for guild in self.bot.guilds:
                        voice_client = guild.voice_client
                        if (voice_client and voice_client.is_connected() and
                            hasattr(voice_client, 'alone_since') and
                            len([m for m in voice_client.channel.members if not m.bot]) == 0):
                            should_skip_presence = True
                            break

                    if should_skip_presence:
                        break

            if should_skip_presence:
                logger.debug("Music playing or bot alone in VC, skipping presence change")
                return

            self.guild_count = len(self.bot.guilds)
            available_statuses = self.get_statuses()
            if not available_statuses:
                return

            self.current_status = random.choice(available_statuses)
            activity_type, status_text = self.current_status
            activity = discord.Activity(type=activity_type, name=status_text)
            await self.bot.change_presence(activity=activity)
            logger.debug(f"Changed presence to: {activity_type.name} {status_text}")
        except Exception as e:
            logger.error(f"Error in change_status loop: {e}", exc_info=True)

    @change_status.before_loop
    async def before_change_status(self):
        """Waits until the bot is ready before starting the loop."""
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    """Adds the PresenceCog to the bot."""
    await bot.add_cog(PresenceCog(bot))
