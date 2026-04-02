import asyncio
import logging
import sys
from pathlib import Path

import discord
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DISCORD_TOKEN, GUILD_ID
from database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("rota-bot")

intents = discord.Intents.default()
intents.message_content = True
intents.guild_messages = True
intents.guild_reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    log.info(f"Synced slash commands to guild {GUILD_ID}")
    log.info(f"Bot is ready as {bot.user}")


async def main():
    await init_db()
    async with bot:
        await bot.load_extension("cogs.schedule")
        await bot.load_extension("cogs.scheduler")
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
