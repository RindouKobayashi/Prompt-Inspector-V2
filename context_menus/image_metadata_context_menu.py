import discord
from discord.ext import commands
import settings
from settings import logger
from cogs.metadata_cog import read_attachment_metadata, get_params_from_string, get_embed, comfyui_get_data
from collections import OrderedDict
import json
import asyncio
from io import StringIO
from discord import app_commands

logger.info("Image Metadata Context Menu Commands Loaded")

def setup_contextmenu(bot: commands.Bot):
    @app_commands.user_install
    @bot.tree.context_menu(name="View Raw Prompt")
    async def view_raw_prompt(interaction: discord.Interaction, message: discord.Message):
        """Show raw prompt metadata from image"""
        await handle_image_metadata(interaction, message, format="raw")

    @app_commands.user_install
    @bot.tree.context_menu(name="View Parameters/Prompt") 
    async def view_formatted_prompt(interaction: discord.Interaction, message: discord.Message):
        """Show formatted prompt parameters from image"""
        await handle_image_metadata(interaction, message, format="formatted")

async def handle_image_metadata(interaction: discord.Interaction, message: discord.Message, format: str):
    attachments = [a for a in message.attachments if a.filename.lower().endswith(".png")]
    if not attachments:
        await interaction.response.send_message("No PNG images found", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    metadata = OrderedDict()
    tasks = [read_attachment_metadata(i, a, metadata) for i, a in enumerate(attachments)]
    await asyncio.gather(*tasks)

    if not metadata:
        await interaction.followup.send("No metadata found in images", ephemeral=True)
        return

    attachment = attachments[0]
    data = metadata[0]

    if format == "raw":
        formatted = json.dumps(data, indent=2)
        if len(formatted) < 1900:
            await interaction.followup.send(f"```json\n{formatted}```", ephemeral=True)
        else:
            with StringIO() as f:
                f.write(formatted)
                f.seek(0)
                await interaction.followup.send(file=discord.File(f, "raw_prompt.json"))

        # Forward raw data to logging channel
        try:
            channel = interaction.client.get_channel(1257736917374730260)
            if channel:
                forward_embed = discord.Embed(
                        title="Prompt Inspection",
                        description=f"Message from {message.author.mention} in {message.channel.mention}",
                        color=message.author.color
                )
                forward_embed.add_field(name="Requested by", value=interaction.user.mention, inline=True)
                forward_embed.set_image(url=attachment.url)
                forward_message = await channel.send(embed=forward_embed)
                
                if len(formatted) < 1900:
                    await forward_message.reply(f"```json\n{formatted}```")
                else:
                    with StringIO() as f:
                        f.write(formatted)
                        f.seek(0)
                        await forward_message.reply(file=discord.File(f, "raw_prompt_log.json"))
        except Exception as e:
            logger.error(f"Error forwarding raw prompt: {e}")
    else:
        try:
            embed = format_metadata_embed(data, message, attachment)
            await interaction.followup.send(embed=embed)
            
            # Forward message to logging channel
            try:
                channel = interaction.client.get_channel(1257736917374730260)
                if channel:
                    forward_message = await channel.send(f"Prompt Inspection for {message.author.mention} in {message.channel.mention}\nRequested by {interaction.user.mention}", allowed_mentions=discord.AllowedMentions.none())
                    await forward_message.reply(embed=embed)
            except Exception as e:
                logger.error(f"Error forwarding message: {e}")
        except Exception as e:
            logger.error(f"Error formatting metadata: {e}")
            await interaction.followup.send(f"Error processing metadata:\n```{str(data)[:1900]}```", ephemeral=True)

def format_metadata_embed(data, message, attachment):
    if 'Steps:' in data:
        params = get_params_from_string(data)
        embed = get_embed(params, message)
    else:
        embed = discord.Embed(color=message.author.color)
        if "\"inputs\"" in data:  # ComfyUI
            embed.title = "ComfyUI Parameters"
            for i, d in enumerate(comfyui_get_data(data)):
                embed.add_field(name=f"{d['type']} {i+1}", value=d['val'][:1024], inline=True)
        else:  # NovelAI/Swarm - Use logic similar to on_raw_reaction_add
            x = json.loads(data)
            if "sui_image_params" in x.keys():
                t = x['sui_image_params'].copy()
                del x['sui_image_params']
                for key in t:
                    t[key] = str(t[key])
                x = x|t
                embed.title = "Swarm Parameters"
            else:
                embed.title = "Nai Parameters"
            if "Comment" in x.keys():
                try: # Add try-except for Comment parsing
                    t = x['Comment'].replace(r'\"', '"')
                    t = json.loads(t)
                    for key in t:
                        t[key] = str(t[key])
                    x = x | t
                    del x['Comment']
                    if 'Description' in x: # Remove Description only if Comment was successfully parsed
                        del x['Description']
                except json.JSONDecodeError:
                    logger.warning("Could not parse 'Comment' field as JSON.")
                    # Keep original Comment field if parsing fails
            
            i = 0
            for k in x.keys():
                i += 1
                if i >= 25: # Limit fields
                    embed.add_field(name="...", value="Too many fields to display.", inline=False)
                    break
                inline = 'prompt' not in k.lower() # Case-insensitive check for prompt
                value_str = str(x[k])
                if len(value_str) > 1000:
                    value_str = value_str[:997] + '...'
                embed.add_field(name=k, value=f"```\n{value_str}\n```", inline=inline)
    
    embed.set_image(url=attachment.url)
    embed.set_footer(text=f"Posted by {message.author}", icon_url=message.author.display_avatar)
    return embed
