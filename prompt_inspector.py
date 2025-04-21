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
bot = commands.Bot(command_prefix=';', intents=intents)

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

    # Do stuff here before the bot shuts down

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
