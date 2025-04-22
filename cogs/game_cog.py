import discord
from discord.ext import commands
from discord import app_commands
import settings
from settings import logger
import random
from dataclasses import dataclass, field, asdict
from typing import Dict, List
import json
from pathlib import Path
from datetime import datetime

@dataclass
class WordleGameHistory:
    word: str
    timestamp: str
    participants: List[int]  # List of user IDs
    winner_id: int = None
    winning_guesses: int = None
    all_guesses: Dict[int, List[str]] = field(default_factory=dict)  # user_id -> list of guesses
    total_participants: int = 0
    total_guesses: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> 'WordleGameHistory':
        """Create WordleGameHistory from dictionary."""
        return cls(
            word=data.get('word', ''),
            timestamp=data.get('timestamp', ''),
            participants=data.get('participants', []),
            winner_id=data.get('winner_id'),
            winning_guesses=data.get('winning_guesses'),
            all_guesses=data.get('all_guesses', {}),
            total_participants=data.get('total_participants', 0),
            total_guesses=data.get('total_guesses', 0)
        )

@dataclass
class PlayerStats:
    games_played: int = 0
    games_won: int = 0
    total_guesses: int = 0
    current_streak: int = 0
    best_streak: int = 0
    guess_distribution: Dict[int, int] = field(default_factory=lambda: {i: 0 for i in range(1, 7)})
    correct_letters: int = 0  # Number of 🟩
    yellow_letters: int = 0   # Number of 🟨
    gray_letters: int = 0     # Number of ⬛

    @property
    def games_lost(self) -> int:
        return self.games_played - self.games_won
    
    @property
    def win_rate(self) -> float:
        if self.games_played == 0:
            return 0.0
        return round((self.games_won / self.games_played) * 100, 1)
    
    @property
    def average_attempts(self) -> float:
        if self.games_won == 0:
            return 0.0
        total_winning_attempts = sum(attempts * count for attempts, count in self.guess_distribution.items())
        return round(total_winning_attempts / self.games_won, 1)

    def add_game_result(self, won: bool, attempts: int):
        self.games_played += 1
        if won:
            self.games_won += 1
            self.current_streak += 1
            self.best_streak = max(self.best_streak, self.current_streak)
            self.guess_distribution[attempts] += 1
        else:
            self.current_streak = 0

    def count_letters(self, result: str):
        """Count different types of letters from a guess result."""
        self.correct_letters += result.count("🟩")
        self.yellow_letters += result.count("🟨")
        self.gray_letters += result.count("⬛")

    @classmethod
    def from_dict(cls, data: dict) -> 'PlayerStats':
        """Create PlayerStats from dictionary."""
        stats = cls()
        stats.games_played = data.get('games_played', 0)
        stats.games_won = data.get('games_won', 0)
        stats.total_guesses = data.get('total_guesses', 0)
        stats.current_streak = data.get('current_streak', 0)
        stats.best_streak = data.get('best_streak', 0)
        # Convert string keys back to integers for guess_distribution
        raw_dist = data.get('guess_distribution', {str(i): 0 for i in range(1, 7)})
        stats.guess_distribution = {int(k): v for k, v in raw_dist.items()}
        stats.correct_letters = data.get('correct_letters', 0)
        stats.yellow_letters = data.get('yellow_letters', 0)
        stats.gray_letters = data.get('gray_letters', 0)
        return stats

class GameState:
    def __init__(self):
        self.attempts_remaining = 6
        self.last_result = None
        self.guesses = []  # List of [word, result] pairs
        self.game_over = False  # True if player lost (out of attempts)

    def has_word_been_guessed(self, word: str) -> bool:
        """Check if a word has already been guessed."""
        return any(word.lower() == previous_word.lower() for previous_word, _ in self.guesses)
    
    def get_full_history(self) -> str:
        """Get complete guess history with words and results."""
        return "\n".join(f"`{word}`: {result}" for word, result in self.guesses)
        
    def get_patterns_only(self) -> str:
        """Get just the square patterns without words."""
        return "\n".join(result for _, result in self.guesses)

class GameCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.answer_words, self.valid_guesses = self._load_word_list()
        self.current_word = self._get_random_word()
        self.games = {}  # Store game state per user: {user_id: GameState}
        self.stats = {}  # Store stats per user: {user_id: PlayerStats}
        self.stats_file = Path("data/wordle_stats.json")
        self.history_file = Path("data/wordle_history.json")
        self.current_game_history = WordleGameHistory(
            word=self.current_word,
            timestamp=datetime.now().isoformat(),
            participants=[]
        )
        self.game_history = []  # List of past games
        self.load_stats()
        self.load_history()
        # logger.info(f"Initialized GameCog with word: {self.current_word}") # Removed logging

    def save_history(self):
        """Save game history to JSON file."""
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            history_data = [asdict(h) for h in self.game_history]
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(history_data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving Wordle history: {e}")

    def load_history(self):
        """Load game history from JSON file."""
        try:
            if self.history_file.exists():
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    history_data = json.load(f)
                self.game_history = [WordleGameHistory.from_dict(data) for data in history_data]
            else:
                self.game_history = []
        except Exception as e:
            logger.error(f"Error loading Wordle history: {e}")
            self.game_history = []

    def save_stats(self):
        """Save player stats to JSON file."""
        try:
            # Create directory if it doesn't exist
            self.stats_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Convert stats to dictionary format
            stats_dict = {
                str(user_id): asdict(stats) 
                for user_id, stats in self.stats.items()
            }
            
            # Save to file
            with open(self.stats_file, 'w', encoding='utf-8') as f:
                json.dump(stats_dict, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving Wordle stats: {e}")

    def load_stats(self):
        """Load player stats from JSON file."""
        try:
            if self.stats_file.exists():
                with open(self.stats_file, 'r', encoding='utf-8') as f:
                    stats_dict = json.load(f)
                    
                # Convert dictionary data back to PlayerStats objects
                self.stats = {
                    int(user_id): PlayerStats.from_dict(data)
                    for user_id, data in stats_dict.items()
                }
                logger.info("Loaded Wordle stats successfully")
            else:
                logger.info("No existing stats file found, starting fresh")
        except Exception as e:
            logger.error(f"Error loading Wordle stats: {e}")
            self.stats = {}  # Start fresh if loading fails

    def _load_word_list(self) -> tuple[set, set]:
        """Load the list of answer words and allowed guesses."""
        try:
            # Load answer words
            with open("data/wordle-answers-alphabetical.txt", "r") as f:
                answers = {line.strip() for line in f if line.strip()}
            
            # Load allowed guesses
            with open("data/wordle-allowed-guesses.txt", "r") as f:
                guesses = {line.strip() for line in f if line.strip()}
            
            # Combine them for valid guesses
            valid_guesses = answers.union(guesses)
            return answers, valid_guesses
            
        except Exception as e:
            logger.error(f"Error loading word lists: {e}")
            # Fallback to some default words if files can't be loaded
            answers = {"apple", "beach", "chair", "dance", "eagle"}
            return answers, answers.copy()

    def _get_random_word(self) -> str:
        """Get a random word from the answer list."""
        answers, _ = self._load_word_list()
        return random.choice(list(answers))

    def _reset_game(self, user_id: int):
        """Reset game state for a user."""
        self.games[user_id] = GameState()

    def _reset_all_games(self, old_word: str):
        """Reset all games and pick a new word."""
        new_word = self._get_random_word()
        # Make sure new word is different from old word
        while new_word == old_word:
            new_word = self._get_random_word()
            
        self.current_word = new_word
        # logger.info(f"New word chosen: {self.current_word}") # Removed logging
        for player_id in self.games:
            self._reset_game(player_id)

    def get_guess_distribution_display(self, stats: PlayerStats) -> str:
        """Create a visual bar graph of guess distribution."""
        if stats.games_won == 0:
            return "No wins yet!"
        
        max_count = max(stats.guess_distribution.values())
        bars = []
        for attempts, count in stats.guess_distribution.items():
            bar_length = int((count / max_count) * 20) if max_count > 0 else 0
            bar = "█" * bar_length
            bars.append(f"{attempts}│ {bar} {count}")
        return "\n".join(bars)

    def check_word(self, guess: str) -> str:
        """Check the guess against the current word and return formatted result."""
        if len(guess) != 5:
            return None

        result = []
        guess = guess.lower()
        
        # Check for exact matches first (green)
        for i in range(5):
            if i >= len(guess):
                break
            if guess[i] == self.current_word[i]:
                result.append("🟩")
            elif guess[i] in self.current_word:
                result.append("🟨")
            else:
                result.append("⬛")
                
        return "".join(result)

    @app_commands.command(name="wordle", description="Guess the 5-letter word in 6 tries. 🟩=correct, 🟨=wrong spot, ⬛=not in word")
    @app_commands.describe(word="Type a 5-letter word to guess")
    async def wordle(self, interaction: discord.Interaction, word: str):
        """Play Wordle using autocompletion."""
        user_id = interaction.user.id
        
        # Initialize attempts if first time
        if user_id not in self.games:
            self._reset_game(user_id)
            # Add user to current game history
            if user_id not in self.current_game_history.participants:
                self.current_game_history.participants.append(user_id)
                self.current_game_history.all_guesses[user_id] = []
                # We'll count total_participants when saving game based on who actually made guesses
        if user_id not in self.stats:
            self.stats[user_id] = PlayerStats()
        
        state = self.games[user_id]
        stats = self.stats[user_id]
        
        # Handle admin skip
        if word == "adminskip" and user_id == settings.BOT_OWNER_ID:
            old_word = self.current_word
            # Save current unfinished game to history
            self.current_game_history.word = old_word  # Ensure the word is recorded
            self.game_history.append(self.current_game_history)
            self.save_history()
            
            self._reset_all_games(old_word)
            # Start new game history
            self.current_game_history = WordleGameHistory(
                word=self.current_word,
                timestamp=datetime.now().isoformat(),
                participants=[]
            )
            
            await interaction.response.send_message(
                f"🎮 Admin skip: The word was `{old_word}`\n"
                f"A new word has been chosen for everyone!"
            )
            return
        
        if state.last_result == "win":
            attempts_used = 6 - state.attempts_remaining
            stats.add_game_result(won=True, attempts=attempts_used)
            
            # Update game history
            self.current_game_history.winner_id = user_id
            self.current_game_history.winning_guesses = attempts_used
            if user_id not in self.current_game_history.all_guesses:
                self.current_game_history.all_guesses[user_id] = []
            self.current_game_history.all_guesses[user_id].extend([g[0] for g in state.guesses])
            self.current_game_history.total_guesses = sum(len(guesses) for guesses in self.current_game_history.all_guesses.values())
            
            # Update total_participants based on who actually made guesses
            self.current_game_history.total_participants = len([uid for uid, guesses in self.current_game_history.all_guesses.items() if guesses])
            # Save current game and start new one
            self.game_history.append(self.current_game_history)
            self.save_history()
            self.save_stats()
            
            await interaction.response.send_message(
                f"🎉 Congratulations! You found the word `{word}` in {attempts_used} attempts!\n"
                f"A new word has been chosen for everyone!\n\n"
                f"Your guesses:\n{state.get_full_history()}\n\n"
                f"Current streak: {stats.current_streak} | Best streak: {stats.best_streak}"
            )
            
            old_word = self.current_word
            self._reset_all_games(old_word)
            # Start new game history
            self.current_game_history = WordleGameHistory(
                word=self.current_word,
                timestamp=datetime.now().isoformat(),
                participants=[]
            )
        elif state.game_over or word == "game_over":  # Handle both actual game over and game over value
            # Update stats and history for loss
            if not state.last_result:  # Only update stats once
                stats.add_game_result(won=False, attempts=6)
                state.last_result = "lose"
                if user_id not in self.current_game_history.all_guesses:
                    self.current_game_history.all_guesses[user_id] = []
                self.current_game_history.all_guesses[user_id].extend([g[0] for g in state.guesses])
                self.current_game_history.total_guesses = sum(len(guesses) for guesses in self.current_game_history.all_guesses.values())
                self.current_game_history.total_participants = len([uid for uid, guesses in self.current_game_history.all_guesses.items() if guesses])
                self.save_stats()
            # Show patterns without revealing words
            await interaction.response.send_message(
                f"❌ Out of attempts! Wait for someone to solve the word!\n\n"
                f"Your patterns:\n{state.get_patterns_only()}\n\n"
                f"Streak ended at: {stats.current_streak} wins"
            )
        elif not word or len(word) != 5:
            await interaction.response.send_message("Please enter a 5-letter word!")
        else:
            _, valid_guesses = self._load_word_list()
            if word.lower() not in valid_guesses:
                await interaction.response.send_message("Please enter a valid 5-letter word!")
                return
            
            # Track guess in history
            if user_id not in self.current_game_history.all_guesses:
                self.current_game_history.all_guesses[user_id] = []
            self.current_game_history.all_guesses[user_id].append(word.lower())
            self.current_game_history.total_guesses = sum(len(guesses) for guesses in self.current_game_history.all_guesses.values())
            await interaction.response.send_message(
                f"Keep guessing! {state.attempts_remaining} attempts remaining\n\n"
                f"{state.get_full_history()}"
            )

    @app_commands.command(name="wordle-history", description="View Wordle game history")
    async def wordle_history(self, interaction: discord.Interaction):
        """Show history of played Wordle games."""
        if not self.game_history:
            await interaction.response.send_message("No game history available yet!")
            return

        # Create embed
        embed = discord.Embed(
            title="📜 Recent Wordle Games",
            description="Showing the 6 most recent games played",
            color=discord.Color.purple()
        )

        # Show last 6 completed games in a 2x3 grid (3 rows of 3 fields each)
        last_games = self.game_history[-6:]
        fields_added = 0
        
        for i, game in enumerate(reversed(last_games)):
            winner = self.bot.get_user(game.winner_id) if game.winner_id else None
            winner_name = winner.name if winner else "No winner"
            
            # Convert timestamp to readable format
            try:
                date = datetime.fromisoformat(game.timestamp).strftime("%H:%M")
            except:
                date = "??"
            
            solved = "✅" if game.winner_id is not None else "❌"
            game_num = len(self.game_history) - i  # Most recent game gets highest number
            
            # Add the game field
            embed.add_field(
                name=f"{solved} Game #{game_num}",
                value=f"`{game.word}` | {winner_name}\n"
                      f"👥 {game.total_participants} | 🎯 {game.total_guesses}\n"
                      f"Try #{game.winning_guesses if game.winning_guesses else '-'} | {date}",
                inline=True
            )
            fields_added += 1
            
            # Add a spacer field after every second game field to create pairs
            # And add a spacer before the third row starts
            if i % 2 == 1 or i == 3: # Add spacer after 2nd, 4th, 6th game OR before 5th game (index 4)
                 embed.add_field(name="\u200b", value="\u200b", inline=True) # Use zero-width space
                 fields_added += 1

        # Fill any remaining slots to ensure 9 fields before the separator
        while fields_added < 9:
             embed.add_field(name="\u200b", value="\u200b", inline=True)
             fields_added += 1

        # Add separator
        embed.add_field(name="〰️" * 12, value="", inline=False)

        # Add total stats with emoji
        total_games = len(self.game_history)
        total_participants = sum(game.total_participants for game in self.game_history)
        total_guesses = sum(game.total_guesses for game in self.game_history)
        solved_games = len([g for g in self.game_history if g.winner_id is not None])
        solve_rate = (solved_games / total_games * 100) if total_games > 0 else 0

        embed.add_field(
            name="📊 Overall History Stats",
            value=f"🎮 Total Games: **{total_games}**\n"
                  f"👥 Total Players: **{total_participants}**\n"
                  f"🎯 Total Guesses: **{total_guesses}**\n"
                  f"✨ Solved Games: **{solved_games}/{total_games}** ({solve_rate:.1f}%)",
            inline=False
        )

        # Add footer with average stats
        if total_games > 0:
            embed.set_footer(text=f"Average: {total_participants/total_games:.1f} players and {total_guesses/total_games:.1f} guesses per game")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="wordle-stats", description="View your Wordle statistics")
    async def wordle_stats(self, interaction: discord.Interaction):
        """Show player's Wordle statistics."""
        user_id = interaction.user.id
        if user_id not in self.stats:
            self.stats[user_id] = PlayerStats()
        
        stats = self.stats[user_id]
        
        # Create an embed for better formatting
        embed = discord.Embed(
            title="🎮 Wordle Statistics",
            color=discord.Color.blue()
        )
        
        # Basic stats
        embed.add_field(
            name="Games",
            value=f"Played: {stats.games_played}\n"
                  f"Won: {stats.games_won}\n"
                  f"Win Rate: {stats.win_rate}%",
            inline=True
        )
        
        embed.add_field(
            name="Streaks",
            value=f"Current: {stats.current_streak}\n"
                  f"Best: {stats.best_streak}\n"
                  f"Avg. Attempts: {stats.average_attempts}",
            inline=True
        )
        
        embed.add_field(
            name="Letter Stats",
            value=f"🟩 Correct: {stats.correct_letters}\n"
                  f"🟨 Wrong Spot: {stats.yellow_letters}\n"
                  f"⬛ Wrong: {stats.gray_letters}",
            inline=True
        )
        
        # Guess distribution
        embed.add_field(
            name="Guess Distribution",
            value=f"```\n{self.get_guess_distribution_display(stats)}\n```",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="wordle-leaderboard", description="View the Wordle leaderboard")
    async def wordle_leaderboard(self, interaction: discord.Interaction):
        """Show global Wordle leaderboard."""
        if not self.stats:
            await interaction.response.send_message("No stats available yet! Play some games first!")
            return

        # Sort players by different metrics
        sorted_by_winrate = sorted(
            self.stats.items(),
            key=lambda x: (x[1].win_rate, x[1].games_won),
            reverse=True
        )[:10]

        # Create embed
        embed = discord.Embed(
            title="🏆 Wordle Leaderboard",
            color=discord.Color.gold()
        )

        # Create leaderboard text for win rates
        leaderboard_text = []
        for rank, (user_id, stats) in enumerate(sorted_by_winrate, 1):
            user = self.bot.get_user(user_id)
            username = user.name if user else f"User{user_id}"
            leaderboard_text.append(
                f"{rank}. **{username}**\n"
                f"   • Win Rate: {stats.win_rate}%\n"
                f"   • Games Won: {stats.games_won}/{stats.games_played}\n"
                f"   • Best Streak: {stats.best_streak}"
            )

        embed.add_field(
            name="Top Players",
            value="\n".join(leaderboard_text) if leaderboard_text else "No players yet!",
            inline=False
        )

        # Add global stats
        total_games = sum(stats.games_played for stats in self.stats.values())
        total_wins = sum(stats.games_won for stats in self.stats.values())
        avg_win_rate = sum(stats.win_rate for stats in self.stats.values()) / len(self.stats) if self.stats else 0
        best_streak = max(stats.best_streak for stats in self.stats.values())

        embed.add_field(
            name="Global Stats",
            value=f"Total Games: {total_games}\n"
                  f"Total Wins: {total_wins}\n"
                  f"Average Win Rate: {avg_win_rate:.1f}%\n"
                  f"Best Streak: {best_streak}",
            inline=False
        )

        await interaction.response.send_message(embed=embed)

    @wordle.autocomplete("word")
    async def word_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Actual game logic for checking the word."""
        user_id = interaction.user.id
        if user_id not in self.games:
            self._reset_game(user_id)
        if user_id not in self.stats:
            self.stats[user_id] = PlayerStats()
            
        state = self.games[user_id]
        stats = self.stats[user_id]
        choices = []
        
        # If game is over, show game over first
        if state.game_over:
            choices.append(app_commands.Choice(
                name="❌ Out of attempts! Press Enter to see your patterns",
                value="game_over"
            ))
            return choices
            
        # Add admin skip option if user is bot owner
        if user_id == settings.BOT_OWNER_ID and current and "adminskip".startswith(current.lower()):
            choices.append(app_commands.Choice(
                name="[ADMIN] Skip to next word",
                value="adminskip"
            ))
            if current.lower() == "adminskip":
                return [choices[-1]]  # Return only admin option when fully typed
        
        # Always show previous guesses as choices with both word and pattern
        for word, result in state.guesses:
            choices.append(app_commands.Choice(name=f"{word}: {result}", value=word))
        
        # If no input but we have guesses, show attempts remaining
        if not current and state.guesses:
            choices.append(app_commands.Choice(
                name=f"{state.attempts_remaining} attempts remaining",
                value="status"
            ))
            return choices
        
        # Handle current input
        if current:
            current = current.lower()
            if len(current) == 5:
                # Check for duplicate guesses first
                if state.has_word_been_guessed(current):
                    choices.append(app_commands.Choice(
                        name=f"❌ Already guessed '{current}'! Try a different word",
                        value=current
                    ))
                    return choices
                
                _, valid_guesses = self._load_word_list()
                if current not in valid_guesses:
                    choices.append(app_commands.Choice(
                        name=f"❌ Not a valid word ({state.attempts_remaining} attempts left)",
                        value=current
                    ))
                else:
                    # Decrease attempt for valid 5-letter word check
                    if state.attempts_remaining > 0:
                        state.attempts_remaining -= 1
                        result = self.check_word(current)
                        
                        # Update letter stats
                        stats.count_letters(result)
                        # Save stats after updating
                        self.save_stats()
                        
                        # Handle correct guess
                        if current == self.current_word:
                            state.last_result = "win"
                            state.guesses.append([current, result])
                            choices.append(app_commands.Choice(
                                name=f"{current}: {result} (Press Enter to submit!)",
                                value=current
                            ))
                        # Handle running out of attempts
                        elif state.attempts_remaining <= 0:
                            state.game_over = True  # Mark player as out of attempts
                            state.guesses.append([current, result])
                            # Return only game over choice
                            return [app_commands.Choice(
                                name="❌ Out of attempts! Press Enter to see your patterns",
                                value="game_over"
                            )]
                        # Regular guess
                        else:
                            state.guesses.append([current, result])
                            choices.append(app_commands.Choice(
                                name=f"{current}: {result} ({state.attempts_remaining} attempts left)",
                                value=current
                            ))
            else:
                choices.append(app_commands.Choice(
                    name=f"Type a 5-letter word ({state.attempts_remaining} attempts left)",
                    value=current
                ))
        
        return choices

async def setup(bot: commands.Bot):
    await bot.add_cog(GameCog(bot))
