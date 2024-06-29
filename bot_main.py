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
import tempfile

from cytoolz import sliding_window
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

player_last_rolls = {}

player_last_missing_target_cmd = {}


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


async def _as_json_file(ctx, data, summary="", filename="output.txt"):
    fh = tempfile.NamedTemporaryFile("w")
    json.dump(data, fh, indent=4)
    fh.flush()
    error_file = discord.File(fh.name, filename=filename)
    await ctx.send(summary, file=error_file)
    fh.close()


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
    await _as_json_file(ctx, res.json(), filename="raw.txt")


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


def _insensitive_entity(game, name):
    entities = get_in(["result", "entities"], game)
    lower_map = {e.lower(): e for e in entities}
    return lower_map.get(name.lower(), name)


def entities_from_message(message):
    explicit_matches = [
        m.group(1) for m in re.finditer(r'@\s*(\S+)', message.content)
    ]
    match_for_author_claim = player_mapping.get(message.author.display_name)
    match_for_author_name = re.search(r'[(](\S+)[)]', message.author.display_name)

    if explicit_matches:
        found = explicit_matches
    elif match_for_author_claim:
        found = [match_for_author_claim]
    elif match_for_author_name:
        found = [match_for_author_name]
    else:
        found = []

    game = _get_game()
    return [_insensitive_entity(game, n) for n in found]


def pretty_print_entity(entity):
    name = entity["name"]
    fp = entity.get("fate", 0)
    refresh = entity.get("refresh", 0)
    stresses = {
        track: " ".join(
            "x" if i in get_in(["stress", track, "checked"], entity, default=[]) else
            "o"
            for i in range(
                1,
                get_in(["stress", track, "max"], entity, default=2) + 1,
            )
        )
        for track in entity.get("stress", {})
    }

    sections = [f"**{name.upper()}**"]
    sections += [f"**{track[0].upper()})** {boxes}" for (track, boxes) in stresses.items()]
    sections += [f"**FP)** {fp}/{refresh}"]
    first_line = "  ".join(sections)

    aspects = entity.get("aspects", [])
    translate = {
        "sticky": "s",
        "fragile": "f",
        "mild": "mild",
        "moderate": "mod",
        "severe": "sev",
        "extreme": "extreme",
    }
    aspects_f = []
    for aspect in aspects:
        if "tags" in aspects:
            tags_f = " ".join(["(#)"]*aspects["tags"]) + " "
        else:
            tags_f = ""

        if "kind" in aspect:
            kind_f = "**(" + translate.get(aspect["kind"], aspect["kind"]) + ")** "
        else:
            kind_f = ""

        name = aspect["name"]
        aspect_f = f"<{tags_f}{kind_f}{name}>"

        aspects_f.append(aspect_f)

    aspect_line = "  ".join(aspects_f)

    return f"{first_line}\n{aspect_line}"


def targeted(func):

    @wraps(func)
    async def _targeted(message, *args, **kwargs):
        if "entity" in kwargs and kwargs["entity"]:
            entities = [_insensitive_entity(_get_game(), kwargs["entity"])]
        else:
            entities = entities_from_message(message)

        if not entities:
            await message.channel.send("Could not find applicable entity.")
            author = message.author.display_name
            player_last_missing_target_cmd[author] = (func, message, args, kwargs)

        for entity in entities:
            all_kwargs = {**kwargs, "entity": entity}
            await func(message, *args, **all_kwargs)

    return _targeted


async def standard_abort(message, response):
    inner_ok = get_in(["result", "ok"], response)
    if not inner_ok:
        desc = (
            get_in(["result", "description"], response) or
            get_in(["description"], response)
        )
        await _as_json_file(
            message.channel,
            response,
            summary=f"{message.author.mention}: Error: {desc}",
            filename="error.txt",
        )
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


@cmds.register(r"[.](claim|c)\s+(?P<entity>\w+)")
async def _claim(message, entity):
    game = _get_game()
    entities = get_in(["result", "entities"], game)
    lower_map = {e.lower(): e for e in entities}
    if entity.lower() not in lower_map:
        await message.channel.send(f"{message.author.mention}: No such entity: {entity}")
    else:
        author = message.author.display_name
        entity_name = lower_map[entity.lower()]
        player_mapping[author] = entity_name
        await message.channel.send(f"{author} is now playing {entity_name}")


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

    # Case-insensitive
    game = _get_game()
    entities = [_insensitive_entity(game, e) for e in entities]

    if not entities:
        await message.channel.send(f"Could not find applicable entity.")

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


def _omit_match_spans(matches, string):
    last_idx = len(string)
    spans = [
        (e1, s2)
        for ((s1, e1), (s2, e2))
        in sliding_window(2, [(0, 0)] + [m.span() for m in matches] + [(last_idx, None)])
    ]
    return "".join(string[a:b] for (a, b) in spans)


@cmds.register(r"[.](add_aspect|aspect[+]|a[+])(?P<maybe_aspect>.+)")
@targeted
async def _add_aspect(message, maybe_aspect, entity):
    kind_translator = {
        "f": "fragile",
        "s": "sticky",
        "mod": "moderate",
        "sev": "severe",
        "x": "extreme",
    }
    aspect_kinds_matches = list(re.finditer(r'[(](.+)[)]', maybe_aspect))
    entity_id_matches = list(re.finditer(r'@\s+(\S+)', maybe_aspect))
    aspect_text = _omit_match_spans(
        aspect_kinds_matches + entity_id_matches,
        maybe_aspect,
    )
    if aspect_kinds_matches:
        for kind_match in aspect_kinds_matches:
            k = kind_translator.get(
                kind_match.group(1).lower(),
                kind_match.group(1),
            )
            result = _issue_command({
                "command": "add_aspect",
                "name": aspect_text.strip(),
                "entity": entity,
                "kind": k,
            })
            if await standard_abort(message, result):
                return
    else:
        result = _issue_command({
            "command": "add_aspect",
            "name": aspect_text.strip(),
            "entity": entity,
        })
        if await standard_abort(message, result):
            return

    ent = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_entity(ent))


@cmds.register(r"[.](remove_aspect|aspect[-]|a[-])(?P<maybe_aspect>.+)")
@targeted
async def _remove_aspect(message, maybe_aspect, entity):
    aspect_kinds_matches = list(re.finditer(r'[(](.+)[)]', maybe_aspect))
    entity_id_matches = list(re.finditer(r'@\s+(\S+)', maybe_aspect))
    aspect_text = _omit_match_spans(
        aspect_kinds_matches + entity_id_matches,
        maybe_aspect,
    )
    result = _issue_command({
        "command": "remove_aspect",
        "name": aspect_text.strip(),
        "entity": entity,
    })
    if await standard_abort(message, result):
        return

    ent = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_entity(ent))


@cmds.register(r"[.](remove_all_temporary_aspects|clear_temporary_aspects|aspect[#]|a[#])")
async def _clear_all_temporary_aspects(message):
    result = _issue_command({
        "command": "remove_all_temporary_aspects",
    })
    if await standard_abort(message, result):
        return

    await message.channel.send(f"All temporary aspects cleared")


@cmds.register(r"[.](clear_consequences|cons#)\s+(?P<max_cons>.+)")
async def _clear_consequences(message, max_cons):
    kind_translator = {
        "f": "fragile",
        "s": "sticky",
        "mod": "moderate",
        "sev": "severe",
        "x": "extreme",
    }
    k = kind_translator.get(
        max_cons.strip().lower(),
        max_cons.strip(),
    )
    result = _issue_command({
        "command": "clear_consequences",
        "max_severity": k,
    })
    if await standard_abort(message, result):
        return

    await message.channel.send(f"All consequences up to {max_cons} cleared")


@cmds.register(
    r"[.](inflict_stress|stress[+]|s[+])\s+((?P<box>\d+).*([(](?P<track>\w+)[)]))",
    r"[.](inflict_stress|stress[+]|s[+])\s+(([(](?P<track>\w+)[)]).*(?P<box>\d+))",
)
@targeted
async def _inflict_stress(message, track, box, entity=None):
    tracks = [
        "physical",
        "mental",
        "hunger",
        "social",
    ]
    stress = next(
        (t for t in tracks if t.startswith(track.lower())),
        None,
    )
    result = _issue_command({
        "command": "add_stress",
        "stress": stress,
        "box": box,
        "entity": entity,
    })
    if await standard_abort(message, result):
        return

    ent = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_entity(ent))


@cmds.register(
    r"[.](clear_stress|stress[-]|s[-])\s+((?P<box>\d+).*([(](?P<track>\w+)[)]))",
    r"[.](clear_stress|stress[-]|s[-])\s+(([(](?P<track>\w+)[)]).*(?P<box>\d+))",
)
@targeted
async def _remove_stress(message, track, box, entity=None):
    tracks = [
        "physical",
        "mental",
        "hunger",
        "social",
    ]
    stress = next(
        (t for t in tracks if t.startswith(track.lower())),
        None,
    )
    result = _issue_command({
        "command": "clear_stress_box",
        "stress": stress,
        "box": box,
        "entity": entity,
    })
    if await standard_abort(message, result):
        return

    ent = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_entity(ent))


@cmds.register(r"[.](clear_all_stress|stress[#]|s[#])")
async def _clear_all_stress(message):
    result = _issue_command({
        "command": "clear_all_stress",
    })
    if await standard_abort(message, result):
        return

    await message.channel.send(f"Cleared all stress")


@cmds.register(r"[.](target|t)(\s+(?P<entity>\w+))?")
@targeted
async def _target(message, entity):
    author = message.author.display_name
    if author in player_last_missing_target_cmd:
        (func, message, args, kwargs) = player_last_missing_target_cmd[author]
        kwargs["entity"] = entity
        await func(message, *args, **kwargs)


@cmds.register(r"[.]roll(\s+(?P<maybe_bonuses>.*))?")
async def _roll(message, maybe_bonuses):
    maybe_bonuses = maybe_bonuses or ""
    rolled = random.choices([-1, 0, 1], k=4)
    rolls_formatted = [
        "+" if r == 1 else
        "-" if r == -1 else
        "0"
        for r in rolled
    ]
    roll_format = "[" + " ".join(rolls_formatted) + "]"
    roll_value = sum(rolled)
    bonuses = [b.group(0) for b in re.finditer(r'[+-]\d+', maybe_bonuses)]
    bonus_values = [
        -int(b[1:]) if b.startswith("-") else int(b[1:])
        for b in bonuses
    ]
    bonus_value = sum(bonus_values)
    display = " ".join([
        message.author.mention,
        "rolled: ",
        "**`" + roll_format + "`**",
        f"`{roll_value}`",
        f"`{' '.join(bonuses)}" if bonuses else "",
        "=`",
        f"**`{roll_value + bonus_value}`**",
    ]).replace("``", "")
    player_last_rolls[message.author.display_name] = {
        "rolls_f": roll_format,
        "roll_v": roll_value,
        "bonuses": bonuses,
        "bonus_v": bonus_value,
        "total": roll_value + bonus_value,
    }
    await message.channel.send(display)


@cmds.register(r"[.]amend(\s+(?P<maybe_bonuses>.*))?")
async def _amend(message, maybe_bonuses):
    author = message.author.display_name
    if author not in player_last_rolls:
        await message.channel.send(f"Could not find previous roll for {author} :sad:")
        return

    maybe_bonuses = maybe_bonuses or ""
    bonuses = [b.group(0) for b in re.finditer(r'[+-]\d+', maybe_bonuses)]
    bonus_values = [
        -int(b[1:]) if b.startswith("-") else int(b[1:])
        for b in bonuses
    ]
    bonus_value = sum(bonus_values)

    last_roll = player_last_rolls[author]
    roll_format = last_roll["rolls_f"]
    roll_value = last_roll["roll_v"]
    bonuses = last_roll["bonuses"] + bonuses
    bonus_value = last_roll["bonus_v"] + bonus_value

    display = " ".join([
        message.author.mention,
        "rolled: ",
        "**`" + roll_format + "`**",
        f"`{roll_value}`",
        f"`{' '.join(bonuses)}" if bonuses else "",
        "=`",
        f"**`{roll_value + bonus_value}`**",
    ]).replace("``", "")
    await message.channel.send(display)


#
# Entry point
#

if __name__ == "__main__":
    bot.run(config["token"])
