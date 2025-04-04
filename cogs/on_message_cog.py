import discord
import settings
from settings import logger
from discord.ext import commands


class OnMessageCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):

        
        if message.author.bot:
            return
        
        # Check if bot is mentioned

        logger.info(f"Checking if {self.bot} is mentioned in message :{message.mentions}")

        if self.bot in message.mentions:
            
            async with message.channel.typing():

                # Check if there is any attachment
                if message.attachments:
                    logger.info(f"Attachment detected in message, type: {message.attachments[0].content_type}")
                    attachments = [a for a in message.attachments if a.filename.lower().endswith(".png") or a.filename.lower().endswith(".webp")]

                else:
                    response = settings.CHAT.send_message(
                        message.content
                    )

                    for part in response.candidates[0].content.parts:
                        if part.text is not None:
                            if len(part.text) > 2000:
                                # Split the message into multiple messages
                                for chunk in [part.text[i:i + 2000] for i in range(0, len(part.text), 2000)]:
                                    message = await message.reply(chunk)
                            else:
                                message = await message.reply(part.text)

                        elif part.inline_data is not None:
                            image_bytes = settings.Image.open(settings.BytesIO(part.inline_data.data))
                            image_base64 = settings.base64.b64encode(image_bytes).decode("utf-8")

                            # Save the image
                            file_path = f"temp/{message.id}.png"
                            output_dir = settings.GEMINI_DIR / file_path
                            output_dir.mkdir(exist_ok=True)
                            (output_dir / file_path).write_bytes(image_bytes)

                            # Send the image
                            files = []
                            file_path = f"{output_dir}/{message.id}.png"
                            file = discord.File(file_path)
                            files.append(file)

                            message = await message.reply(file=file)





async def setup(bot: commands.Bot):
    await bot.add_cog(OnMessageCog(bot))