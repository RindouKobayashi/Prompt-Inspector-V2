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

    async def split_long_response(self, text, message:discord.Message):
        """Split long responses into multiple messages without breaking words"""
        chunks = self.split_by_words(text)
        for chunk in chunks:
            await message.reply(chunk)
            time.sleep(0.5)  # Rate limiting

    async def generate_ai_response(self, prompt_content, message: discord.Message):
        """Core AI response generation. Handles both text and multimodal prompts."""
        try:
            # Send the prompt content (string or list) to the AI
            response = CHAT.send_message(prompt_content)
            
            # Process the response text
            if len(response.text) > 2000:
                await self.split_long_response(response.text, message)
            else:
                await message.reply(response.text)
            
            logger.info(f"Responded to {message.author} with {len(response.text)} chars")
        except Exception as e:
            logger.error(f"AI response error: {e}")
            await message.reply("Sorry, I encountered an error processing your request.")

    async def process_attachments(self, message: discord.Message):
        """
        Processes the first image attachment found in the message.
        Returns a list [prompt_text, image_object] if an image is processed, otherwise None.
        """
        try:
            for attachment in message.attachments:
                if attachment.content_type.startswith('image/'):
                    image_data = await attachment.read()
                    img = Image.open(BytesIO(image_data))
                    
                    # Create prompt combining message text and image
                    prompt_text = f"""User ({message.author}) sent this image with message:
                    {message.content}

                    Please respond appropriately."""
                    
                    # Return the prompt text and image object for the first image found
                    return [prompt_text, img] 
            # Return None if no image attachment was processed
            return None
        except Exception as e:
            logger.error(f"Image processing error: {e}")
            await message.reply("I had trouble processing that image.")
            return None # Ensure None is returned on error too

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

        async with message.channel.typing():
            # Build context from message reference if available
            context = ""
            if message.reference:
                try:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                    context = f"\n\n(Context: {ref_msg.content})"
                except:
                    pass

            # Prepare the base text prompt
            base_prompt = f"{message.author.mention}: {message.content}{context}"
            
            # Default content to send is the base text prompt
            content_to_send = base_prompt

            # If there are attachments, try to process them
            if message.attachments:
                multimodal_content = await self.process_attachments(message)
                # If an image was successfully processed, use that content
                if multimodal_content:
                    content_to_send = multimodal_content
            
            # Generate the response using the determined content (text or multimodal)
            await self.generate_ai_response(content_to_send, message)


async def setup(bot: commands.Bot):
    await bot.add_cog(OnMessageCog(bot))
