import discord
import asyncio
import time
from discord.ext import commands
from settings import logger, CHAT
from PIL import Image
from io import BytesIO
import base64

class OnMessageCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.response_cooldowns = {}  # Track user cooldowns

    async def split_long_response(self, text, message:discord.Message):
        """Split long responses into multiple messages without breaking words"""
        chunks = self.split_by_words(text)
        for chunk in chunks:
            await message.reply(chunk)
            time.sleep(0.5)  # Rate limiting

    async def generate_ai_response(self, prompt, message: discord.Message):
        """Core AI response generation"""
        try:
            response = CHAT.send_message(prompt)
            if len(response.text) > 2000:
                await self.split_long_response(response.text, message)
            else:
                await message.reply(response.text)
            
            logger.info(f"Responded to {message.author} with {len(response.text)} chars")
        except Exception as e:
            logger.error(f"AI response error: {e}")
            await message.reply("Sorry, I encountered an error processing your request.")

    async def process_attachments(self, message: discord.Message):
        """Handle image attachments"""
        try:
            for attachment in message.attachments:
                if attachment.content_type.startswith('image/'):
                    image_data = await attachment.read()
                    img = Image.open(BytesIO(image_data))
                    
                    # Create prompt combining message text and image
                    prompt = f"""User ({message.author}) sent this image with message:
                    {message.content}

                    Please respond appropriately."""
                    
                    # Send multimodal request
                    response = CHAT.send_message([prompt, img])
                    await self.generate_ai_response(response.text, message)
        except Exception as e:
            logger.error(f"Image processing error: {e}")
            await message.reply("I had trouble processing that image.")

    def split_by_words(self, text, max_length=2000):
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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
            
        # Check if bot is mentioned OR message is a reply to bot
        is_mentioned = self.bot.user in message.mentions
        is_reply = False
        if message.reference:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                is_reply = ref_msg.author == self.bot.user
            except:
                pass
                
        if not (is_mentioned or is_reply):
            return

        # Check cooldown
        if self.response_cooldowns.get(message.author.id, 0) > time.time() - 10:
            return

        async with message.channel.typing():
            # Build context from message reference if available
            context = ""
            if message.reference:
                try:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                    context = f"\n\n(Context: {ref_msg.content})"
                except:
                    pass

            prompt = f"{message.author.mention}: {message.content}{context}"

            if message.attachments:
                await self.process_attachments(message)
            else:
                await self.generate_ai_response(prompt, message)

            # Update cooldown
            self.response_cooldowns[message.author.id] = time.time()

async def setup(bot: commands.Bot):
    await bot.add_cog(OnMessageCog(bot))
