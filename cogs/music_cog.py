import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import LayoutView, TextDisplay, Section, ActionRow, Button, Container, Separator
import settings
import asyncio
from settings import logger
from mutagen.mp3 import MP3
from mutagen.id3 import ID3
from io import BytesIO

class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.song_cache = None
        self.cache_timestamp = 0
        self.CACHE_DURATION = 300  # 5 minutes
        self.music_queues = {}  # guild_id -> list of song_info dicts
        self.now_playing = {}   # guild_id -> current song_info dict

    @app_commands.command(name="play", description="Play a song from local library")
    # Song name using autocomplete
    @app_commands.describe(song_name="Name of the song to play")
    async def play(self, interaction: discord.Interaction, song_name: str):
        # Check if user is in voice channel
        if interaction.user.voice is None:
            await interaction.response.send_message("You are not connected to a voice channel.", delete_after=10)
            return
        
        # Verify the song exists in cache
        song_exists = any(song['display_name'] == song_name for song in self.song_cache) if self.song_cache else False
        if not song_exists:
            await interaction.response.send_message(f"Song `{song_name}` not found in library.", delete_after=10)
            return

        # Check if bot is already in a different voice channel
        if interaction.guild.voice_client is not None:
            if interaction.guild.voice_client.channel != interaction.user.voice.channel:
                await interaction.response.send_message("I'm already connected to a different voice channel.", delete_after=10)
                return
            # Bot is already in the same channel, proceed
        else:
            # Bot not connected, join the channel
            await interaction.user.voice.channel.connect()
        await interaction.response.defer()

        # Create music player interface using LayoutView
        view = LayoutView()

        # Get song info from cache
        song_info = None
        for song in self.song_cache:
            if song['display_name'] == song_name:
                song_info = song
                break

        if song_info:
            # Initialize queue for this guild if it doesn't exist
            guild_id = interaction.guild.id
            if guild_id not in self.music_queues:
                self.music_queues[guild_id] = []

            # Check if something is currently playing
            is_playing = interaction.guild.voice_client and interaction.guild.voice_client.is_playing()

            if is_playing:
                # Add to queue
                self.music_queues[guild_id].append(song_info)
                queue_position = len(self.music_queues[guild_id])

                # Create queue addition interface using LayoutView
                view = LayoutView()
                queue_container = Container()

                # Try to get album art for the queued song
                album_art = None
                album_art_file = None
                # Convert duration in seconds to minutes:seconds for queue display
                duration_str = "Unknown"
                try:
                    audio = MP3(song_info['file_path'])
                    duration_seconds = int(audio.info.length)
                    minutes = duration_seconds // 60
                    seconds = duration_seconds % 60
                    duration_str = f"{minutes}:{seconds:02d}"
                except Exception as e:
                    logger.warning(f"Could not get duration for queue: {e}")

                try:
                    audio = MP3(song_info['file_path'], ID3=ID3)
                    if 'APIC:' in audio:
                        album_art_data = audio['APIC:'].data
                        album_art_file = discord.File(BytesIO(album_art_data), filename="album_art.jpg")
                        album_art = discord.ui.Thumbnail(media=album_art_file)
                except Exception as e:
                    logger.warning(f"Could not extract album art for queue: {e}")

                queue_container.add_item(Section(
                    TextDisplay(f"🎵 Added to Queue • Position #{queue_position}"),
                    TextDisplay(f"**{song_info['title']}**"),
                    TextDisplay(f"👤 {song_info['artist']} ({duration_str})"),
                    accessory=album_art
                ))
                view.add_item(queue_container)

                # Send the view with album art file if it exists
                if album_art_file is not None:
                    await interaction.followup.send(view=view, file=album_art_file)
                else:
                    await interaction.followup.send(view=view)
            else:
                # Play immediately
                self.now_playing[guild_id] = song_info
                await self.play_song(interaction, song_info)
        else:
            await interaction.followup.send(f"Could not load song info for: {song_name}")

    async def play_song(self, interaction: discord.Interaction, song_info: dict, send_message: bool = True):
        """Play a song and set up the music interface"""
        # Try to get album art
        album_art = None
        album_art_file = None
        # Convert duration in seconds to minutes:seconds
        if 'duration' in song_info and song_info['duration']:
            minutes = song_info['duration'] // 60
            seconds = song_info['duration'] % 60
            song_info['duration_str'] = f"{minutes}:{seconds:02d}"
        else:
            song_info['duration_str'] = "Unknown"
        try:
            audio = MP3(song_info['file_path'], ID3=ID3)
            if 'APIC:' in audio:
                album_art_data = audio['APIC:'].data
                # Create file and send it with the view
                album_art_file = discord.File(BytesIO(album_art_data), filename="album_art.jpg")
                album_art = discord.ui.Thumbnail(media=album_art_file)
        except Exception as e:
            logger.warning(f"Could not extract album art: {e}")

        # Create music player interface using LayoutView
        view = LayoutView()

        # Create a container for the song info section
        song_container = Container()
        song_container.add_item(Section(
            TextDisplay(f"# 🎵 Now Playing 🎵"),
            TextDisplay(f"### {song_info['title']}"),
            TextDisplay(f"👤 {song_info['artist']} ({song_info['duration_str']})"),
            accessory=album_art
        ))
        view.add_item(song_container)

        # Send the view with the album art file if it exists (only if send_message is True)
        if send_message:
            if album_art_file is not None:
                await interaction.followup.send(view=view, file=album_art_file)
            else:
                await interaction.followup.send(view=view)

        # Play the song
        try:
            source = discord.FFmpegPCMAudio(str(song_info['file_path']))
            interaction.guild.voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
                self.on_song_end(interaction), self.bot.loop
            ))
        except Exception as e:
            logger.error(f"Error playing song: {e}")
            await interaction.followup.send("An error occurred while trying to play the song.", delete_after=10)

    async def on_song_end(self, interaction: discord.Interaction):
        """Called when a song finishes playing"""
        guild_id = interaction.guild.id

        # Check if there are songs in queue
        if guild_id in self.music_queues and self.music_queues[guild_id]:
            # Play next song without sending followup message
            next_song = self.music_queues[guild_id].pop(0)
            self.now_playing[guild_id] = next_song
            await self.play_song(interaction, next_song, send_message=False)
        else:
            # No more songs, clear now playing and disconnect from voice channel
            if guild_id in self.now_playing:
                del self.now_playing[guild_id]

            # Disconnect from voice channel after a short delay
            if interaction.guild.voice_client:
                await asyncio.sleep(1)  # Brief pause before disconnecting
                await interaction.guild.voice_client.disconnect()
                #logger.info(f"Disconnected from voice channel in guild {guild_id} - no more songs in queue")


    # Song name autocomplete function
    @play.autocomplete("song_name")
    async def song_name_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        import time

        # Check if we need to refresh cache
        current_time = time.time()
        if self.song_cache is None or (current_time - self.cache_timestamp) > self.CACHE_DURATION:
            def get_song_metadata():
                songs_with_metadata = []
                for mp3_file in settings.SONGS_DIR.glob("**/*.mp3"):
                    try:
                        audio = MP3(mp3_file, ID3=ID3)
                        title = audio.get('TIT2', mp3_file.stem).text[0] if audio.get('TIT2') else mp3_file.stem
                        artist = audio.get('TPE1', 'Unknown Artist').text[0] if audio.get('TPE1') else 'Unknown Artist'

                        # Create display name with artist
                        display_name = f"{title} - {artist}"
                        songs_with_metadata.append({
                            'file_path': mp3_file,
                            'title': title,
                            'artist': artist,
                            'display_name': display_name,
                            'duration': int(audio.info.length) if audio.info else 0
                        })
                    except Exception as e:
                        logger.warning(f"Could not read metadata for {mp3_file}: {e}")
                        # Fallback to filename
                        songs_with_metadata.append({
                            'file_path': mp3_file,
                            'title': mp3_file.stem,
                            'artist': 'Unknown Artist',
                            'display_name': f"{mp3_file.stem} - Unknown Artist"
                        })

                return songs_with_metadata

            self.song_cache = await asyncio.to_thread(get_song_metadata)
            self.cache_timestamp = current_time
            logger.info(f"Cached {len(self.song_cache)} songs")

        def search_current_term(query):
            songs_data = self.song_cache
            current_term = query.strip().lower()

            if not current_term:
                # Return first 25 songs with metadata
                return [song['display_name'] for song in songs_data[:25]]

            results = [
                song['display_name'] for song in songs_data
                if current_term in song['title'].lower() or
                   current_term in song['artist'].lower() or
                   current_term in song['display_name'].lower()
            ]

            return results[:25]  # Limit to 25 results as per Discord's limit

        results = await asyncio.to_thread(search_current_term, current)

        valid_choices = [
            app_commands.Choice(name=item[:100], value=item[:100])
            for item in results
            if len(item) > 0  # Ensure item is not empty
        ]
        return valid_choices


    @app_commands.command(name="queue", description="Show the current music queue")
    async def queue(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id = interaction.guild.id

        # Create music player interface using LayoutView
        view = LayoutView()

        # Check if there's a current song playing
        if guild_id in self.now_playing:
            song_info = self.now_playing[guild_id]

            # Try to get album art
            album_art = None
            album_art_file = None
            try:
                audio = MP3(song_info['file_path'], ID3=ID3)
                if 'APIC:' in audio:
                    album_art_data = audio['APIC:'].data
                    album_art_file = discord.File(BytesIO(album_art_data), filename="album_art.jpg")
                    album_art = discord.ui.Thumbnail(media=album_art_file)
            except Exception as e:
                logger.warning(f"Could not extract album art: {e}")

            # Create container for now playing section
            full_container = Container()
            full_container.add_item(Section(
                TextDisplay(f"# 🎵 Now Playing 🎵"),
                TextDisplay(f"### {song_info['title']}"),
                TextDisplay(f"👤 {song_info['artist']} ({song_info.get('duration_str', 'Unknown')})"),
                accessory=album_art
            ))
            view.add_item(full_container)

        full_container.add_item(Separator())
        full_container.add_item(TextDisplay(f"## 📋 Queue • {len(self.music_queues[guild_id])} songs"))
        if guild_id in self.music_queues and self.music_queues[guild_id]:
            for i, song in enumerate(self.music_queues[guild_id][:10], 1):  # Show first 10
                full_container.add_item(TextDisplay(f"{i}. {song['title']} - {song['artist']}\n"))

            if len(self.music_queues[guild_id]) > 10:
                full_container.add_item(TextDisplay(f"... and {len(self.music_queues[guild_id]) - 10} more songs"))

        else:
            full_container.add_item(TextDisplay("No songs in queue"))

        # Send the view with album art file if it exists
        if 'album_art_file' in locals() and album_art_file is not None:
            await interaction.followup.send(view=view, file=album_art_file)
        else:
            await interaction.followup.send(view=view)

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        if interaction.user.voice is None:
            await interaction.response.send_message("You are not connected to a voice channel.", ephemeral=True)
            return

        if interaction.guild.voice_client is None:
            await interaction.response.send_message("I'm not currently playing music.", ephemeral=True)
            return

        if not interaction.guild.voice_client.is_playing():
            await interaction.response.send_message("No song is currently playing.", ephemeral=True)
            return

        # Stop current song (this will trigger on_song_end)
        interaction.guild.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped current song!", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
