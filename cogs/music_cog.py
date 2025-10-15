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
import json
import os
from pathlib import Path

class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.song_cache = None
        self.cache_timestamp = 0
        self.CACHE_DURATION = 300  # 5 minutes
        self.music_queues = {}  # guild_id -> list of song_info dicts
        self.now_playing = {}   # guild_id -> current song_info dict
        self.priority_queues = {}  # guild_id -> list of priority song_info dicts
        self.voice_check_task = None
        self.ALONE_TIMEOUT = 180  # 3 minutes in seconds
        self.EMPTY_CHANNEL_TIMEOUT = 30  # 30 seconds for empty channel
        self.CACHE_FILE = settings.SONGS_DIR / "song_cache.json"

        # Create initial cache on startup
        asyncio.create_task(self.initialize_cache())

    async def initialize_cache(self):
        """Initialize song cache on startup"""
        try:
            # Try to load existing cache first
            if not self.load_song_cache():
                # If no valid cache exists, build one
                import time
                current_time = time.time()
                self.song_cache = []
                self.cache_timestamp = current_time

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
                                'file_path': str(mp3_file),  # Convert Path to string for JSON
                                'title': title,
                                'artist': artist,
                                'display_name': display_name,
                                'duration': int(audio.info.length) if audio.info else 0
                            })
                        except Exception as e:
                            logger.warning(f"Could not read metadata for {mp3_file}: {e}")
                            # Fallback to filename
                            songs_with_metadata.append({
                                'file_path': str(mp3_file),
                                'title': mp3_file.stem,
                                'artist': 'Unknown Artist',
                                'display_name': f"{mp3_file.stem} - Unknown Artist",
                                'duration': 0
                            })

                    return songs_with_metadata

                self.song_cache = await asyncio.to_thread(get_song_metadata)
                self.cache_timestamp = current_time
                logger.info(f"Built initial song cache: {len(self.song_cache)} songs")

                # Save the initial cache
                self.save_song_cache()

        except Exception as e:
            logger.error(f"Failed to initialize song cache: {e}")
            self.song_cache = []

    @app_commands.command(name="play", description="Play a song from local library")
    # Song name using autocomplete
    @app_commands.describe(song_name="Name of the song to play")
    async def play(self, interaction: discord.Interaction, song_name: str):
        # Check if user is in voice channel
        if interaction.user.voice is None:
            await interaction.response.send_message("You are not connected to a voice channel.", delete_after=10)
            return

        # Check bot permissions for voice channel
        bot_member = interaction.guild.get_member(self.bot.user.id)
        if not interaction.user.voice.channel.permissions_for(bot_member).connect:
            await interaction.response.send_message("I don't have permission to connect to your voice channel.", delete_after=10)
            return
        if not interaction.user.voice.channel.permissions_for(bot_member).speak:
            await interaction.response.send_message("I don't have permission to speak in your voice channel.", delete_after=10)
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
            # Initialize queues for this guild if they don't exist
            guild_id = interaction.guild.id
            if guild_id not in self.priority_queues:
                self.priority_queues[guild_id] = []
            if guild_id not in self.music_queues:
                self.music_queues[guild_id] = []

            # Check if something is currently playing
            is_playing = interaction.guild.voice_client and interaction.guild.voice_client.is_playing()

            if is_playing:
                # Add to priority queue (user-requested songs get priority)
                self.priority_queues[guild_id].append(song_info)
                # Calculate position in combined queue
                total_priority = len(self.priority_queues[guild_id])
                total_regular = len(self.music_queues[guild_id])
                queue_position = total_priority + total_regular

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
                # Play immediately and ensure we have songs queued
                self.now_playing[guild_id] = song_info

                # Ensure we have at least 3 songs in the regular queue
                current_regular_count = len(self.music_queues.get(guild_id, []))
                if current_regular_count < 3:
                    songs_to_add = 3 - current_regular_count
                    await self.add_random_songs(guild_id, min_count=songs_to_add)

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

        # Update Discord Rich Presence (only for this server)
        try:
            activity = discord.Activity(
                type=discord.ActivityType.listening,
                name=song_info['title'],
                details=f"by {song_info['artist']}",
                state=f"Duration: {song_info['duration_str']}"
            )
            await self.bot.change_presence(activity=activity)
        except discord.Forbidden:
            # No permission to change presence, skip silently
            pass

        # Voice channel monitoring is now handled by event listeners

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

        # Check priority queue first (user-added songs)
        if guild_id in self.priority_queues and self.priority_queues[guild_id]:
            # Play next priority song
            next_song = self.priority_queues[guild_id].pop(0)
            self.now_playing[guild_id] = next_song
            await self.play_song(interaction, next_song, send_message=False)
            return

        # Check if there are songs in regular queue
        if guild_id in self.music_queues and self.music_queues[guild_id]:
            # Play next song without sending followup message
            next_song = self.music_queues[guild_id].pop(0)
            self.now_playing[guild_id] = next_song
            await self.play_song(interaction, next_song, send_message=False)
        else:
            # No more songs in regular queue, add random songs
            await self.add_random_songs(guild_id)

            # Try to play from the newly added random songs
            if guild_id in self.music_queues and self.music_queues[guild_id]:
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

                # Clear Rich Presence when no music is playing
                await self.bot.change_presence(activity=None)

    async def add_random_songs(self, guild_id: int, min_count: int = 3):
        """Add up to 3 random songs to the queue"""
        if not self.song_cache:
            return

        import random

        # Get songs not currently in queue or playing
        available_songs = []
        current_queue = self.music_queues.get(guild_id, [])
        priority_queue = self.priority_queues.get(guild_id, [])
        current_playing = self.now_playing.get(guild_id)

        # Create set of songs already in queues
        queued_song_paths = set()
        for song in current_queue + priority_queue:
            queued_song_paths.add(song['file_path'])
        if current_playing:
            queued_song_paths.add(current_playing['file_path'])

        # Filter available songs
        for song in self.song_cache:
            if song['file_path'] not in queued_song_paths:
                available_songs.append(song)

        if not available_songs:
            return

        # Select up to the minimum count (default 3) or available songs
        num_to_add = min(min_count, len(available_songs))
        random_songs = random.sample(available_songs, num_to_add)

        # Mark these songs as random for display purposes
        for song in random_songs:
            song['is_random'] = True

        # Initialize queue if needed
        if guild_id not in self.music_queues:
            self.music_queues[guild_id] = []

        # Add random songs to queue
        self.music_queues[guild_id].extend(random_songs)
        logger.info(f"Added {len(random_songs)} random songs to queue for guild {guild_id}")


    def load_song_cache(self):
        """Load song cache from file if it exists and is valid"""
        if self.CACHE_FILE.exists():
            try:
                with open(self.CACHE_FILE, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)

                # Check if cache is still valid (files haven't changed)
                cached_files = set(cache_data.get('files', []))
                current_files = set(str(f) for f in settings.SONGS_DIR.glob("**/*.mp3"))

                if cached_files == current_files:
                    self.song_cache = cache_data['songs']
                    self.cache_timestamp = cache_data.get('timestamp', 0)
                    logger.info(f"Loaded song cache from file: {len(self.song_cache)} songs")
                    return True
                else:
                    logger.info("Song files have changed, rebuilding cache")
            except Exception as e:
                logger.warning(f"Failed to load song cache: {e}")

        return False

    def save_song_cache(self):
        """Save current song cache to file"""
        try:
            cache_data = {
                'songs': self.song_cache,
                'timestamp': self.cache_timestamp,
                'files': [str(f) for f in settings.SONGS_DIR.glob("**/*.mp3")]
            }

            # Ensure directory exists
            self.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

            with open(self.CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)

            logger.info(f"Saved song cache to file: {len(self.song_cache)} songs")
        except Exception as e:
            logger.error(f"Failed to save song cache: {e}")

    # Song name autocomplete function
    @play.autocomplete("song_name")
    async def song_name_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        import time

        # Check if we need to refresh cache
        current_time = time.time()
        if self.song_cache is None:
            # Try to load from file first
            if not self.load_song_cache():
                # File doesn't exist or is invalid, build cache
                self.song_cache = []
                self.cache_timestamp = current_time

        if (current_time - self.cache_timestamp) > self.CACHE_DURATION:
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
                            'file_path': str(mp3_file),  # Convert Path to string for JSON
                            'title': title,
                            'artist': artist,
                            'display_name': display_name,
                            'duration': int(audio.info.length) if audio.info else 0
                        })
                    except Exception as e:
                        logger.warning(f"Could not read metadata for {mp3_file}: {e}")
                        # Fallback to filename
                        songs_with_metadata.append({
                            'file_path': str(mp3_file),
                            'title': mp3_file.stem,
                            'artist': 'Unknown Artist',
                            'display_name': f"{mp3_file.stem} - Unknown Artist",
                            'duration': 0
                        })

                return songs_with_metadata

            self.song_cache = await asyncio.to_thread(get_song_metadata)
            self.cache_timestamp = current_time
            logger.info(f"Cached {len(self.song_cache)} songs")

            # Save cache to file
            self.save_song_cache()

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

        # Show priority queue (user-requested songs) first
        priority_count = len(self.priority_queues.get(guild_id, []))
        regular_count = len(self.music_queues.get(guild_id, []))
        total_count = priority_count + regular_count

        full_container.add_item(TextDisplay(f"## 📋 Queue • {total_count} songs"))

        if total_count > 0:
            # Show priority queue items first
            if priority_count > 0:
                full_container.add_item(TextDisplay("### ⭐ Priority Queue:"))
                for i, song in enumerate(self.priority_queues[guild_id][:5], 1):  # Show first 5 priority
                    full_container.add_item(TextDisplay(f"⭐ {i}. {song['title']} - {song['artist']}\n"))

                if priority_count > 5:
                    full_container.add_item(TextDisplay(f"⭐ ... and {priority_count - 5} more priority songs"))

            # Show regular queue items
            if regular_count > 0:
                if priority_count > 0:
                    full_container.add_item(TextDisplay("### 🎲 Regular Queue:"))

                # Calculate starting index for regular queue display
                start_index = 1
                if priority_count > 0:
                    start_index = priority_count + 1

                # Show up to 5 more regular queue items (total display limit consideration)
                display_limit = 10 - min(priority_count, 5)  # Leave room for priority items
                regular_display = self.music_queues[guild_id][:display_limit]

                for i, song in enumerate(regular_display, start_index):
                    queue_type = "🎲" if song.get('is_random', False) else "➡️"
                    full_container.add_item(TextDisplay(f"{queue_type} {i}. {song['title']} - {song['artist']}\n"))

                remaining_regular = regular_count - len(regular_display)
                if remaining_regular > 0:
                    full_container.add_item(TextDisplay(f"➡️ ... and {remaining_regular} more songs"))
        else:
            full_container.add_item(TextDisplay("No songs in queue"))

        # Send the view with album art file if it exists
        if 'album_art_file' in locals() and album_art_file is not None:
            await interaction.followup.send(view=view, file=album_art_file)
        else:
            await interaction.followup.send(view=view)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Handle voice channel state changes for music management"""
        # Ignore bot state changes
        if member.bot:
            return

        guild = member.guild
        voice_client = guild.voice_client

        # Only care if bot is connected to voice
        if not voice_client or not voice_client.is_connected():
            return

        channel = voice_client.channel

        # Check if someone joined/left our channel
        if before.channel == channel or after.channel == channel:
            # Count non-bot members in the channel
            human_members = [m for m in channel.members if not m.bot]

            if len(human_members) == 0:
                # Channel is now empty, pause music if playing
                if voice_client.is_playing():
                    voice_client.pause()
                    logger.info(f"Paused music in {guild.name} - channel is empty")

                # Start alone timer if not already started
                if not hasattr(voice_client, 'alone_since'):
                    voice_client.alone_since = asyncio.get_event_loop().time()
                    # Schedule disconnect check
                    asyncio.create_task(self.schedule_disconnect(guild, voice_client))

                # Update presence to show disconnect countdown immediately
                try:
                    await self.update_alone_presence(guild)
                except discord.Forbidden:
                    # No permission to change presence, skip silently
                    pass
                except Exception as e:
                    logger.warning(f"Failed to update alone presence: {e}")

            elif len(human_members) > 0:
                # People are back - always resume and restore normal presence
                was_alone = hasattr(voice_client, 'alone_since')

                if voice_client.is_paused():
                    voice_client.resume()
                    logger.info(f"Resumed music in {guild.name} - people returned")

                # Reset alone timer
                if hasattr(voice_client, 'alone_since'):
                    delattr(voice_client, 'alone_since')

                # Always restore normal music presence when people return
                try:
                    if guild.id in self.now_playing:
                        song_info = self.now_playing[guild.id]
                        activity = discord.Activity(
                            type=discord.ActivityType.listening,
                            name=song_info['title'],
                            details=f"by {song_info['artist']}",
                            state=f"Duration: {song_info.get('duration_str', 'Unknown')}"
                        )
                        await self.bot.change_presence(activity=activity)
                    elif was_alone:
                        # If we were alone but no music is playing, clear presence
                        await self.bot.change_presence(activity=None)
                except discord.Forbidden:
                    # No permission to change presence, skip silently
                    pass
                except Exception as e:
                    logger.warning(f"Failed to update presence on return: {e}")

    async def update_alone_presence(self, guild: discord.Guild):
        """Update Rich Presence to show disconnect countdown when alone"""
        try:
            voice_client = guild.voice_client
            if not voice_client or not hasattr(voice_client, 'alone_since'):
                return

            alone_time = asyncio.get_event_loop().time() - voice_client.alone_since
            remaining_time = max(0, self.ALONE_TIMEOUT - alone_time)

            if remaining_time > 0:
                minutes = int(remaining_time // 60)
                seconds = int(remaining_time % 60)

                activity = discord.Activity(
                    type=discord.ActivityType.listening,
                    name="⏸️ Paused - Alone in VC",
                    details=f"Disconnecting in {minutes}:{seconds:02d}",
                    state=f"🎵 {guild.name}"
                )
                await self.bot.change_presence(activity=activity)

        except Exception as e:
            logger.error(f"Error updating alone presence: {e}")

    async def schedule_disconnect(self, guild: discord.Guild, voice_client):
        """Schedule automatic disconnect after alone timeout"""
        try:
            # Update presence periodically while alone
            update_interval = 5  # Update every 5 seconds
            updates_remaining = self.ALONE_TIMEOUT // update_interval

            for _ in range(updates_remaining):
                if not hasattr(voice_client, 'alone_since'):
                    # Timer was cancelled
                    return
                await self.update_alone_presence(guild)
                await asyncio.sleep(update_interval)

            # Final check - full timeout reached
            if (hasattr(voice_client, 'alone_since') and
                voice_client.is_connected() and
                len([m for m in voice_client.channel.members if not m.bot]) == 0):

                await voice_client.disconnect()
                logger.info(f"Disconnected from {guild.name} - alone for {self.ALONE_TIMEOUT} seconds")

                # Clear Rich Presence
                await self.bot.change_presence(activity=None)

                # Clear now playing for this guild
                if guild.id in self.now_playing:
                    del self.now_playing[guild.id]

        except Exception as e:
            logger.error(f"Error in scheduled disconnect: {e}")

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
