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
import tempfile
from pathlib import Path
import soundfile as sf
import numpy as np

# Set local cache directory for Kokoro models before importing
KOKORO_CACHE_DIR = Path(__file__).parent.parent / "cogs" / "kokoro_models"
KOKORO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
os.environ['HF_HUB_CACHE'] = str(KOKORO_CACHE_DIR)
os.environ['HF_HOME'] = str(KOKORO_CACHE_DIR)
os.environ['XDG_CACHE_HOME'] = str(KOKORO_CACHE_DIR)

from kokoro import KPipeline

# Override the default cache location in huggingface_hub
import huggingface_hub
huggingface_hub.constants.HF_HUB_CACHE = str(KOKORO_CACHE_DIR)
huggingface_hub.constants.HF_HOME = str(KOKORO_CACHE_DIR)

# Also try to override any existing cache
try:
    import transformers
    transformers.utils.hub.HF_HUB_CACHE = str(KOKORO_CACHE_DIR)
    transformers.utils.hub.HF_HOME = str(KOKORO_CACHE_DIR)
except ImportError:
    pass

# Music cog configuration
SONGS_DIR = Path(__file__).parent.parent / "songs"

class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.song_cache = None
        self.cache_timestamp = 0
        self.CACHE_DURATION = 300  # 5 minutes
        self.music_queues = {}  # guild_id -> list of song_info dicts
        self.now_playing = {}   # guild_id -> current song_info dict
        self.priority_queues = {}  # guild_id -> list of priority song_info dicts
        self.pause_states = {}  # guild_id -> bool (True if paused)
        self.loop_modes = {}   # guild_id -> str ('off', 'single', 'queue')
        self.voice_check_task = None
        self.ALONE_TIMEOUT = 180  # 3 minutes in seconds
        self.EMPTY_CHANNEL_TIMEOUT = 30  # 30 seconds for empty channel
        self.CACHE_FILE = SONGS_DIR / "song_cache.json"
        self.STATS_FILE = SONGS_DIR / "song_stats.json"
        self.song_stats = {}  # file_path -> stats dict
        self.current_play_start = {}  # guild_id -> timestamp when current song started
        self.skip_in_progress = {}  # guild_id -> bool (True if song is being skipped)
        self.stop_in_progress = {}  # guild_id -> bool (True if stop operation is in progress)
        self.current_queue_type = {}  # guild_id -> str ('priority' or 'regular') for currently playing song

        # TTS Configuration
        self.tts_enabled = True  # Enable/disable TTS announcements
        self.tts_temp_dir = Path(__file__).parent.parent / "temp"  # Project temp directory for audio files
        self.tts_temp_dir.mkdir(parents=True, exist_ok=True)

        # Clean up any leftover temp files on startup
        asyncio.create_task(self.cleanup_temp_files())

        # Create initial cache on startup
        asyncio.create_task(self.initialize_cache())
        asyncio.create_task(self.load_song_stats())

    async def cleanup_temp_files(self):
        """Clean up any leftover TTS temp files on startup"""
        try:
            if self.tts_temp_dir.exists():
                for temp_file in self.tts_temp_dir.glob("tts_*.wav"):
                    try:
                        temp_file.unlink()
                        logger.info(f"Cleaned up leftover temp file: {temp_file.name}")
                    except Exception as e:
                        logger.warning(f"Failed to clean up temp file {temp_file}: {e}")
        except Exception as e:
            logger.error(f"Error during temp file cleanup: {e}")

    def clean_text_for_tts(self, text: str) -> str:
        """Clean text for TTS by removing non-ASCII characters and replacing abbreviations"""
        import re

        # Replace "feat." with "feature" (more flexible pattern)
        text = re.sub(r'feat\.', 'feature', text, flags=re.IGNORECASE)

        # Keep only ASCII characters (removes Japanese, Chinese, etc.)
        # This preserves parentheses, brackets, and other punctuation that are part of song titles
        text = re.sub(r'[^\x00-\x7F]+', '', text)

        # Clean up extra whitespace and normalize spaces
        text = re.sub(r'\s+', ' ', text).strip()

        return text

    async def tts_speak(self, guild: discord.Guild, text: str, voice_name: str = 'af_heart'):
        """Generate and play TTS audio using Kokoro"""
        if not self.tts_enabled:
            return

        try:
            # Initialize Kokoro pipeline (create once and reuse)
            if not hasattr(self, 'tts_pipeline'):
                self.tts_pipeline = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')  # American English

            # Generate TTS audio
            generator = self.tts_pipeline(
                text=text,
                voice=voice_name,
                speed=1.0
            )

            # Collect all audio segments
            audio_segments = []
            for i, (gs, ps, audio) in enumerate(generator):
                audio_segments.append(audio)

            if not audio_segments:
                logger.warning("No audio generated by TTS")
                return

            # Concatenate all audio segments
            if len(audio_segments) == 1:
                final_audio = audio_segments[0]
            else:
                final_audio = np.concatenate(audio_segments)

            # Create temporary file for the audio
            import uuid
            temp_filename = f"tts_{uuid.uuid4().hex}.wav"
            temp_path = os.path.join(self.tts_temp_dir, temp_filename)

            # Save audio to temporary file
            sf.write(temp_path, final_audio, 24000)

            # Play the TTS audio
            voice_client = guild.voice_client
            if voice_client and voice_client.is_connected():
                # Create audio source from the temp file
                tts_source = discord.FFmpegPCMAudio(temp_path)

                # Play TTS (this will interrupt current music if playing)
                voice_client.play(tts_source)

                # Wait for TTS to finish, then clean up
                while voice_client.is_playing():
                    await asyncio.sleep(0.1)

                # Clean up temp file
                try:
                    os.remove(temp_path)
                except Exception as e:
                    logger.warning(f"Failed to clean up TTS temp file {temp_path}: {e}")

        except Exception as e:
            logger.error(f"TTS error: {e}")
            # Clean up any temp files on error
            try:
                if 'temp_path' in locals() and os.path.exists(temp_path):
                    os.remove(temp_path)
            except:
                pass

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
                    for mp3_file in SONGS_DIR.glob("**/*.mp3"):
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
                # Update stats for priority queue addition
                self.update_song_stats(song_info['file_path'], event_type='queued', queue_type='priority', user_id=str(interaction.user.id))
                # Calculate position in combined queue (priority songs play first)
                queue_position = len(self.priority_queues[guild_id])

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

                await self.play_song(interaction, song_info, queue_type='priority', user_id=str(interaction.user.id))
        else:
            await interaction.followup.send(f"Could not load song info for: {song_name}")

    async def play_song(self, interaction: discord.Interaction, song_info: dict, send_message: bool = True, queue_type: str = 'regular', user_id: str = None):
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

        # Update voice channel status if bot has permission
        try:
            voice_channel = interaction.guild.voice_client.channel
            if voice_channel.permissions_for(interaction.guild.get_member(self.bot.user.id)).manage_channels:
                status_text = f"🎵 {song_info['title']} - {song_info['artist']}"
                await voice_channel.edit(status=status_text)
        except (discord.Forbidden, AttributeError):
            # No permission to edit channel status or no voice channel, skip silently
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

        # Record play start time and update stats for song start
        guild_id = interaction.guild.id
        self.current_play_start[guild_id] = asyncio.get_event_loop().time()
        self.current_queue_type[guild_id] = queue_type
        self.update_song_stats(song_info['file_path'], event_type='started', queue_type=queue_type, user_id=user_id)

        # Announce the song with TTS first
        if self.tts_enabled:
            announcement_text = f"Now playing {song_info['title']} by {song_info['artist']}"
            # Clean the announcement text for TTS
            announcement_text = self.clean_text_for_tts(announcement_text)
            logger.info(f"TTS Announcement: '{announcement_text}'")
            await self.tts_speak(interaction.guild, announcement_text)

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

        # Check if bot is still connected to voice - if not, don't try to play next song
        if not interaction.guild.voice_client or not interaction.guild.voice_client.is_connected():
            # Record play duration for the song that just finished
            if guild_id in self.now_playing and guild_id in self.current_play_start:
                song_info = self.now_playing[guild_id]
                play_duration = asyncio.get_event_loop().time() - self.current_play_start[guild_id]

                # Always record play duration (even for skipped songs)
                self.record_play_duration(song_info['file_path'], play_duration)

                # Record appropriate event type
                current_type = self.current_queue_type.get(guild_id, 'regular')
                if not self.skip_in_progress.get(guild_id, False):
                    # Record completion event
                    self.update_song_stats(song_info['file_path'], event_type='completed', queue_type=current_type)
                else:
                    # Record skip event
                    self.update_song_stats(song_info['file_path'], event_type='skipped', queue_type=current_type)

                    del self.current_play_start[guild_id]
                    # Clear skip flag
                    if guild_id in self.skip_in_progress:
                        del self.skip_in_progress[guild_id]

            # Clear now playing for this guild since we're disconnected
            if guild_id in self.now_playing:
                del self.now_playing[guild_id]
            # Clear Rich Presence when no music is playing
            await self.bot.change_presence(activity=None)
            return

        # Record play duration for the song that just finished (always record duration and session)
        if guild_id in self.now_playing and guild_id in self.current_play_start:
            song_info = self.now_playing[guild_id]
            play_duration = asyncio.get_event_loop().time() - self.current_play_start[guild_id]

            # Always record play duration and session (even for skipped songs)
            self.record_play_duration(song_info['file_path'], play_duration)

            # Record appropriate event type
            current_type = self.current_queue_type.get(guild_id, 'regular')
            if not self.skip_in_progress.get(guild_id, False):
                # Record completion event
                self.update_song_stats(song_info['file_path'], event_type='completed', queue_type=current_type)
            else:
                # Record skip event
                self.update_song_stats(song_info['file_path'], event_type='skipped', queue_type=current_type)

            del self.current_play_start[guild_id]
            # Clear skip flag
            if guild_id in self.skip_in_progress:
                del self.skip_in_progress[guild_id]

        # Check if stop operation is in progress - if so, don't play next song
        if self.stop_in_progress.get(guild_id, False):
            # Clear stop flag since we're handling it now
            if guild_id in self.stop_in_progress:
                del self.stop_in_progress[guild_id]
            return

        # Check loop mode first
        current_loop_mode = self.loop_modes.get(guild_id, 'off')

        if current_loop_mode == 'single' and guild_id in self.now_playing:
            # Loop current song
            current_song = self.now_playing[guild_id]
            await self.play_song(interaction, current_song, send_message=False)
            return

        elif current_loop_mode == 'queue':
            # Loop entire queue - add current song back to end of regular queue
            if guild_id in self.now_playing:
                current_song = self.now_playing[guild_id]
                # Initialize queue if needed
                if guild_id not in self.music_queues:
                    self.music_queues[guild_id] = []
                # Add current song to end of queue
                self.music_queues[guild_id].append(current_song)

        # Check priority queue first (user-added songs)
        if guild_id in self.priority_queues and self.priority_queues[guild_id]:
            # Play next priority song
            next_song = self.priority_queues[guild_id].pop(0)
            self.now_playing[guild_id] = next_song
            await self.play_song(interaction, next_song, send_message=False, queue_type='priority')
            return

        # Check if there are songs in regular queue
        if guild_id in self.music_queues and self.music_queues[guild_id]:
            # Play next song without sending followup message
            next_song = self.music_queues[guild_id].pop(0)
            self.now_playing[guild_id] = next_song

            # Check if we need to maintain 3-song minimum after consuming from regular queue
            current_regular_count = len(self.music_queues.get(guild_id, []))
            if current_regular_count < 3:
                songs_to_add = 3 - current_regular_count
                # Add random songs in background without awaiting
                asyncio.create_task(self.add_random_songs(guild_id, min_count=songs_to_add))

            await self.play_song(interaction, next_song, send_message=False, queue_type='regular')
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

                # Clear voice channel status if bot has permission
                try:
                    voice_channel = interaction.guild.voice_client.channel
                    if voice_channel.permissions_for(interaction.guild.get_member(self.bot.user.id)).manage_channels:
                        await voice_channel.edit(status=None)
                except (discord.Forbidden, AttributeError):
                    # No permission to edit channel status or no voice channel, skip silently
                    pass

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

        # Update stats for each randomly added song
        for song in random_songs:
            self.update_song_stats(song['file_path'], event_type='queued', queue_type='regular')


    def load_song_cache(self):
        """Load song cache from file if it exists and is valid"""
        if self.CACHE_FILE.exists():
            try:
                with open(self.CACHE_FILE, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)

                # Check if cache is still valid (files haven't changed)
                cached_files = set(cache_data.get('files', []))
                current_files = set(str(f) for f in SONGS_DIR.glob("**/*.mp3"))

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
            'files': [str(f) for f in SONGS_DIR.glob("**/*.mp3")]
            }

            # Ensure directory exists
            self.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

            with open(self.CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)

            logger.info(f"Saved song cache to file: {len(self.song_cache)} songs")

            logger.info(f"Saved song cache to file: {len(self.song_cache)} songs")
        except Exception as e:
            logger.error(f"Failed to save song cache: {e}")

    # Song name autocomplete function
    @play.autocomplete("song_name")
    async def song_name_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        # Only load from file, don't regenerate cache
        if self.song_cache is None:
            # Try to load from file
            if not self.load_song_cache():
                # If no cache file exists, return empty
                return []

        if not self.song_cache:
            return []

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

        # Show priority queue (user-requested songs) first
        priority_count = len(self.priority_queues.get(guild_id, []))
        regular_count = len(self.music_queues.get(guild_id, []))
        total_count = priority_count + regular_count

        # If no songs in queue, just send a simple message and delete it
        if total_count == 0:
            message: discord.Message = await interaction.followup.send("No songs in queue")
            await message.delete(delay=10)
            return

        # Create music player interface using LayoutView
        view = LayoutView()

        # Create container for the queue display
        full_container = Container()

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

            # Add now playing section
            full_container.add_item(Section(
                TextDisplay(f"# 🎵 Now Playing 🎵"),
                TextDisplay(f"### {song_info['title']}"),
                TextDisplay(f"👤 {song_info['artist']} ({song_info.get('duration_str', 'Unknown')})"),
                accessory=album_art
            ))

        view.add_item(full_container)

        full_container.add_item(Separator())

        # If priority queue is large, show only priority queue to avoid UI limits
        if priority_count > 3:
            # Show only priority queue when it's large
            full_container.add_item(TextDisplay(f"### ⭐ Priority Queue: ({priority_count})"))
            priority_to_show = min(6, priority_count)  # Show up to 6 priority items

            for i, song in enumerate(self.priority_queues[guild_id][:priority_to_show], 1):
                # Format duration for display
                if 'duration' in song and song['duration']:
                    minutes = song['duration'] // 60
                    seconds = song['duration'] % 60
                    duration_str = f"{minutes}:{seconds:02d}"
                else:
                    duration_str = "Unknown"
                full_container.add_item(Section(
                    TextDisplay(f"{i}. **{song['title']}**"),
                    TextDisplay(f"-# {song['artist']} ({duration_str})"),
                    accessory=discord.ui.Button(
                        style=discord.ButtonStyle.secondary,
                        label="Remove",
                        custom_id=f"remove_priority_{guild_id}_{i-1}",
                    )
                ))

            if priority_count > priority_to_show:
                full_container.add_item(TextDisplay(f"⭐ ... and {priority_count - priority_to_show} more priority songs"))

            # Don't show regular queue when priority is large
            if regular_count > 0:
                full_container.add_item(TextDisplay(f"🎲 {regular_count} regular songs queued after priority"))
        else:
            # Show both queues when priority is small
            # Limit display to avoid hitting 40 child limit
            max_sections = 8  # Stay well under 40 total children
            sections_used = 0

            # Show priority queue items first (prioritize these)
            if priority_count > 0 and sections_used < max_sections:
                full_container.add_item(TextDisplay(f"### ⭐ Priority Queue: ({priority_count})"))
                priority_to_show = min(3, priority_count, max_sections - sections_used)

                for i, song in enumerate(self.priority_queues[guild_id][:priority_to_show], 1):
                    # Format duration for display
                    if 'duration' in song and song['duration']:
                        minutes = song['duration'] // 60
                        seconds = song['duration'] % 60
                        duration_str = f"{minutes}:{seconds:02d}"
                    else:
                        duration_str = "Unknown"
                    full_container.add_item(Section(
                        TextDisplay(f"{i}. **{song['title']}**"),
                        TextDisplay(f"-# {song['artist']} ({duration_str})"),
                        accessory=discord.ui.Button(
                            style=discord.ButtonStyle.secondary,
                            label="Remove",
                            custom_id=f"remove_priority_{guild_id}_{i-1}",
                        )
                    ))
                    sections_used += 1

                if priority_count > priority_to_show:
                    full_container.add_item(TextDisplay(f"⭐ ... and {priority_count - priority_to_show} more priority songs"))

            # Show regular queue items only if we have space
            if regular_count > 0 and sections_used < max_sections:
                remaining_slots = max_sections - sections_used
                if priority_count > 0:
                    full_container.add_item(Separator(visible=False))
                full_container.add_item(TextDisplay(f"### 🎲 Regular Queue: ({regular_count})"))

                # Calculate starting index for regular queue display
                start_index = 1
                if priority_count > 0:
                    start_index = priority_count + 1

                # Show remaining available slots for regular queue
                regular_to_show = min(remaining_slots, regular_count)
                regular_display = self.music_queues[guild_id][:regular_to_show]

                for i, song in enumerate(regular_display, start_index):
                    # Format duration for display
                    if 'duration' in song and song['duration']:
                        minutes = song['duration'] // 60
                        seconds = song['duration'] % 60
                        duration_str = f"{minutes}:{seconds:02d}"
                    else:
                        duration_str = "Unknown"
                    full_container.add_item(Section(
                        TextDisplay(f"{i}. **{song['title']}**"),
                        TextDisplay(f"-# {song['artist']} ({duration_str})"),
                        accessory=discord.ui.Button(
                            style=discord.ButtonStyle.secondary,
                            label="Remove",
                            custom_id=f"remove_regular_{guild_id}_{i-start_index}",
                        )
                    ))

                remaining_regular = regular_count - len(regular_display)
                if remaining_regular > 0:
                    full_container.add_item(TextDisplay(f"➡️ ... and {remaining_regular} more songs"))

        # Determine pause/resume button state
        is_paused = self.pause_states.get(guild_id, False)
        pause_label = "Resume" if is_paused else "Pause"
        pause_emoji = "▶️" if is_paused else "⏸️"

        # Determine loop button state
        current_loop_mode = self.loop_modes.get(guild_id, 'off')
        if current_loop_mode == 'off':
            loop_label = "Loop: Off"
            loop_emoji = "🔁"
            loop_style = discord.ButtonStyle.secondary
        elif current_loop_mode == 'single':
            loop_label = "Loop: Single"
            loop_emoji = "🔂"
            loop_style = discord.ButtonStyle.primary
        elif current_loop_mode == 'queue':
            loop_label = "Loop: Queue"
            loop_emoji = "🔁"
            loop_style = discord.ButtonStyle.primary

        # Add control buttons container
        control_container = Container(
            ActionRow(
                discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label=pause_label,
                    emoji=pause_emoji,
                    custom_id=f"queue_pause_{guild_id}",
                ),
                discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="Skip",
                    emoji="⏭️",
                    custom_id=f"queue_skip_{guild_id}",
                ),
                discord.ui.Button(
                    style=loop_style,
                    label=loop_label,
                    emoji=loop_emoji,
                    custom_id=f"queue_loop_{guild_id}",
                ),
                discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label="Clear Queue",
                    emoji="🧹",
                    custom_id=f"queue_clear_{guild_id}",
                ),
                discord.ui.Button(
                    style=discord.ButtonStyle.danger,
                    label="Stop",
                    emoji="⏹️",
                    custom_id=f"queue_stop_{guild_id}",
                ),
            ),
        )
        view.add_item(control_container)

        # Send the view with album art file if it exists
        if 'album_art_file' in locals() and album_art_file is not None:
            await interaction.followup.send(view=view, file=album_art_file)
        else:
            await interaction.followup.send(view=view)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Handle button interactions for queue management"""
        if interaction.type == discord.InteractionType.component:
            custom_id = interaction.data.get('custom_id', '')
            if custom_id.startswith('remove_'):
                await self.handle_remove_button(interaction, custom_id)
            elif custom_id.startswith('queue_'):
                await self.handle_queue_button(interaction, custom_id)
            elif custom_id.startswith('play_again_'):
                await self.handle_play_again_button(interaction, custom_id)

    async def handle_remove_button(self, interaction: discord.Interaction, custom_id: str):
        """Handle remove button clicks"""
        # Parse custom_id format: remove_{queue_type}_{guild_id}_{index}
        parts = custom_id.split('_')
        if len(parts) != 4:
            return

        _, queue_type, guild_id_str, index_str = parts
        try:
            guild_id = int(guild_id_str)
            index = int(index_str)
        except ValueError:
            return

        # Check if user has permission (in voice channel)
        if not interaction.user.voice or interaction.user.voice.channel != interaction.guild.voice_client.channel:
            await interaction.response.send_message("You must be in the voice channel to manage the queue.", ephemeral=True)
            return

        # Defer the interaction to prevent timeout
        await interaction.response.defer()

        # Remove the song from the appropriate queue
        if queue_type == 'priority':
            if guild_id in self.priority_queues and index < len(self.priority_queues[guild_id]):
                removed_song = self.priority_queues[guild_id].pop(index)
                # Update the queue message with new view
                await self.update_queue_message(interaction)
            else:
                await interaction.response.send_message("Song not found in priority queue.", ephemeral=True)
        elif queue_type == 'regular':
            if guild_id in self.music_queues and index < len(self.music_queues[guild_id]):
                removed_song = self.music_queues[guild_id].pop(index)

                # Check if we need to add more songs to maintain 3-song minimum
                current_regular_count = len(self.music_queues.get(guild_id, []))
                if current_regular_count < 3:
                    songs_to_add = 3 - current_regular_count
                    # Add random songs and wait for completion
                    await self.add_random_songs(guild_id, min_count=songs_to_add)

                # Update the queue message with new view after songs are added
                await self.update_queue_message(interaction)
            else:
                await interaction.response.send_message("Song not found in regular queue.", ephemeral=True)

    async def handle_queue_button(self, interaction: discord.Interaction, custom_id: str):
        """Handle queue control button clicks"""
        # Parse custom_id format: queue_{action}_{guild_id}
        parts = custom_id.split('_')
        if len(parts) != 3:
            return

        _, action, guild_id_str = parts
        try:
            guild_id = int(guild_id_str)
        except ValueError:
            return

        # Check if user has permission (in voice channel)
        if not interaction.user.voice or interaction.user.voice.channel != interaction.guild.voice_client.channel:
            await interaction.response.send_message("You must be in the voice channel to control music.", ephemeral=True)
            return

        # Defer the interaction to prevent timeout
        await interaction.response.defer()

        if action == 'skip':
            # Same logic as skip command
            if interaction.guild.voice_client is None:
                await interaction.response.send_message("I'm not currently playing music.", ephemeral=True)
                return

            if not interaction.guild.voice_client.is_playing():
                await interaction.response.send_message("No song is currently playing.", ephemeral=True)
                return

            # Mark as skip in progress (on_song_end will handle the stats recording)
            self.skip_in_progress[guild_id] = True

            # Stop current song (this will trigger on_song_end)
            interaction.guild.voice_client.stop()

            # Ensure we maintain 3 songs in regular queue after skip
            current_regular_count = len(self.music_queues.get(guild_id, []))
            if current_regular_count < 3:
                songs_to_add = 3 - current_regular_count
                # Add random songs in background without awaiting
                asyncio.create_task(self.add_random_songs(guild_id, min_count=songs_to_add))

            # Wait a moment for the song transition to complete
            await asyncio.sleep(0.5)

            # Update the queue message with new view
            await self.update_queue_message(interaction)

        elif action == 'stop':
            # Set stop flag to prevent on_song_end from playing next song
            self.stop_in_progress[guild_id] = True

            # Record play duration for currently playing song before stopping
            if guild_id in self.now_playing and guild_id in self.current_play_start:
                song_info = self.now_playing[guild_id]
                play_duration = asyncio.get_event_loop().time() - self.current_play_start[guild_id]

                # Always record play duration and session (even for stopped songs)
                self.record_play_duration(song_info['file_path'], play_duration)

                # Record appropriate event type - treat stop as a special case
                current_type = self.current_queue_type.get(guild_id, 'regular')
                # For stop, we don't count it as completed or skipped, but we record the duration
                # This is a new event type we might want to track separately

                del self.current_play_start[guild_id]

            # Clear all queues and disconnect
            if guild_id in self.priority_queues:
                del self.priority_queues[guild_id]
            if guild_id in self.music_queues:
                del self.music_queues[guild_id]
            if guild_id in self.now_playing:
                del self.now_playing[guild_id]

            # Clear voice channel status if bot has permission
            try:
                voice_channel = interaction.guild.voice_client.channel
                if voice_channel.permissions_for(interaction.guild.get_member(self.bot.user.id)).manage_channels:
                    await voice_channel.edit(status=None)
            except (discord.Forbidden, AttributeError):
                # No permission to edit channel status or no voice channel, skip silently
                pass

            # Stop current song and disconnect
            if interaction.guild.voice_client:
                if interaction.guild.voice_client.is_playing():
                    interaction.guild.voice_client.stop()
                await asyncio.sleep(0.5)  # Brief pause before disconnecting
                await interaction.guild.voice_client.disconnect()

            # Clear Rich Presence
            await self.bot.change_presence(activity=None)

            # Clear stop flag after disconnect
            if guild_id in self.stop_in_progress:
                del self.stop_in_progress[guild_id]

            # Create a stopped message with no buttons
            view = LayoutView()
            stopped_container = Container()
            stopped_container.add_item(Section(
                TextDisplay("⏹️ **Music Stopped**"),
                TextDisplay("All queues cleared and disconnected from voice channel."),
                TextDisplay("-# Use `/play` to start playing music again"),
                accessory=discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="Play Again",
                    emoji="▶️",
                    custom_id=f"play_again_{guild_id}",
                )
            ))
            view.add_item(stopped_container)

            # Update the message with stopped state (no action row)
            try:
                await interaction.message.edit(view=view)
            except Exception as e:
                logger.warning(f"Could not update stop message: {e}")
                await interaction.followup.send(view=view, ephemeral=True)

        elif action == 'pause':
            # Toggle pause/resume
            if interaction.guild.voice_client:
                if interaction.guild.voice_client.is_playing():
                    interaction.guild.voice_client.pause()
                    self.pause_states[guild_id] = True
                    # Update the queue message with paused state
                    await self.update_queue_message(interaction)
                elif interaction.guild.voice_client.is_paused():
                    interaction.guild.voice_client.resume()
                    self.pause_states[guild_id] = False
                    # Update the queue message with resumed state
                    await self.update_queue_message(interaction)
                else:
                    await interaction.response.send_message("No song is currently playing.", ephemeral=True)
            else:
                await interaction.response.send_message("I'm not currently playing music.", ephemeral=True)

        elif action == 'loop':
            # Cycle through loop modes: off -> single -> queue -> off
            current_mode = self.loop_modes.get(guild_id, 'off')
            if current_mode == 'off':
                self.loop_modes[guild_id] = 'single'
            elif current_mode == 'single':
                self.loop_modes[guild_id] = 'queue'
            elif current_mode == 'queue':
                self.loop_modes[guild_id] = 'off'
            # Update the queue message to show new loop mode
            await self.update_queue_message(interaction)

        elif action == 'clear':
            # Record play duration for currently playing song before clearing queues
            if guild_id in self.now_playing and guild_id in self.current_play_start:
                song_info = self.now_playing[guild_id]
                play_duration = asyncio.get_event_loop().time() - self.current_play_start[guild_id]

                # Always record play duration and session (even for cleared songs)
                self.record_play_duration(song_info['file_path'], play_duration)

                # Record appropriate event type - treat clear as a special case
                current_type = self.current_queue_type.get(guild_id, 'regular')
                # For clear, we don't count it as completed or skipped, but we record the duration
                # This is a new event type we might want to track separately

                del self.current_play_start[guild_id]

            # Clear all songs from both priority and regular queues
            cleared_anything = False
            if guild_id in self.priority_queues:
                self.priority_queues[guild_id].clear()
                cleared_anything = True
            if guild_id in self.music_queues:
                self.music_queues[guild_id].clear()
                cleared_anything = True

            if cleared_anything:
                # Add back 3 random songs to maintain minimum
                await self.add_random_songs(guild_id, min_count=3)
                # Update the queue message with cleared state
                await self.update_queue_message(interaction)
            else:
                await interaction.response.send_message("No songs in queue to clear.", ephemeral=True)

        elif action == 'shuffle':
            # Shuffle the regular queue
            if guild_id in self.music_queues and self.music_queues[guild_id]:
                import random
                random.shuffle(self.music_queues[guild_id])
                # Update the queue message with shuffled view
                await self.update_queue_message(interaction)
            else:
                await interaction.response.send_message("No songs in regular queue to shuffle.", ephemeral=True)

    async def update_queue_message(self, interaction: discord.Interaction):
        """Update the queue message with current state"""
        guild_id = interaction.guild.id

        # Create updated music player interface using LayoutView
        view = LayoutView()

        # Create container for the queue display
        full_container = Container()

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

            # Add now playing section
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

        if total_count > 0:
            # If priority queue is large, show only priority queue to avoid UI limits
            if priority_count > 3:
                # Show only priority queue when it's large
                full_container.add_item(TextDisplay(f"### ⭐ Priority Queue: ({priority_count})"))
                priority_to_show = min(6, priority_count)  # Show up to 6 priority items

                for i, song in enumerate(self.priority_queues[guild_id][:priority_to_show], 1):
                    # Format duration for display
                    if 'duration' in song and song['duration']:
                        minutes = song['duration'] // 60
                        seconds = song['duration'] % 60
                        duration_str = f"{minutes}:{seconds:02d}"
                    else:
                        duration_str = "Unknown"
                    full_container.add_item(Section(
                        TextDisplay(f"{i}. **{song['title']}**"),
                        TextDisplay(f"-# {song['artist']} ({duration_str})"),
                        accessory=discord.ui.Button(
                            style=discord.ButtonStyle.secondary,
                            label="Remove",
                            custom_id=f"remove_priority_{guild_id}_{i-1}",
                        )
                    ))

                if priority_count > priority_to_show:
                    full_container.add_item(TextDisplay(f"⭐ ... and {priority_count - priority_to_show} more priority songs"))

                # Don't show regular queue when priority is large
                if regular_count > 0:
                    full_container.add_item(TextDisplay(f"🎲 {regular_count} regular songs queued after priority"))
            else:
                # Show both queues when priority is small
                # Limit display to avoid hitting 40 child limit
                max_sections = 8  # Stay well under 40 total children
                sections_used = 0

                # Show priority queue items first (prioritize these)
                if priority_count > 0 and sections_used < max_sections:
                    full_container.add_item(TextDisplay(f"### ⭐ Priority Queue: ({priority_count})"))
                    priority_to_show = min(3, priority_count, max_sections - sections_used)

                    for i, song in enumerate(self.priority_queues[guild_id][:priority_to_show], 1):
                        # Format duration for display
                        if 'duration' in song and song['duration']:
                            minutes = song['duration'] // 60
                            seconds = song['duration'] % 60
                            duration_str = f"{minutes}:{seconds:02d}"
                        else:
                            duration_str = "Unknown"
                        full_container.add_item(Section(
                            TextDisplay(f"{i}. **{song['title']}**"),
                            TextDisplay(f"-# {song['artist']} ({duration_str})"),
                            accessory=discord.ui.Button(
                                style=discord.ButtonStyle.secondary,
                                label="Remove",
                                custom_id=f"remove_priority_{guild_id}_{i-1}",
                            )
                        ))
                        sections_used += 1

                    if priority_count > priority_to_show:
                        full_container.add_item(TextDisplay(f"⭐ ... and {priority_count - priority_to_show} more priority songs"))

                # Show regular queue items only if we have space
                if regular_count > 0 and sections_used < max_sections:
                    remaining_slots = max_sections - sections_used
                    if priority_count > 0:
                        full_container.add_item(Separator(visible=False))
                    full_container.add_item(TextDisplay(f"### 🎲 Regular Queue: ({regular_count})"))

                    # Calculate starting index for regular queue display
                    start_index = 1
                    if priority_count > 0:
                        start_index = priority_count + 1

                    # Show remaining available slots for regular queue
                    regular_to_show = min(remaining_slots, regular_count)
                    regular_display = self.music_queues[guild_id][:regular_to_show]

                    for i, song in enumerate(regular_display, start_index):
                        # Format duration for display
                        if 'duration' in song and song['duration']:
                            minutes = song['duration'] // 60
                            seconds = song['duration'] % 60
                            duration_str = f"{minutes}:{seconds:02d}"
                        else:
                            duration_str = "Unknown"
                        full_container.add_item(Section(
                            TextDisplay(f"{i}. **{song['title']}**"),
                            TextDisplay(f"-# {song['artist']} ({duration_str})"),
                            accessory=discord.ui.Button(
                                style=discord.ButtonStyle.secondary,
                                label="Remove",
                                custom_id=f"remove_regular_{guild_id}_{i-start_index}",
                            )
                        ))

                    remaining_regular = regular_count - len(regular_display)
                    if remaining_regular > 0:
                        full_container.add_item(TextDisplay(f"➡️ ... and {remaining_regular} more songs"))
        else:
            full_container.add_item(TextDisplay("No songs in queue"))

        # Determine pause/resume button state
        is_paused = self.pause_states.get(guild_id, False)
        pause_label = "Resume" if is_paused else "Pause"
        pause_emoji = "▶️" if is_paused else "⏸️"

        # Determine loop button state
        current_loop_mode = self.loop_modes.get(guild_id, 'off')
        if current_loop_mode == 'off':
            loop_label = "Loop: Off"
            loop_emoji = "🔁"
            loop_style = discord.ButtonStyle.secondary
        elif current_loop_mode == 'single':
            loop_label = "Loop: Single"
            loop_emoji = "🔂"
            loop_style = discord.ButtonStyle.primary
        elif current_loop_mode == 'queue':
            loop_label = "Loop: Queue"
            loop_emoji = "🔁"
            loop_style = discord.ButtonStyle.primary

        # Add control buttons container
        control_container = Container(
            ActionRow(
                discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label=pause_label,
                    emoji=pause_emoji,
                    custom_id=f"queue_pause_{guild_id}",
                ),
                discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="Skip",
                    emoji="⏭️",
                    custom_id=f"queue_skip_{guild_id}",
                ),
                discord.ui.Button(
                    style=loop_style,
                    label=loop_label,
                    emoji=loop_emoji,
                    custom_id=f"queue_loop_{guild_id}",
                ),
                discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label="Clear Queue",
                    emoji="🧹",
                    custom_id=f"queue_clear_{guild_id}",
                ),
                discord.ui.Button(
                    style=discord.ButtonStyle.danger,
                    label="Stop",
                    emoji="⏹️",
                    custom_id=f"queue_stop_{guild_id}",
                ),
            ),
        )
        view.add_item(control_container)

        # Update the original message with the new view
        try:
            if 'album_art_file' in locals() and album_art_file is not None:
                await interaction.message.edit(view=view, attachments=[album_art_file])
            else:
                await interaction.message.edit(view=view)
        except Exception as e:
            logger.warning(f"Could not update queue message: {e}")
            # Fallback to sending a new message
            if 'album_art_file' in locals() and album_art_file is not None:
                await interaction.followup.send(view=view, file=album_art_file, ephemeral=True)
            else:
                await interaction.followup.send(view=view, ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Handle voice channel state changes for music management"""
        guild = member.guild
        voice_client = guild.voice_client

        # Check if bot disconnected (kicked, manually disconnected, etc.)
        if member == guild.me and before.channel is not None and after.channel is None:
            # Bot was disconnected from voice channel
            # Clear voice channel status if bot has permission (before disconnecting)
            try:
                if before.channel.permissions_for(guild.get_member(self.bot.user.id)).manage_channels:
                    await before.channel.edit(status=None)
            except (discord.Forbidden, AttributeError):
                # No permission to edit channel status or channel no longer accessible, skip silently
                pass

            # Clear Rich Presence when disconnected
            try:
                await self.bot.change_presence(activity=None)
            except discord.Forbidden:
                # No permission to change presence, skip silently
                pass

            # Clear now playing for this guild
            if guild.id in self.now_playing:
                del self.now_playing[guild.id]

            return

        # Ignore other bot state changes
        if member.bot:
            return

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

                # Clear voice channel status if bot has permission
                try:
                    if voice_client.permissions_for(guild.get_member(self.bot.user.id)).manage_channels:
                        await voice_client.edit(status=None)
                except (discord.Forbidden, AttributeError):
                    # No permission to edit channel status or no voice channel, skip silently
                    pass

                await voice_client.disconnect()
                logger.info(f"Disconnected from {guild.name} - alone for {self.ALONE_TIMEOUT} seconds")

                # Clear Rich Presence
                await self.bot.change_presence(activity=None)

                # Clear now playing for this guild
                if guild.id in self.now_playing:
                    del self.now_playing[guild.id]

        except Exception as e:
            logger.error(f"Error in scheduled disconnect: {e}")

    async def load_song_stats(self):
        """Load song statistics from file on startup"""
        try:
            if self.STATS_FILE.exists():
                with open(self.STATS_FILE, 'r', encoding='utf-8') as f:
                    self.song_stats = json.load(f)
                #logger.info(f"Loaded song stats for {len(self.song_stats)} songs")
            else:
                self.song_stats = {}
                logger.info("No existing song stats file found, starting fresh")
        except Exception as e:
            logger.error(f"Failed to load song stats: {e}")
            self.song_stats = {}

    def save_song_stats(self):
        """Save song statistics to file"""
        try:
            # Ensure directory exists
            self.STATS_FILE.parent.mkdir(parents=True, exist_ok=True)

            with open(self.STATS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.song_stats, f, ensure_ascii=False, indent=2)

            #logger.info(f"Saved song stats for {len(self.song_stats)} songs")
        except Exception as e:
            logger.error(f"Failed to save song stats: {e}")

    def update_song_stats(self, file_path: str, event_type: str = 'queued', queue_type: str = 'regular', user_id: str = None):
        """Update statistics for a song

        event_type: 'queued' (added to queue), 'started' (began playing), 'completed' (finished playing), 'skipped' (was skipped)
        queue_type: 'priority' or 'regular' (only used for queued events)
        """
        import time
        current_time = time.time()

        if file_path not in self.song_stats:
            self.song_stats[file_path] = {
                # Queue tracking
                'queued_total': 0,
                'queued_priority': 0,
                'queued_regular': 0,
                'first_queued': current_time,
                'last_queued': current_time,

                # Playback tracking
                'started_plays': 0,
                'started_priority': 0,
                'started_regular': 0,
                'completed_plays': 0,
                'completed_priority': 0,
                'completed_regular': 0,
                'skipped_plays': 0,
                'skipped_priority': 0,
                'skipped_regular': 0,
                'first_played': None,
                'last_played': None,

                # Time tracking
                'total_play_time': 0,
                'play_sessions': [],

                # User tracking
                'request_users': {}
            }

        stats = self.song_stats[file_path]

        if event_type == 'queued':
            # Song was added to a queue
            stats['queued_total'] += 1
            if queue_type == 'priority':
                stats['queued_priority'] += 1
            else:
                stats['queued_regular'] += 1
            stats['last_queued'] = current_time

            # Track requesting user
            if user_id:
                user_key = str(user_id)
                if user_key not in stats['request_users']:
                    stats['request_users'][user_key] = 0
                stats['request_users'][user_key] += 1

        elif event_type == 'started':
            # Song actually started playing
            stats['started_plays'] += 1
            if queue_type == 'priority':
                stats['started_priority'] += 1
            else:
                stats['started_regular'] += 1
            if stats['first_played'] is None:
                stats['first_played'] = current_time
            stats['last_played'] = current_time

            # Track requesting user for started songs (for immediate plays)
            if user_id:
                user_key = str(user_id)
                if user_key not in stats['request_users']:
                    stats['request_users'][user_key] = 0
                stats['request_users'][user_key] += 1

        elif event_type == 'completed':
            # Song finished playing completely
            stats['completed_plays'] += 1
            if queue_type == 'priority':
                stats['completed_priority'] += 1
            else:
                stats['completed_regular'] += 1

        elif event_type == 'skipped':
            # Song was skipped during playback
            stats['skipped_plays'] += 1
            if queue_type == 'priority':
                stats['skipped_priority'] += 1
            else:
                stats['skipped_regular'] += 1

        # Save stats after every update for now (can be optimized later)
        self.save_song_stats()

    def record_skip(self, file_path: str):
        """Record a skip for a song"""
        if file_path in self.song_stats:
            self.song_stats[file_path]['skips'] += 1

    def record_play_duration(self, file_path: str, duration: float):
        """Record the duration a song was played"""
        if file_path in self.song_stats:
            self.song_stats[file_path]['total_play_time'] += duration
            self.song_stats[file_path]['play_sessions'].append(duration)

            # Keep only last 100 sessions to prevent unlimited growth
            if len(self.song_stats[file_path]['play_sessions']) > 100:
                self.song_stats[file_path]['play_sessions'] = self.song_stats[file_path]['play_sessions'][-100:]

    async def handle_play_again_button(self, interaction: discord.Interaction, custom_id: str):
        """Handle play again button clicks"""
        # Parse custom_id format: play_again_{guild_id}
        parts = custom_id.split('_')
        if len(parts) != 3:
            return

        _, _, guild_id_str = parts
        try:
            guild_id = int(guild_id_str)
        except ValueError:
            return

        # Check if user is in voice channel
        if not interaction.user.voice:
            await interaction.response.send_message("You are not connected to a voice channel.", ephemeral=True)
            return

        # Check bot permissions for voice channel
        bot_member = interaction.guild.get_member(self.bot.user.id)
        if not interaction.user.voice.channel.permissions_for(bot_member).connect:
            await interaction.response.send_message("I don't have permission to connect to your voice channel.", ephemeral=True)
            return
        if not interaction.user.voice.channel.permissions_for(bot_member).speak:
            await interaction.response.send_message("I don't have permission to speak in your voice channel.", ephemeral=True)
            return

        # Check if bot is already in a different voice channel
        if interaction.guild.voice_client is not None:
            if interaction.guild.voice_client.channel != interaction.user.voice.channel:
                await interaction.response.send_message("I'm already connected to a different voice channel.", ephemeral=True)
                return
        else:
            # Bot not connected, join the channel
            await interaction.user.voice.channel.connect()

        # Defer the interaction to prevent timeout
        await interaction.response.defer()

        # Add 3 random songs to start playing again
        await self.add_random_songs(guild_id, min_count=3)

        # Ensure we have at least 3 songs in the queue before proceeding
        current_queue_count = len(self.music_queues.get(guild_id, []))
        if current_queue_count < 3:
            # Try to add more songs if we don't have enough
            additional_needed = 3 - current_queue_count
            await self.add_random_songs(guild_id, min_count=additional_needed)

        # Try to play the first song
        if guild_id in self.music_queues and self.music_queues[guild_id]:
            next_song = self.music_queues[guild_id].pop(0)
            self.now_playing[guild_id] = next_song
            # Play the song without sending a message
            await self.play_song(interaction, next_song, send_message=False)

            # Wait a moment for the song to start
            await asyncio.sleep(0.5)

            # Update the message with the full queue view
            await self.update_queue_message(interaction)
        else:
            await interaction.response.send_message("No songs available to play.", ephemeral=True)

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

        # Mark as skip in progress (on_song_end will handle the stats recording)
        guild_id = interaction.guild.id
        self.skip_in_progress[guild_id] = True

        # Stop current song (this will trigger on_song_end)
        interaction.guild.voice_client.stop()

        # Ensure we maintain 3 songs in regular queue after skip
        current_regular_count = len(self.music_queues.get(guild_id, []))
        if current_regular_count < 3:
            songs_to_add = 3 - current_regular_count
            # Add random songs in background without awaiting
            asyncio.create_task(self.add_random_songs(guild_id, min_count=songs_to_add))

        await interaction.response.send_message("⏭️ Skipped current song!", ephemeral=True)

    @app_commands.command(name="songstats", description="Show statistics for a specific song")
    @app_commands.describe(song_name="Name of the song to get stats for")
    async def songstats(self, interaction: discord.Interaction, song_name: str):
        await interaction.response.defer()

        # Find the song in cache
        song_info = None
        for song in self.song_cache:
            if song['display_name'] == song_name:
                song_info = song
                break

        if not song_info:
            await interaction.response.send_message(f"Song `{song_name}` not found in library.", ephemeral=True)
            return

        # Get stats for this song
        file_path = song_info['file_path']
        if file_path not in self.song_stats:
            await interaction.response.send_message(f"No statistics available for `{song_name}` yet.", ephemeral=True)
            return

        stats = self.song_stats[file_path]

        # Format the stats display
        import time
        from datetime import datetime

        # Calculate average play duration
        avg_duration = 0
        if stats['play_sessions']:
            avg_duration = sum(stats['play_sessions']) / len(stats['play_sessions'])

        # Format timestamps
        first_played = datetime.fromtimestamp(stats['first_played']).strftime('%Y-%m-%d %H:%M')
        last_played = datetime.fromtimestamp(stats['last_played']).strftime('%Y-%m-%d %H:%M')

        # Calculate skip rate
        total_interactions = stats['total_plays'] + stats['skips']
        skip_rate = (stats['skips'] / total_interactions * 100) if total_interactions > 0 else 0

        # Create stats display using LayoutView
        view = LayoutView()

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
            logger.warning(f"Could not extract album art for stats: {e}")

        stats_container = Container()
        stats_container.add_item(Section(
            TextDisplay(f"📊 **Song Statistics**"),
            TextDisplay(f"### {song_info['title']}"),
            TextDisplay(f"👤 {song_info['artist']}"),
            accessory=album_art
        ))

        # Stats details - show new accurate stats
        completion_rate = (stats['completed_plays'] / stats['started_plays'] * 100) if stats['started_plays'] > 0 else 0

        stats_container.add_item(Section(
            TextDisplay("**Play Statistics**"),
            TextDisplay(f"🎵 Started Plays: **{stats['started_plays']}**"),
            TextDisplay(f"✅ Completed Plays: **{stats['completed_plays']}** ({completion_rate:.1f}%)"),
            TextDisplay(f"⏭️ Skipped Plays: **{stats['skipped_plays']}**"),
            TextDisplay(f"📋 Total Queued: **{stats['queued_total']}** ({stats['queued_priority']} priority, {stats['queued_regular']} regular)"),
        ))

        # Time statistics
        minutes_avg = int(avg_duration // 60)
        seconds_avg = int(avg_duration % 60)
        total_hours = stats['total_play_time'] / 3600

        stats_container.add_item(Section(
            TextDisplay("**Time Statistics**"),
            TextDisplay(f"🕒 First Played: **{first_played}**"),
            TextDisplay(f"🕒 Last Played: **{last_played}**"),
            TextDisplay(f"⏱️ Average Duration: **{minutes_avg}:{seconds_avg:02d}**"),
            TextDisplay(f"📈 Total Play Time: **{total_hours:.1f} hours**"),
        ))

        # User statistics
        top_users = sorted(stats['request_users'].items(), key=lambda x: x[1], reverse=True)[:5]
        if top_users:
            user_stats = []
            for user_id, count in top_users:
                try:
                    user = await self.bot.fetch_user(int(user_id))
                    user_stats.append(f"{user.display_name}: {count}")
                except:
                    user_stats.append(f"User {user_id}: {count}")

            stats_container.add_item(Section(
                TextDisplay("**Top Requesters**"),
                *[TextDisplay(f"👤 {stat}") for stat in user_stats[:3]],  # Limit to 3 users
            ))

        view.add_item(stats_container)

        # Send the view with album art file if it exists
        if album_art_file is not None:
            await interaction.followup.send(view=view, file=album_art_file)
        else:
            await interaction.followup.send(view=view)

    @app_commands.command(name="topplayed", description="Show most played songs")
    async def topplayed(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not self.song_stats:
            await interaction.response.send_message("No song statistics available yet.", ephemeral=True)
            return

        # Sort songs by started plays (accurate play count)
        sorted_songs = sorted(self.song_stats.items(), key=lambda x: x[1]['started_plays'], reverse=True)[:10]

        # Create top played display using LayoutView
        view = LayoutView()

        top_container = Container()
        top_container.add_item(TextDisplay("🏆 **Most Played Songs**"))
        top_container.add_item(TextDisplay("Top 10 songs by total play count"))

        for i, (file_path, stats) in enumerate(sorted_songs, 1):
            # Find song info from cache
            song_info = None
            for song in self.song_cache:
                if song['file_path'] == file_path:
                    song_info = song
                    break

            if song_info:
                top_container.add_item(TextDisplay(f"#{i} **{song_info['title']}**"))
                top_container.add_item(TextDisplay(f"👤 {song_info['artist']} • 🎵 {stats['started_plays']} plays"))

        view.add_item(top_container)
        await interaction.followup.send(view=view)

    @app_commands.command(name="mostskipped", description="Show most skipped songs")
    async def mostskipped(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not self.song_stats:
            await interaction.followup.send("No song statistics available yet.", ephemeral=True)
            return

        # Filter songs with at least 3 started plays and sort by skip rate
        songs_with_skips = []
        for file_path, stats in self.song_stats.items():
            total_interactions = stats['started_plays'] + stats['skipped_plays']
            if total_interactions >= 3:  # Minimum threshold
                skip_rate = stats['skipped_plays'] / total_interactions
                songs_with_skips.append((file_path, stats, skip_rate))

        if not songs_with_skips:
            await interaction.followup.send("No songs with sufficient data to calculate skip rates.", ephemeral=True)
            return

        # Sort by skip rate (descending)
        sorted_songs = sorted(songs_with_skips, key=lambda x: x[2], reverse=True)[:10]

        # Create most skipped display using LayoutView
        view = LayoutView()

        skip_container = Container()
        skip_container.add_item(TextDisplay("😬 **Most Skipped Songs**"))
        skip_container.add_item(TextDisplay("Top 10 songs by skip rate (minimum 3 interactions)"))

        for i, (file_path, stats, skip_rate) in enumerate(sorted_songs, 1):
            # Find song info from cache
            song_info = None
            for song in self.song_cache:
                if song['file_path'] == file_path:
                    song_info = song
                    break

            if song_info:
                total_interactions = stats['started_plays'] + stats['skipped_plays']
                skip_container.add_item(TextDisplay(f"#{i} **{song_info['title']}**"))
                skip_container.add_item(TextDisplay(f"👤 {song_info['artist']} • ⏭️ {skip_rate*100:.1f}% skipped • 🎵 {total_interactions} total"))

        view.add_item(skip_container)
        await interaction.followup.send(view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
