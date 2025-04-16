import discord
import settings
import asyncio
import signal
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
    loaded_cogs = 0
    for cog_file in settings.COGS_DIR.glob("*cog.py"):
        if cog_file.name != "__init__.py":
            ext_name = f"cogs.{cog_file.name[:-3]}"
            if ext_name not in bot.extensions: # Check if extension is already loaded
                try:
                    await bot.load_extension(ext_name)
                    logger.info(f"Loaded cog: {cog_file.name}")
                    loaded_cogs += 1
                except Exception as e:
                    logger.error(f"Failed to load cog {cog_file.name}: {e}")
            else:
                logger.debug(f"Cog {cog_file.name} already loaded.") # Optional: Log if already loaded
    logger.info(f"Total cogs loaded: {loaded_cogs}")

    # Loading context menus
    try:
        # Assuming setup_contextmenu might be async or just sync setup
        # If it's just adding commands, it might not need await, but check its definition
        if asyncio.iscoroutinefunction(image_metadata_context_menu.setup_contextmenu):
             await image_metadata_context_menu.setup_contextmenu(bot)
        else:
             image_metadata_context_menu.setup_contextmenu(bot)
        logger.info("Context menus setup.")
    except Exception as e:
        logger.error(f"Failed to setup context menus: {e}")


    # Change bot status
    try:
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="your messages"))
        logger.info("Bot presence set.")
    except Exception as e:
        logger.error(f"Failed to set bot presence: {e}")

    logger.info("Bot is ready.")
    
async def shutdown_tasks():
    """
    This function will be called when the bot is shutting down.
    """
    logger.warning("Bot is shutting down...")

    # Do stuff here before the bot shuts down

async def main():
    # Remove the try/except/finally block from here
    # Let the run() function handle the shutdown signal
    async with bot: # Use async context manager for bot lifecycle
        # Load extensions/cogs BEFORE starting the bot
        # (Moved loading logic primarily to on_ready, ensure setup is complete there)

        # Start the bot
        await bot.start(settings.DISCORD_API_TOKEN)
    # The bot will run until bot.close() is called or an error occurs


def run():
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Initiating shutdown...")
        # Perform graceful shutdown HERE
        try:
            # Run shutdown_tasks first
            loop.run_until_complete(shutdown_tasks())

            # Then close the bot connection
            if bot.is_ready(): # Check if bot is actually running/ready
                 logger.info("Closing Discord connection...")
                 loop.run_until_complete(bot.close())
                 logger.info("Discord connection closed.")
            else:
                 logger.warning("Bot was not ready, skipping bot.close().")

        except Exception as e:
            logger.error(f"Error during graceful shutdown: {e}")

    finally:
        logger.info("Cleaning up asyncio tasks...")
        # Now cancel any remaining tasks (should be fewer after bot.close())
        pending = asyncio.all_tasks(loop=loop)
        if pending:
             logger.info(f"Cancelling {len(pending)} pending tasks.")
             for task in pending:
                 task.cancel()
             group = asyncio.gather(*pending, return_exceptions=True)
             try:
                 # Give cancellations a moment to run
                 loop.run_until_complete(asyncio.wait_for(group, timeout=5.0))
                 logger.info("Pending tasks cancelled/finished.")
             except asyncio.TimeoutError:
                 logger.warning("Timeout waiting for tasks to cancel cleanly.")
             except Exception as e:
                 logger.error(f"Error while gathering cancelled tasks: {e}")
        else:
            logger.info("No pending tasks to cancel.")

        if not loop.is_closed():
             loop.close()
             logger.info("Event loop closed.")
        logger.info("Bot has been shut down.")


if __name__ == "__main__":
    run()
