import discord
import asyncio
import time
import re # Added for regex
from urllib.parse import urlparse # Added for URL parsing
from discord.ext import commands
from settings import logger, CHAT
from PIL import Image
from io import BytesIO
import base64

# Helper function to extract domain from URL or text
def get_domain(text):
    try:
        # Try parsing as a full URL first
        parsed_url = urlparse(text)
        if parsed_url.netloc:
            domain = parsed_url.netloc
        else:
            # If not a full URL, try adding scheme and parsing again
            # This helps catch domains like "google.com" in display text
            parsed_url = urlparse(f"http://{text}")
            if parsed_url.netloc:
                domain = parsed_url.netloc
            else: # If still no domain, return None
                return None
        # Remove 'www.' prefix if present and convert to lowercase
        return domain.replace("www.", "").lower()
    except Exception:
        return None

class OnMessageCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def notify_potential_scam(self, message: discord.Message, display_text: str, actual_url: str):
        """Replies with an embed warning about potential masked link scams."""
        embed = discord.Embed(
            title="⚠️ Potential Scam Link Detected!",
            description="This message contains a link where the displayed text might not match the actual destination. Please exercise caution.",
            color=discord.Color.orange() # Or discord.Color.red()
        )
        # Use inline=False to ensure fields appear on separate lines
        embed.add_field(name="Displayed Text", value=f"`{display_text}`", inline=False)
        # Use code formatting for the URL to prevent Discord from trying to embed it
        embed.add_field(name="Actual Destination", value=f"`{actual_url}`", inline=False)
        embed.set_footer(text="Always verify links before clicking, especially if they seem suspicious.")

        try:
            await message.reply(embed=embed)
            logger.warning(f"Potential scam link detected in message {message.id} from {message.author}. Display: '{display_text}', Actual: '{actual_url}'")
        except discord.HTTPException as e:
            logger.error(f"Failed to send scam warning embed for message {message.id}: {e}")

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
        # Ignore bots
        if message.author.bot:
            return

        # --- Masked Link Scam Check ---
        # Regex to find markdown links: [display text](actual_url) or [display text](<actual_url>)
        # It captures the display text (group 1) and the actual URL (group 2)
        # Handles optional angle brackets < > around the URL.
        masked_link_pattern = r'\[([^\]]+)\]\(<?(https?://[^>\)]+)>?\)'
        suspicious_link_details = None # Store details if found
        for match in re.finditer(masked_link_pattern, message.content):
            display_text = match.group(1)
            actual_url = match.group(2)

            display_domain = get_domain(display_text)
            actual_domain = get_domain(actual_url)

            # If we could extract both domains and they are different, flag it
            if display_domain and actual_domain and display_domain != actual_domain:
                # Check for common false positives like linking subdomains/paths
                # e.g., [docs.example.com](https://example.com/docs) should be allowed
                # Allow if display domain is a subdomain of actual domain or vice-versa
                if not (display_domain.endswith(f".{actual_domain}") or actual_domain.endswith(f".{display_domain}")):
                    suspicious_link_details = (display_text, actual_url)
                    break # Stop checking after the first suspicious link

        # If a suspicious link was found, notify and stop processing
        if suspicious_link_details:
            await self.notify_potential_scam(message, suspicious_link_details[0], suspicious_link_details[1])
            return # Stop processing this message further

        # --- Original AI Response Logic (Only if not a scam and mentioned/replied) ---
        # Check if bot is mentioned OR message is a reply to bot
        is_mentioned = self.bot.user in message.mentions
        is_reply = False
        if message.reference:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                is_reply = ref_msg.author == self.bot.user
            except discord.NotFound:
                logger.warning(f"Reference message {message.reference.message_id} not found.")
            except discord.HTTPException as e:
                 logger.error(f"Failed to fetch reference message {message.reference.message_id}: {e}")
            except Exception as e:
                 logger.error(f"Unexpected error fetching reference message {message.reference.message_id}: {e}")


        # Only proceed with AI response if mentioned or replied to
        if not (is_mentioned or is_reply):
            return # Don't process for AI if not mentioned/reply

        # If it's a mention/reply and passed scam check, proceed with AI response
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
