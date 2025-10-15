import io
import asyncio
import gzip
import json
import os
import traceback
from collections import OrderedDict
from pathlib import Path
import gradio_client
import discord
import settings
from discord import Embed, ButtonStyle, Message, Attachment, File, RawReactionActionEvent
from discord.ext import commands
from discord.ui import View, button
from PIL import Image
from settings import logger

SCAN_LIMIT_BYTES = int(os.getenv('SCAN_LIMIT_BYTES', '41943040'))  # Default 40 MB
METADATA_EMOJI = os.getenv('METADATA_EMOJI', '🔎')
GUESS_EMOJI = os.getenv('GUESS_EMOJI', '❔')
GRADIO_BACKEND = os.getenv('GRADIO_BACKEND', "https://yoinked-da-nsfw-checker.hf.space/")
GRADCL = None  # Lazy initialization

def get_gradio_client():
    """Lazy initialization of Gradio client"""
    global GRADCL
    if GRADCL is None:
        try:
            GRADCL = gradio_client.Client(GRADIO_BACKEND)
        except Exception as e:
            logger.info(f"Failed to load Gradio API: {e}")
            GRADCL = None
    return GRADCL

def comfyui_get_data(dat):
    """Extract prompt/loras/checkpoints from comfy metadata"""
    try:
        aa = []
        dat = json.loads(dat)
        for _, value in dat.items():
            if value['class_type'] == "CLIPTextEncode":
                aa.append({"val": value['inputs']['text'][:1023],
                        "type": "prompt"})
            elif value['class_type'] == "CheckpointLoaderSimple":
                aa.append({"val": value['inputs']['ckpt_name'][:1023],
                        "type": "model"})
            elif value['class_type'] == "LoraLoader":
                aa.append({"val": value['inputs']['lora_name'][:1023],
                        "type": "lora"})
        return aa
    except ValueError as e:
        print(e)
        return []

def get_params_from_string(param_str):
    """Get parameters from old a1111 metadata"""
    output_dict = {}
    parts = param_str.split('Steps: ')
    prompts = parts[0]
    params = 'Steps: ' + parts[1]
    if 'Negative prompt: ' in prompts:
        output_dict['Prompt'] = prompts.split('Negative prompt: ')[0]
        output_dict['Negative Prompt'] = prompts.split('Negative prompt: ')[1]
        if len(output_dict['Negative Prompt']) > 1024:
            output_dict['Negative Prompt'] = output_dict['Negative Prompt'][:1020] + '...'
    else:
        output_dict['Prompt'] = prompts
    if len(output_dict['Prompt']) > 1024:
        output_dict['Prompt'] = output_dict['Prompt'][:1020] + '...'
    params = params.split(', ')
    for param in params:
        try:
            key, value = param.split(': ')
            output_dict[key] = value
        except ValueError:
            pass
    return output_dict

def get_embed(embed_dict, context: Message):
    """Create embed from dictionary"""
    embed = Embed(color=context.author.color)
    i = 0
    for key, value in embed_dict.items():
        if key.strip() == "" or value.strip() == "":
            continue
        i += 1
        if i >= 25:
            continue
        value = f"```\n{str(value)[:1000]}\n```"
        embed.add_field(name=key[:255], value=value[:1023], inline='Prompt' not in key)
    embed.set_footer(text=f'Posted by {context.author} - woof~', icon_url=context.author.display_avatar)
    return embed

async def read_info_from_image_stealth(image: Image.Image):
    """Read stealth PNGInfo"""
    width, height = image.size
    pixels = image.load()
    has_alpha = image.mode == "RGBA"
    mode = None
    compressed = False
    binary_data = []
    buffer_a = []
    buffer_rgb = []
    index_a = 0
    index_rgb = 0
    sig_confirmed = False
    confirming_signature = True
    reading_param_len = False
    reading_param = False
    read_end = False
    for x in range(width):
        for y in range(height):
            if has_alpha:
                r, g, b, a = pixels[x, y]
                buffer_a.append(str(a & 1))
                index_a += 1
            else:
                r, g, b = pixels[x, y]
            buffer_rgb.append(str(r & 1))
            buffer_rgb.append(str(g & 1))
            buffer_rgb.append(str(b & 1))
            index_rgb += 3
            
            # Yield control every 1000 pixels to prevent blocking
            if (x * height + y) % 1000 == 0:
                await asyncio.sleep(0)
            if confirming_signature:
                if index_a == len("stealth_pnginfo") * 8:
                    buffer_a_str = ''.join(buffer_a)
                    decoded_sig = bytearray(
                        int(buffer_a_str[i : i + 8], 2) for i in range(0, len(buffer_a_str), 8)
                    ).decode("utf-8", errors="ignore")
                    if decoded_sig in {"stealth_pnginfo", "stealth_pngcomp"}:
                        confirming_signature = False
                        sig_confirmed = True
                        reading_param_len = True
                        mode = "alpha"
                        if decoded_sig == "stealth_pngcomp":
                            compressed = True
                        buffer_a = [] # Reset to list
                        index_a = 0
                    else:
                        read_end = True
                        break
                elif index_rgb == len("stealth_pnginfo") * 8:
                    buffer_rgb_str = ''.join(buffer_rgb)
                    decoded_sig = bytearray(
                        int(buffer_rgb_str[i : i + 8], 2) for i in range(0, len(buffer_rgb_str), 8)
                    ).decode("utf-8", errors="ignore")
                    if decoded_sig in {"stealth_rgbinfo", "stealth_rgbcomp"}:
                        confirming_signature = False
                        sig_confirmed = True
                        reading_param_len = True
                        mode = "rgb"
                        if decoded_sig == "stealth_rgbcomp":
                            compressed = True
                        buffer_rgb = [] # Reset to list
                        index_rgb = 0
            elif reading_param_len:
                if mode == "alpha":
                    if index_a == 32:
                        param_len = int("".join(buffer_a), 2) # Join list before int conversion
                        reading_param_len = False
                        reading_param = True
                        buffer_a = [] # Reset to list
                        index_a = 0
                else:
                    if index_rgb == 33:
                        pop = buffer_rgb.pop() # Use pop() for list
                        param_len = int("".join(buffer_rgb), 2) # Join list before int conversion
                        reading_param_len = False
                        reading_param = True
                        buffer_rgb = [pop] # Reset to list containing the popped item
                        index_rgb = 1
            elif reading_param:
                if mode == "alpha":
                    if index_a == param_len:
                        binary_data = buffer_a
                        read_end = True
                        break
                else:
                    if index_rgb >= param_len:
                        diff = param_len - index_rgb
                        if diff < 0:
                            buffer_rgb = buffer_rgb[:diff]
                        binary_data = buffer_rgb
                        read_end = True
                        break
            else:
                read_end = True
                break
        if read_end:
            break
    if sig_confirmed and binary_data:
        binary_data_str = ''.join(binary_data)
        byte_data = bytearray(int(binary_data_str[i : i + 8], 2) for i in range(0, len(binary_data_str), 8))
        try:
            if compressed:
                decoded_data = gzip.decompress(bytes(byte_data)).decode("utf-8")
            else:
                decoded_data = byte_data.decode("utf-8", errors="ignore")
            return decoded_data
        except Exception as e:
            print(e)
    return None

class MetadataView(View):
    def __init__(self):
        super().__init__(timeout=3600)
        self.metadata = None

    @button(label='Full Parameters', style=ButtonStyle.green)
    async def details(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        await interaction.response.edit_message(view=self)
        if len(self.metadata) > 1980:
            with io.StringIO() as f:
                indented = json.dumps(json.loads(self.metadata), sort_keys=True, indent=2)
                f.write(indented)
                f.seek(0)
                await interaction.followup.send(file=File(f, "parameters.json"))
        else:
            await interaction.followup.send(f"```json\n{self.metadata}```")

async def read_attachment_metadata(i: int, attachment: Attachment, metadata: OrderedDict):
    """Download and read image metadata"""
    try:
        image_data = await attachment.read()
        with Image.open(io.BytesIO(image_data)) as img:
            if img.info:
                if 'parameters' in img.info:
                    info = img.info['parameters']
                elif 'prompt' in img.info:
                    info = img.info['prompt']
                elif 'Comment' in img.info:
                    info = img.info["Comment"]
                else:
                    info = comfyui_get_data(img.info)
            else:
                info = await read_info_from_image_stealth(img)
            if info:
                if info:
                    metadata[i] = info # Add to dict only if info was found
    except Exception as error:
        # Log the error more informatively
        print(f"Error reading metadata for attachment {i} ({attachment.filename}): {type(error).__name__}: {error}")
        print(traceback.format_exc()) # Print full traceback

class MetadataCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: Message):
        """Add magnifying glass if post has metadata"""
        if message.channel.id in settings.monitored_channels and message.attachments:
            attachments = [a for a in message.attachments 
                         if a.filename.lower().endswith(".png") 
                         and a.size < SCAN_LIMIT_BYTES]
            for i, attachment in enumerate(attachments):
                metadata = OrderedDict()
                await read_attachment_metadata(i, attachment, metadata)
                if metadata:
                    await message.add_reaction(METADATA_EMOJI)
                    return

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, ctx: RawReactionActionEvent):
        """Send metadata to user DMs when reacted"""
        if ctx.emoji.name not in [METADATA_EMOJI, GUESS_EMOJI] or ctx.channel_id not in settings.monitored_channels or ctx.member.bot:
            return
            
        channel = self.bot.get_channel(ctx.channel_id)
        message = await channel.fetch_message(ctx.message_id)
        if not message:
            return
            
        attachments = [a for a in message.attachments if a.filename.lower().endswith(".png")]
        if not attachments:
            return
            
        if ctx.emoji.name == GUESS_EMOJI:
            try:
                user_dm = await self.bot.get_user(ctx.user_id).create_dm()
                embed = Embed(title="Predicted Prompt", color=message.author.color)
                embed = embed.set_image(url=attachments[0].url)
                gradcl = get_gradio_client()
                if gradcl:
                    predicted = gradcl.predict(gradio_client.file(attachments[0].url),
                                           "chen-evangelion",
                                           0.45, True, True, api_name="/classify")[1]
                    predicted = f"```\n{predicted}\n```"
                    embed.add_field(name="DashSpace", value=predicted)
                    predicted = predicted.replace(" ", ",").replace("-", " ").replace(",", ", ")
                    embed.add_field(name="CommaSpace", value=predicted)
                else:
                    embed.add_field(name="Error", value="Gradio API not available")
                await user_dm.send(embed=embed)
            except Exception as e:
                print(e)
            return

        metadata = OrderedDict()
        tasks = [read_attachment_metadata(i, attachment, metadata) for i, attachment in enumerate(attachments)]
        await asyncio.gather(*tasks)
        if not metadata:
            return
            
        user_dm = await self.bot.get_user(ctx.user_id).create_dm()
        for attachment, data in [(attachments[i], data) for i, data in metadata.items()]:
            try:
                if 'Steps:' in data:
                    try:
                        params = get_params_from_string(data)
                        embed = get_embed(params, message)
                        embed.set_image(url=attachment.url)
                        custom_view = MetadataView()
                        custom_view.metadata = data
                        await user_dm.send(view=custom_view, embed=embed)
                    except Exception as e:
                        print(e)
                        txt = "## Metadata Error\nCould not parse the metadata. Here's the raw content:\n```json\n" + data + "\n```"
                        await user_dm.send(txt)
                else:
                    img_type = "ComfyUI" if "\"inputs\"" in data else "NovelAI"
                    i = 0
                    if img_type=="NovelAI":
                        x = json.loads(data)
                        if "sui_image_params" in x.keys():
                            t = x['sui_image_params'].copy()
                            del x['sui_image_params']
                            for key in t:
                                t[key] = str(t[key])
                            x = x|t
                            embed = Embed(title="Swarm Parameters", color=message.author.color)
                        else:
                            embed = Embed(title="Nai Parameters", color=message.author.color)
                        if "Comment" in x.keys():
                            t = x['Comment'].replace(r'\"', '"')
                            t = json.loads(t)
                            for key in t:
                                t[key] = str(t[key])
                            x = x | t
                            del x['Comment']
                            del x['Description']
                        for k in x.keys():
                            i += 1
                            if i >= 25:
                                continue
                            inline = False if 'prompt' in k else True
                            x[k] = f"```\n{str(x[k])[:1000]}\n```"
                            embed.add_field(name=k, value=str(x[k]), inline=inline)
                    else:
                        embed = Embed(title="ComfyUI Parameters", color=message.author.color)
                        for enum, dax in enumerate(comfyui_get_data(data)):
                            i += 1
                            if i >= 25:
                                continue
                            embed.add_field(name=f"{dax['type']} {enum+1} (beta)", value=dax['val'], inline=True)
                    embed.set_footer(text=f'Posted by {message.author}', icon_url=message.author.display_avatar)
                    embed.set_image(url=attachment.url)
                    with io.StringIO() as f:
                        indented = json.dumps(json.loads(data), sort_keys=True, indent=2)
                        f.write(indented)
                        f.seek(0)
                        att = await attachment.to_file()
                        await user_dm.send(embed=embed, files=[File(f, "parameters.json")])
            except Exception as e:
                print(data)
                print(e)

async def setup(bot):
    await bot.add_cog(MetadataCog(bot))
