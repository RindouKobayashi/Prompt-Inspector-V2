import discord
import random
from datetime import datetime, timezone, timedelta
from discord.ext import commands, tasks
from settings import logger

class PresenceCog(commands.Cog):
    """Handles the bot's auto-rotating presence."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_count = 0
        self.current_status = None
        self.start_time = discord.utils.utcnow()
        # GTA 6 release date: November 19, 2026, 00:00 EST (UTC-5)
        self.release_date = datetime(2026, 11, 19, 0, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
        self.change_status.start()

    def cog_unload(self):
        self.change_status.cancel()

    def get_statuses(self):
        """Returns the GTA 6 countdown status"""
        now = datetime.now(timezone.utc)
        time_remaining = self.release_date - now

        if time_remaining.total_seconds() <= 0:
            return [(discord.ActivityType.playing, "GTA 6 is out!")]

        days = time_remaining.days
        hours, remainder = divmod(time_remaining.seconds, 3600)
        minutes, _ = divmod(remainder, 60)

        countdown_text = f"GTA 6 in {days}d {hours}h {minutes}m"
        return [(discord.ActivityType.watching, countdown_text)]

    @tasks.loop(minutes=1)
    async def change_status(self):
        """Updates the countdown status every minute."""
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

            available_statuses = self.get_statuses()
            if not available_statuses:
                return

            # Since there's only one status now, always use it
            activity_type, status_text = available_statuses[0]
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
