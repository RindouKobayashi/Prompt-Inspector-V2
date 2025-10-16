import discord
import settings
import asyncio
from discord.ext import commands
from settings import logger
from context_menus import image_metadata_context_menu 


# Discord Bot Permission
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(
    command_prefix=';',
    intents=intents,
    # Add voice permissions for music functionality
    permissions=discord.Permissions(
        connect=True,      # Connect to voice channels
        speak=True,        # Speak in voice channels
        use_voice_activation=True  # Use voice activation (optional but recommended)
    )
)

@bot.event
async def on_ready():
    logger.info(f"User: {bot.user} (ID: {bot.user.id})")
    
    # Loading cogs
    for cog_file in settings.COGS_DIR.glob("*cog.py"):
        if cog_file.name != "__init__.py":
            ext_name = f"cogs.{cog_file.name[:-3]}"
            if ext_name not in bot.extensions: # Check if extension is already loaded
                try:
                    await bot.load_extension(ext_name)
                    logger.info(f"Loaded cog: {cog_file.name}")
                except Exception as e:
                    logger.error(f"Failed to load cog {cog_file.name}: {e}")
            else:
                logger.debug(f"Cog {cog_file.name} already loaded.") # Optional: Log if already loaded

    # Loading context menus
    image_metadata_context_menu.setup_contextmenu(bot)

    # Presence is now handled by PresenceCog
    
async def shutdown_tasks():
    """
    This function will be called when the bot is shutting down.
    """
    logger.warning("Bot is shutting down...")

    # Gracefully disconnect from all voice channels
    disconnect_tasks = []

    for guild in bot.guilds:
        voice_client = guild.voice_client
        if voice_client and voice_client.is_connected():
            # Create a task for each disconnect to run concurrently
            task = asyncio.create_task(disconnect_voice_client(guild, voice_client))
            disconnect_tasks.append(task)

    # Wait for all disconnects to complete concurrently
    if disconnect_tasks:
        try:
            await asyncio.gather(*disconnect_tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"Error during voice disconnections: {e}")

    # Do other cleanup here before the bot shuts down

async def disconnect_voice_client(guild, voice_client):
    """Helper function to disconnect a single voice client"""
    try:
        # Stop any currently playing audio
        if voice_client.is_playing():
            voice_client.stop()

        # Disconnect from voice channel with timeout
        try:
            # Use asyncio.wait_for with a 2-second timeout
            await asyncio.wait_for(voice_client.disconnect(), timeout=2.0)
        except asyncio.TimeoutError:
            # Timeout is expected during shutdown, just continue
            pass
        except Exception as e:
            logger.error(f"Error disconnecting from {guild.name}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in disconnect_voice_client for {guild.name}: {e}")

async def main():
    try:
        await bot.start(settings.DISCORD_API_TOKEN)
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt. Shutting down...")
    finally:
        await shutdown_tasks()
        if not bot.is_closed():
            await bot.close()

def run():
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Initiating shutdown...")
    finally:
        pending = asyncio.all_tasks(loop=loop)
        for task in pending:
            task.cancel()
        group = asyncio.gather(*pending, return_exceptions=True)
        loop.run_until_complete(group)
        loop.close()
        logger.info("Bot has been shut down.")

if __name__ == "__main__":
    run()
