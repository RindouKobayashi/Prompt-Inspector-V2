import discord
from discord.ext import commands
from discord import app_commands
import settings
from settings import logger, generate_content

class CommandsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="toggle_channel")
    @app_commands.default_permissions(manage_messages=True)
    async def toggle_channel(self, interaction: discord.Interaction, channel_id: str = None):
        """Adds/Removes a channel to the list of monitored channels"""
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return
            
        try:
            channel_id = int(channel_id) if channel_id else interaction.channel_id
            if channel_id in settings.monitored_channels:
                settings.monitored_channels.remove(channel_id)
                await interaction.response.send_message(f"Removed {channel_id} from the list of monitored channels.", ephemeral=True)
            else:
                settings.monitored_channels.append(channel_id)
                await interaction.response.send_message(f"Added {channel_id} to the list of monitored channels.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Invalid channel ID.", ephemeral=True)
        except Exception as e:
            logger.error(f"{type(e).__name__}: {e}")
            await interaction.response.send_message("Internal bot error, please contact the bot owner.", ephemeral=True)

    @app_commands.user_install
    @app_commands.command(name="status")
    async def status(self, interaction: discord.Interaction):
        """Get the status of the VM/bot."""
        if interaction.user.id != settings.BOT_OWNER_ID:
            await interaction.response.send_message("You are not allowed to run this command.", ephemeral=True)
            return
        try:
            import psutil
            embed = discord.Embed(title="Status", color=0x00ff00)
            embed.add_field(name="CPU Usage", value=f"{psutil.cpu_percent()}%")
            embed.add_field(name="RAM Usage", value=f"{psutil.virtual_memory().percent}%")
            embed.add_field(name="Disk Usage", value=f"{psutil.disk_usage('/').percent}%")
            embed.set_footer(text="migus? plapped.", icon_url=interaction.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except ImportError:
            await interaction.response.send_message("Status monitoring not available (psutil not installed)", ephemeral=True)

    @app_commands.command(name="summarize")
    @app_commands.describe(
        message_count="Number of messages to summarize (default: 10)",
        ephemeral="Whether to show summary privately (default: True)"
    )
    async def summarize_chat(
        self,
        interaction: discord.Interaction,
        message_count: int = 10,
        ephemeral: bool = True
    ):
        """Summarize recent messages in this channel"""
        await interaction.response.defer(ephemeral=ephemeral)
        
        try:
            # Fetch and sort messages
            messages = []
            async for msg in interaction.channel.history(limit=message_count):
                if not msg.author.bot and msg.content:
                    messages.append(msg)
            
            messages.sort(key=lambda m: m.created_at)
            
            if not messages:
                await interaction.followup.send("No messages found to summarize", ephemeral=ephemeral)
                return
            
            # Prepare context for summarization
            context = "\n".join(
                f"{msg.author.display_name} ({msg.created_at.strftime('%H:%M')}): {msg.content}"
                for msg in messages
            )
            
            # Generate summary
            prompt = f"""Summarize this conversation in 3-5 bullet points:
            {context}"""
            
            response = generate_content(prompt)
            summary = response.text
            
            # Format and send response
            embed = discord.Embed(
                title=f"Summary of last {len(messages)} messages",
                description=summary,
                color=discord.Color.blurple()
            )
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
            
        except Exception as e:
            logger.error(f"Summarization error: {e}")
            await interaction.followup.send(
                "Failed to generate summary. Please try again later.",
                ephemeral=ephemeral
            )

    @app_commands.user_install
    @app_commands.describe(
        question="The question to ask the bot",
        ephemeral="Whether to show the answer privately (default: True)"
    )
    @app_commands.command(name="ask", description="Ask the bot a question (no context)")
    async def ask(self, interaction: discord.Interaction, question: str, ephemeral: bool = True):
        """Ask the bot a question"""
        await interaction.response.defer(ephemeral=ephemeral)

        try:
            response = generate_content(question)
            if len(response.text) > 2000:
                # Split response into chunks if it's too long by word
                chunks = split_by_words(response.text, max_length=2000)
                for chunk in chunks:
                    await interaction.followup.send(chunk, ephemeral=ephemeral)
            else:
                # Send the response directly if it's short enough
                await interaction.followup.send(response.text, ephemeral=ephemeral)
        except Exception as e:
            logger.error(f"Ask command error: {e}")
            await interaction.followup.send("Failed to get a response. Please try again later.", ephemeral=ephemeral)

def split_by_words(text, max_length=2000):
        """Split text into chunks without breaking words"""
        words = text.split(' ')
        chunks = []
        current_chunk = ""
        
        for word in words:
            if len(current_chunk) + len(word) + 1 <= max_length:
                current_chunk += f" {word}"
            else:
                chunks.append(current_chunk.strip())
                current_chunk = word
                
        if current_chunk:
            chunks.append(current_chunk.strip())
            
        return chunks


async def setup(bot: commands.Bot):
    await bot.add_cog(CommandsCog(bot))
