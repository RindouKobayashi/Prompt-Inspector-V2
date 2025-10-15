import pathlib
import os
import discord
import logging
import json
from google.generativeai import types
from google import genai
import base64
from PIL import Image
from io import BytesIO

from dotenv import load_dotenv
from logging.config import dictConfig

load_dotenv()

branch = os.getenv("GITHUB_BRANCH", "main")

if branch == "main":
    DISCORD_API_TOKEN = os.getenv("DISCORD_API_TOKEN")
elif branch == "dev":
    DISCORD_API_TOKEN = os.getenv("DISCORD_API_TOKEN_DEV")

BASE_DIR = pathlib.Path(__file__).parent
COGS_DIR = BASE_DIR / "cogs"
SONGS_DIR = BASE_DIR / "songs"


GEMINI_DIR = BASE_DIR / "gemini"
CREDS = BASE_DIR / "load_creds.py"
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
chat_history = GEMINI_DIR / "chat_history.json"
if chat_history.exists():
    with open(chat_history, "r") as f:
        chat_history = json.load(f)
else:
    chat_history = []

SAFETY_SETTINGS = [
    {
        "category": "HARM_CATEGORY_HARASSMENT",
        "threshold": "BLOCK_NONE",
    },
    {
        "category": "HARM_CATEGORY_HATE_SPEECH",
        "threshold": "BLOCK_NONE",
    },
    {
        "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "threshold": "BLOCK_NONE",
    },
    {
        "category": "HARM_CATEGORY_DANGEROUS",
        "threshold": "BLOCK_NONE",
    }
]
CHAT = client.chats.create(model="gemini-2.0-flash-exp", history=chat_history, config={"response_modalities": ["TEXT", "IMAGE"]})

def generate_content(prompt):
    """Single-shot content generation"""
    return client.models.generate_content(
        model="gemini-2.0-flash-exp",
        contents=[prompt]
    )

BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID"))

class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[94m',    # Blue
        'INFO': '\033[92m',     # Green
        'WARNING': '\033[93m',  # Yellow
        'ERROR': '\033[91m',    # Red
        'CRITICAL': '\033[95m', # Magenta
    }
    RESET = '\033[0m'

    def format(self, record):
        color = self.COLORS.get(record.levelname, self.RESET)
        message = super().format(record)
        return f"{color}{message}{self.RESET}"

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(levelname)-10s - %(asctime)s - %(module)-15s : %(message)s",
        },
        "standard": {
            "format": "%(levelname)-10s - %(name)-15s : %(message)s",
        },
        "colored": {
            "()": ColoredFormatter,
            "format": "%(levelname)-10s - %(name)-15s : %(message)s",
        }
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "colored",
            "stream": "ext://sys.stdout",
        },
        "console2": {
            "level": "WARNING",
            "class": "logging.StreamHandler",
            "formatter": "colored",
            "stream": "ext://sys.stdout",
        },
        "file": {
            "level": "INFO",
            "class": "logging.FileHandler",
            "filename": "logs/infos.log",
            "formatter": "verbose",
            "mode": "w",
            "encoding": "utf-8",
        },        
    },
    "loggers": {
        "bot": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False
        },
        "discord": {
            "handlers": ["console2", "file"],
            "level": "INFO",
            "propagate": False
        }
    }
}

logger = logging.getLogger("bot")

dictConfig(LOGGING_CONFIG)

# Prompt Inspector monitored channels
monitored_channels = [int(x) for x in os.getenv('MONITORED_CHANNELS', '').split(',') if x]
