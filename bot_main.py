import asyncio
from functools import wraps
from contextlib import suppress
import json
from uuid import uuid1
import random
import re
import requests
import subprocess
import time

from cytoolz import partition_all
from cytoolz import valmap
from cytoolz import get_in

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


base_url = (
    config["debug_backend_url"] if config.get("mode", "").lower() == "debug" else
    config["backend_url"]
).rstrip("/")


bot_command_dispatch = {}


player_mapping = {}


#
# Helpers
#


class CommandRegistrar:

    def __init__(self):
        self.commands = {}

    def register(self, *regexes):

        def _register(func):
            for regex in regexes:
                self.commands[regex] = func

            @wraps(func)
            def _a(*args, **kwargs):
                return func(*args, **kwargs)

            return _a

        return _register

    def first_match(self, string, fallback=None):
        for (regex, func) in self.commands.items():
            if match := re.search(regex, string):
                return (regex, func, match.groupdict())
        else:
            if fallback:
                return (None, fallback, string)
            else:
                raise ValueError(f"No match found for: {string}")


cmds = CommandRegistrar()


def quick_embed(title, message=None, fields=None, footer=None):
    fields = fields or {}
    embed = discord.Embed(title=title, description=message)
    for (field, value) in fields.items():
        embed.add_field(name=field, value=value, inline=False)
    if footer is not None:
        embed.add_footer(text=footer)
    return embed


def _json_pretty(data):
    return "```\n" + json.dumps(data, indent=2) + "\n```"


def _deep_printable(obj):
    if isinstance(obj, dict):
        return {k: _deep_printable(v) for (k, v) in obj.items()}
    elif isinstance(obj, (tuple, list)):
        return type(obj)([_deep_printable(x) for x in obj])
    elif hasattr(obj, "__dict__"):
        return {k: _deep_printable(v) for (k, v) in obj.__dict__.items()}
    else:
        return obj


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


def _issue_command(cmd):
    res = requests.post(
        base_url + "/commands",
        json=cmd,
    )
    res.raise_for_status()
    return res.json()


def _get_game():
    res = requests.get(base_url + "/game")
    return res.json()


@bot.command()
async def dump_game(ctx: commands.Context):
    await ctx.send(json.dumps(_get_game()))


@bot.command()
async def raw(ctx: commands.Context, *, content: str):
    splitted = content.split(" ", 1)
    if len(splitted) == 1:
        path = splitted[0].lstrip("/").rstrip("/")
        res = requests.get(base_url + "/" + path)
    else:
        (path, payload) = splitted
        payload = "".join(payload)
        res = requests.post(base_url + "/" + path.lstrip("/").rstrip("/"), json=json.loads(payload))
    await ctx.send(_json_pretty(res.json()))


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    guild = bot.get_guild(int(config["guild"]))
    diag = discord.utils.get(guild.channels, name=config["diagnostics"])

    if message.content.startswith("."):
        await _dispatch_bot_command(message)

    elif message.content.startswith("###"):
        await message.channel.send(str(_deep_printable(message)))

    else:
        await bot.process_commands(message)


async def _dispatch_bot_command(message):
    (regex, func, kwargs) = cmds.first_match(message.content, fallback=lambda x: x)
    if regex is not None:
        await func(message, **kwargs)
    else:
        await message.channel.send(f"Could not interpret command: {message.content}")


def entities_from_message(message):
    explicit_matches = [
        m.group(1) for m in re.finditer(r'@\s*(\S+)', message.content)
    ]
    match_for_author_claim = player_mapping.get(message.author.display_name)
    match_for_author_name = re.search(r'[(](\S+)[)]', message.author.display_name)
    if explicit_matches:
        return explicit_matches
    elif match_for_author_claim:
        return [match_for_author_claim]
    elif match_for_author_name:
        return [match_for_author_name]
    else:
        return []


def pretty_print_entity(entity):
    name = entity["name"]
    fp = entity.get("fate", 0)
    refresh = entity.get("refresh", 0)
    stresses = {
        track: " ".join(
            "x" if i in get_in(["stress", track, "checked"], entity, default=[]) else
            "o"
            for i in range(get_in(["stress", track, "max"], entity, default=2))
        )
        for track in entity.get("stress", {})
    }
    sections = [f"**{name.upper()}**"]
    sections += [f"**{track[0].upper()})** {boxes}" for (track, boxes) in stresses.items()]
    sections += [f"**FP)** {fp}/{refresh}"]
    return "  ".join(sections)


def targeted(func):

    @wraps(func)
    async def _targeted(message, *args, **kwargs):
        if "entity" in kwargs and kwargs["entity"]:
            entities = [kwargs["entity"]]
        else:
            entities = entities_from_message(message)

        if not entities:
            await message.channel.send("Could not find applicable entity.")

        for entity in entities:
            all_kwargs = {**kwargs, "entity": entity}
            await func(message, *args, **all_kwargs)

    return _targeted


async def standard_abort(message, response):
    inner_ok = get_in(["result", "ok"], response)
    if not inner_ok:
        await message.channel.send(_json_pretty(response["result"]))
        return True
    else:
        return False


#
# Commands
#


@cmds.register(r"[.]test")
async def _test(message):
    await message.channel.send(f"Successfully found message: {message.content}")


@cmds.register(r"[.]commands")
async def _commands(message):
    listing = []
    for (regex, func) in cmds.commands.items():
        listing.append(f"{func.__name__}  --  {regex}")
    await message.channel.send("```\n" + "\n".join(listing) + "\n```")


@cmds.register(r"[.]claim\s+(?P<entity>\w+)")
async def _claim(message, entity):
    author = message.author.display_name
    player_mapping[author] = entity
    await message.channel.send(f"{author} is now playing {entity}")


@cmds.register(r"[.]unclaim")
async def _unclaim(message):
    author = message.author.display_name
    if author in player_mapping:
        entity = player_mapping.pop(author)
        await message.channel.send(f"{author} is no longer playing {entity}")
    else:
        await message.channel.send(f"{author} is not playing any character")


@cmds.register(r"[.]claimed")
async def _claimed(message):
    await message.channel.send(json.dumps(player_mapping))


@cmds.register(r"[.](info)(\s+(?P<entity>\w+))?")
async def _info(message, entity=None):
    if entity is None:
        entities = entities_from_message(message)
    else:
        entities = [entity]

    if not entities:
        await message.channel.send(f"Could not find applicable entity.")

    game = _get_game()
    for entity in entities:
        ent = get_in(["result", "entities", entity], game)
        await message.channel.send(pretty_print_entity(ent))


@cmds.register(r"[.](increment_fp|fp[+])(\s+(?P<entity>\w+))?")
@targeted
async def _increment_fp(message, entity):
    result = _issue_command({"command": "increment_fp", "entity": entity})
    if await standard_abort(message, result):
        return

    ent = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_entity(ent))


@cmds.register(r"[.](decrement_fp|fp[-])(\s+(?P<entity>\w+))?")
@targeted
async def _decrement_fp(message, entity):
    result = _issue_command({"command": "decrement_fp", "entity": entity})
    if await standard_abort(message, result):
        return

    ent = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_entity(ent))


#
# Entry point
#

if __name__ == "__main__":
    bot.run(config["token"])
