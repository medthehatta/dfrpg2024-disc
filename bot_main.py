import asyncio
from contextlib import suppress
import json
from uuid import uuid1
import random
import re
import subprocess
import time

from cytoolz import partition_all
from cytoolz import valmap

import discord
from discord.ext import commands


#
# Globals
#


with open("config.json", "r") as f:
    config = json.load(f)


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or("!"),
    intents=intents,
)


#
# Helpers
#


def quick_embed(title, message=None, fields=None, footer=None):
    fields = fields or {}
    embed = discord.Embed(title=title, description=message)
    for (field, value) in fields.items():
        embed.add_field(name=field, value=value, inline=False)
    if footer is not None:
        embed.add_footer(text=footer)
    return embed


#
# Bot setup stuff
#


@bot.event
async def on_ready():
    guild = bot.get_guild(int(config["guild"]))
    diag = discord.utils.get(guild.channels, name=config["diagnostics"])

    bot_version = get_version()
    config_version = get_config_version()
    location = get_host()
    admins = discord.utils.get(guild.roles, name=config["bot_admins"])

    embed = quick_embed(
        title=f"Started {bot.user.name}",
        message=f":wave: Hello!  This is {bot.user.mention}.",
        fields={
            "Bot Version": bot_version,
            "Config Version": config_version,
            "Location": location,
            "Admin Role": admins.mention,
        },
    )
    await diag.send(embed=embed)

    print(f"{bot.user} ready.")


async def assert_mod(ctx):
    guild = ctx.guild
    mod_name = config["bot_admins"]
    mod_role = discord.utils.get(guild.roles, name=mod_name)
    is_mod = discord.utils.get(ctx.author.roles, id=mod_role.id)
    if not is_mod:
        await ctx.send(
            f"You don't have permission to do this, please ask a {mod_role.mention}"
        )
        raise


@bot.command()
async def stop(ctx: commands.Context):
    await assert_mod(ctx)
    embed = quick_embed(
        title="Stopping",
        message=f"{bot.user.mention} ({get_host()}) powering down...",
    )
    await ctx.send(embed=embed)
    await bot.close()
    exit()


def get_version():
    result = (
        subprocess.check_output(["git", "rev-parse", "HEAD"])
        .decode("utf-8")
        .strip()
    )
    return result


def get_config_version():
    result = (
        subprocess.check_output(["sha1sum", "config.json"])
        .decode("utf-8")
        .strip()
    )
    (sha1sum, _) = result.split(" ", 1)
    return sha1sum


def get_host():
    result = (
        subprocess.check_output(["hostname"])
        .decode("utf-8")
        .strip()
    )
    if "mancer" in result:
        return "mancer"
    elif "DESKTOP-" in result:
        return "desktop"
    elif "albatross" in result:
        return "albatross"
    else:
        return "unknown"


@bot.command()
async def version(ctx: commands.Context):
    await ctx.send(
        f"{get_version()} (config: {get_config_version()}) "
        f"@ {get_host()}"
    )


#
# Actual functionality
#





#
# Entry point
#

if __name__ == "__main__":
    bot.run(config["token"])
