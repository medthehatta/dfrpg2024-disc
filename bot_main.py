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
import textwrap

from cytoolz import sliding_window
from cytoolz import partition_all
from cytoolz import valmap
from cytoolz import get_in
from cytoolz import unique

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
player_object_mapping = {}

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

    def all_matches(self, string):
        return [
            func for (regex, func) in self.commands.items()
            if re.search(regex, string)
        ]

    def search_by_function_name(self, string):
        return [
            func for (_, func) in self.commands.items()
            if (
                self.function_name(string) in self.function_name(func)
            )
        ]

    def all_function_names(self):
        return [
            self.function_name(func) for func in self.commands.values()
        ]

    def function_name(self, func):
        return func.__name__.strip().lstrip("_.")


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
    data = res.json()
    if data["ok"]:
        return data["result"]
    else:
        return {}


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
    entities = get_in(["entities"], game)
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
    sections += [f"**{track[0].upper()})** {boxes}" for (track, boxes) in stresses.items() if boxes]
    sections += [f"**FP)** {fp}/{refresh}" if refresh else f"**FP)** {fp}"]
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
        if "tags" in aspect:
            tags_f = " ".join(["(#)"]*aspect["tags"]) + " "
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


def pretty_print_order(order_):
    order = order_.get("order", [])
    current = order_.get("current")
    entities = order_.get("entities")
    deferred = order_.get("deferred", [])

    if order and entities:
        wrapped = [
            f"[{i}.{entity}]" if i == (current + 1) else f"{i}.{entity}"
            for (i, entity) in enumerate(order, start=1)
        ]

        if deferred:
            defer_msg = f"\ndeferred: {' '.join(deferred)}"
        else:
            defer_msg = ""

        if current is not None:
            active = order[current]
            active_player = next(
                (
                    player for (player, entity) in player_mapping.items()
                    if entity == active
                ),
                None,
            )
            if active_player:
                active_mention = player_object_mapping[active_player].mention
            else:
                active_mention = None

        if active_mention:
            return f"{active_mention}: {' '.join(wrapped)}{defer_msg}"
        else:
            return f"{' '.join(wrapped)}{defer_msg}"

    if (not order) and entities:
        return f"Ready: {' '.join(entities)}"

    else:
        print(f"{order=} {entities=}")
        return f"No turn order"


def targeted(func):

    @wraps(func)
    async def _targeted(message, *args, **kwargs):
        if "entity" in kwargs and kwargs["entity"]:
            entities = [_insensitive_entity(_get_game(), kwargs["entity"])]
        else:
            entities = entities_from_message(message)

        if not entities:
            await message.channel.send(
                "Could not find applicable entity in command.  "
                "Retry, or designate a target entity with `.target`"
            )
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
        await inline_abort(message, response, desc)
        return True
    else:
        return False


async def inline_abort(message, response, description):
    await _as_json_file(
        message.channel,
        response,
        summary=f"{message.author.mention}: Error: `{description}`",
        filename="error.txt",
    )


#
# Commands
#


@cmds.register(r"[.]test")
async def _test(message):
    """
    Aliases: test

    Echo back the contents of the test message.

    Intended for debugging purposes.
    """
    await message.channel.send(f"Successfully found message: {message.content}")


@cmds.register(r"[.](claim|c|assume)\s+(?P<entity>\w+)")
async def _claim(message, entity):
    """
    Aliases: claim, c, assume

    Provide the name of an entity to assume the role of.  This entity will then
    be used as the default target for any targeted commands issued by you.
    (See .target)

    Intended primarily for GM use, as they may assume the role of various NPCs.
    Players will typically assume their player character and then change it
    rarely if ever.

    List the current associations with .claimed/.assumed.

    Tips:

        Technical note: the associations are cleared if the bot is restarted.

    """
    game = _get_game()
    entities = get_in(["entities"], game)
    lower_map = {e.lower(): e for e in entities}
    if entity.lower() not in lower_map:
        await message.channel.send(f"{message.author.mention}: No such entity: {entity}")
    else:
        author = message.author.display_name
        entity_name = lower_map[entity.lower()]
        player_mapping[author] = entity_name
        player_object_mapping[author] = message.author
        await message.channel.send(f"{author} is now playing {entity_name}")


@cmds.register(r"[.](unclaim|unassume)")
async def _unclaim(message):
    """
    Aliases: unclaim, unassume

    If you, the player, have used .claim/.assume to assume the role of an
    entity (like your player character), this command will clear that
    association.

    Intended primarily for GM use, as they may assume the role of various NPCs.
    Players will typically assume their player character and then change it
    rarely if ever.

    List the existing associations with .claimed/.assumed

    Tips:

        Technical note: the associations are cleared if the bot is restarted.

    """
    author = message.author.display_name
    if author in player_mapping:
        entity = player_mapping.pop(author)
        player_object_mapping.pop(author)
        await message.channel.send(f"{author} is no longer playing {entity}")
    else:
        await message.channel.send(f"{author} is not playing any character")


@cmds.register(r"[.](claimed|assumed)")
async def _claimed(message):
    """
    Aliases: claimed, assumed

    Dump the mapping between players and their "assumed" entities.

    Claim/assume an entity with .claim/.assume.
    """
    await message.channel.send(json.dumps(player_mapping))


@cmds.register(r"[.](info)(\s+(?P<entity>\w+))?")
@targeted
async def _info(message, entity=None):
    """
    Aliases: info

    Targeted.  (See .target)

    Display an entity, including its stress, fate, and aspects.

    Examples:

        .info @ Weft

        .info Weft

    """
    game = _get_game()
    ent = get_in(["entities", entity], game)
    await message.channel.send(pretty_print_entity(ent))


@cmds.register(r"[.](entities)")
async def _entities(message):
    """
    Aliases: entities

    List the entities being tracked by the bot.

    Intended for GM use.  Using this may spoiler the secret presence of NPCs!
    """
    game = _get_game()
    ents = get_in(["entities"], game)
    if ents:
        ents_f = " ".join(v["name"] for v in ents.values())
    else:
        ents_f = "No entities"
    await message.channel.send(ents_f)


@cmds.register(r"[.](increment_fp|fp[+])(\s+(?P<entity>\w+))?")
@targeted
async def _increment_fp(message, entity):
    """
    Aliases: increment_fp, fp+

    Targeted.  (See .target)

    Increment the fate points available to an entity by 1.

    Examples:

        .fp+ @ Weft

        .fp+ Weft

    """
    result = _issue_command({"command": "increment_fp", "entity": entity})
    if await standard_abort(message, result):
        return

    ent = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_entity(ent))


@cmds.register(r"[.](decrement_fp|fp[-])(\s+(?P<entity>\w+))?")
@targeted
async def _decrement_fp(message, entity):
    """
    Aliases: decrement_fp, fp-

    Targeted.  (See .target)

    Decrement the fate points available to an entity by 1.

    Examples:

        .fp- @ Weft

        .fp- Weft

    """
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
    return re.sub(r'(\s){2,}', r'\1', "".join(string[a:b] for (a, b) in spans))


@cmds.register(r"[.](add_aspect|aspect[+]|a[+])(?P<maybe_aspect>.+)")
@targeted
async def _add_aspect(message, maybe_aspect, entity):
    """
    Aliases: add_aspect, aspect+, a+

    Targeted.  (See .target)

    Add an aspect to an entity.  Added aspects (other than consequences) will
    be added with one free tag.

    In addition to generic aspects, also supports indicating an aspect is: (A)
    a consequence with some severity, (B) a fragile aspect, or (C) a sticky
    aspect.

    These annotations will be respected in commands that distinguish between
    temporary aspects, or consqeuence severities, etc.

    Examples:

        .a+ off balance @ Mook

        .a+ (mild) sprained ankle @ Weft

        .a+ (sticky) derelict @ scene

        .a+ (fragile) stiff breeze @ scene

    """
    kind_translator = {
        "f": "fragile",
        "s": "sticky",
        "mild": "mild",
        "mod": "moderate",
        "sev": "severe",
        "x": "extreme",
    }
    consequence_kinds = ["mild", "moderate", "severe", "extreme"]
    aspect_kinds_matches = list(re.finditer(r'[(](.+)[)]', maybe_aspect))
    entity_id_matches = list(re.finditer(r'@\s*(\S+)', maybe_aspect))
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

            if k in consequence_kinds:
                free_tags = 0
            else:
                free_tags = 1

            result = _issue_command({
                "command": "add_aspect",
                "name": aspect_text.strip(),
                "entity": entity,
                "kind": k,
                "tags": free_tags,
            })
            if await standard_abort(message, result):
                return
    else:
        result = _issue_command({
            "command": "add_aspect",
            "name": aspect_text.strip(),
            "entity": entity,
            "tags": 1,
        })
        if await standard_abort(message, result):
            return

    ent = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_entity(ent))


@cmds.register(r"[.](remove_aspect|aspect[-]|a[-])(?P<maybe_aspect>.+)")
@targeted
async def _remove_aspect(message, maybe_aspect, entity):
    """
    Aliases: remove_aspect, aspect-, a-

    Targeted.  (See .target)

    Completely remove the given aspect from an entity.

    Examples:

        .a- drunk @ Jackson

    """
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


@cmds.register(r"[.](tag_aspect|tag)(?P<maybe_aspect>.+)")
@targeted
async def _tag_aspect(message, maybe_aspect, entity):
    """
    Aliases: tag_aspect, tag

    Targeted.  (See .target)

    Tag an aspect on an entity which has a free tag on it.  This just tracks
    that the free tag has been used, but does not automatically apply bonuses
    to a roll or anything like that.

    Examples:

        .tag off balance @ Mook

    """
    aspect_kinds_matches = list(re.finditer(r'[(](.+)[)]', maybe_aspect))
    entity_id_matches = list(re.finditer(r'@\s+(\S+)', maybe_aspect))
    aspect_text = _omit_match_spans(
        aspect_kinds_matches + entity_id_matches,
        maybe_aspect,
    )
    result = _issue_command({
        "command": "tag_aspect",
        "name": aspect_text.strip(),
        "entity": entity,
    })
    if await standard_abort(message, result):
        return

    ent = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_entity(ent))


@cmds.register(r"[.](clear_all_temporary_aspects|clear_temporary_aspects|aspect[#]|a[#])")
async def _clear_all_temporary_aspects(message):
    """
    Aliases: clear_all_temporary_aspects, clear_temporary_aspects, aspect#, a#

    Clear all temporary aspects (not sticky or consequences) on all entities.
    This cannot be targeted to specific entities: this always applies to ALL
    entities.

    Intended for GM use at the end of a scene.

    Examples:

        .aspect#

    """
    result = _issue_command({
        "command": "remove_all_temporary_aspects",
    })
    if await standard_abort(message, result):
        return

    await message.channel.send(f"All temporary aspects cleared")


@cmds.register(r"[.](clear_consequences|cons#)\s+(?P<max_cons>.+)")
async def _clear_consequences(message, max_cons):
    """
    Aliases: clear_consequences, cons#

    Clear all consequences on all entities with severity equal to or less
    severe than the given severity.

    Intended for GM use when sufficient time has elapsed.

    This cannot be targeted to an entity, this clears consequences on ALL
    entities.

    Examples:

        .cons# mild

        .cons# severe

    """
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

    await message.channel.send(f"All consequences up to {k} cleared")


@cmds.register(
    r"[.](inflict_stress|stress[+]|s[+])\s+((?P<box>\d+).*([(](?P<track>\w+)[)]))",
    r"[.](inflict_stress|stress[+]|s[+])\s+(([(](?P<track>\w+)[)]).*(?P<box>\d+))",
)
@targeted
async def _inflict_stress(message, track, box, entity=None):
    """
    Aliases: inflict_stress, stress+, s+

    Targeted.  (See .target)

    Check a stress box on yourself (if you have used .claim/.assume) or an
    entity.

    Examples:

        .s+ (p) 2 @ Weft

        .s+ (ment) 1

    """
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
async def _clear_stress(message, track, box, entity=None):
    """
    Aliases: clear_stress, stress-, s-

    Targeted.  (See .target)

    Uncheck a stress box on an entity.

    Examples:

        .s- (p) 2 @ Weft

        .s- (ment) 1

    """
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
    """
    Aliases: clear_all_stress, stress#, s#

    Intended for GM use after a conflict ends.  Clears ALL stress on all
    entities.

    Can't be targeted to a particular entity: this always applies to everybody.
    To clear specific stress boxes on a specific character, use .clear_stress.
    """
    result = _issue_command({
        "command": "clear_all_stress",
    })
    if await standard_abort(message, result):
        return

    await message.channel.send(f"Cleared all stress")


@cmds.register(r"[.](target|t\b)(\s+(?P<entity>\w+))?")
@targeted
async def _target(message, entity):
    """
    Aliases: target, t

    Targeted.  (See .target -- i.e. this command)

    Many of the bot commands can be "targeted" to specific entities.  You
    target entities by adding "@ Entityname" to the command.  The entity name
    is not case-sensitive.

    Alternately, you can use the .claim/.assume command to assume the role of a
    particular entity, typically your player character.  In that case you can
    omit the "@ Entityname" and your default will be used instead.  (You can
    override this by providing "@ Entityname", however)

    Regarding the .target command specifically:

    If you ran a command but forgot to target it to an entity, use this either
    with an entity name, or after using .claim/.assume to assume the role of an
    entity, and the command will be rerun against that entity.

    Note that this does not allow you to CHANGE the target of a command, it can
    only be used to apply a target when none was present before!

    The targeting works reliably, but the .target command itself is probably a
    little buggy... maybe just rerun the previous command and include the
    entity this time :)

    Examples:

        Say you had not assumed the role of your player character, and you
        wanted to add an aspect to yourself:

        .a+ some aspect name that is long and I don't want to retype it

        The bot will give you an error saying you need to target an entity.
        You can use this to target yourself:

        .target Weft

        Or you can first assume your character, then use .target without
        providing a name and it will also target your character:

        .target

    Tips:

        This remembers rolls on a per-player (not entity) basis.

    """
    author = message.author.display_name
    if author in player_last_missing_target_cmd:
        (func, message, args, kwargs) = player_last_missing_target_cmd[author]
        kwargs["entity"] = entity
        await func(message, *args, **kwargs)


@cmds.register(r"[.]roll(\s+(?P<maybe_bonuses>.*))?")
async def _roll(message, maybe_bonuses):
    """
    Aliases: roll

    Roll fate dice, optionally adding bonuses, and add up the result.

    You can also freely annotate any part of the roll with text to say what
    skill you're rolling, or to describe the flavor of your action, etc.

    Examples:

        .roll

        .roll +2 athletics +3 because I feel like it -1 guilt

    Tips:

        If you make a mistake and need to adjust the bonuses applied to your
        last roll, you can use the .amend command.

        Note that the rolls are associated to you, the player, not your
        character (even if you have used .claim or .assume to assume the guise
        of a character).  So there is no need to target an entity using "@
        Entity" when rolling (or amending your roll).

    """
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
        f"`{' '.join(bonuses)}" if bonuses else "`",
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
    """
    Aliases: amend

    Adjust the bonuses on your previous roll.  Mostly intended for fixing
    mistakes.

    Examples:

        Say you issued a plain .roll and you got a response like:

        @med rolled:  [- 0 0 -] -2  = -2

        But you forgot that actually you have a +4 from a skill, but a -1 from
        an effect.  Rerolling from scratch will likely also change the roll
        result from -2, so use amend:

        .amend +4 athletics for dodging -1 for terrain

        @med rolled:  [- 0 0 -] -2 +4 -1 = 1

    Tips:

        The rolls are associated to you, the player, not your character (even
        if you have used .claim or .assume to assume the guise of a character).
        So there is no need to target an entity using "@ Entity" when rolling
        or amending.

    """
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
        f"`{' '.join(bonuses)}" if bonuses else "`",
        "=`",
        f"**`{roll_value + bonus_value}`**",
    ]).replace("``", "")
    await message.channel.send(display)


@cmds.register(r"[.](order_add|order|ord|order[+]|ord[+])(\s+(?P<maybe_bonuses>.*))")
@targeted
async def _order_add(message, maybe_bonuses, entity):
    """
    Aliases: order_add, order [followed by something], ord, order+, ord+

    Targeted.  (See .target)

    Add yourself or an entity to the turn order tracker.  This is intended for
    setting up the turn order.  If you are trying to claim your spot in the
    turn order after deferring, use .act.

    Examples:

        .order @ Weft

        .order +2 athletics @ Weft

        .ord +2 athletics +3 just because @ Weft

    Reminder: if you have used .claim or .assume to assume an entity as
    yourself, you don't need to provide the "@ Yourname", the command will
    default to your assumed entity.  You can still override this default by
    providing "@ Entity".
    """
    maybe_bonuses = maybe_bonuses or ""
    bonuses = [b.group(0) for b in re.finditer(r'[+-]\d+', maybe_bonuses)]
    bonus_values = [
        -int(b[1:]) if b.startswith("-") else int(b[1:])
        for b in bonuses
    ]
    bonus_value = sum(bonus_values)

    result = _issue_command({
        "command": "order_add",
        "entity": entity,
        "bonus": bonus_value,
    })
    if await standard_abort(message, result):
        return

    order = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_order(order))


@cmds.register(r"[.](order_next|next)")
async def _order_next(message):
    """
    Aliases: order_next, next

    Move on to the next entity in the turn order.
    """
    result = _issue_command({
        "command": "next",
    })
    if await standard_abort(message, result):
        return

    order = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_order(order))


@cmds.register(r"[.](order_back|back|previous|prev)")
async def _order_back(message):
    """
    Aliases: order_back, back, previous, prev

    Move back to the previous entity in the turn order.
    """
    result = _issue_command({
        "command": "back",
    })
    if await standard_abort(message, result):
        return

    order = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_order(order))


@cmds.register(r"[.](order_start|start)")
async def _order_start(message):
    """
    Aliases: order_start, start

    Start the turn order with the entities who have been added to it.
    """
    result = _issue_command({
        "command": "start_order",
    })
    if await standard_abort(message, result):
        return

    order = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_order(order))


@cmds.register(r"[.](order_clear|order[#]|stop)")
async def _order_clear(message):
    """
    Aliases: order_clear, order#, stop

    End the turn order tracking.
    """
    result = _issue_command({
        "command": "clear_order",
    })
    if await standard_abort(message, result):
        return

    await message.channel.send("Turn order cleared")


@cmds.register(r"[.](order_drop|drop|remove|order[-])")
@targeted
async def _order_drop(message, entity=None):
    """
    Aliases: order_drop, drop, remove, order-

    Targeted.  (See .target)

    Remove yourself or another entity from the turn order.  Mostly intended for
    use on NPCs when they are taken out.

    Examples:

        .order-

        .remove @ Mook

        .remove @ Mook @ Mook2

    """
    result = _issue_command({
        "command": "drop_from_order",
        "entity": entity,
    })
    if await standard_abort(message, result):
        return

    order = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_order(order))


@cmds.register(r"[.](order_defer|defer)")
async def _order_defer(message):
    """
    Aliases: order_defer, defer

    Defer the current player's turn.  That entity can re-enter the turn order
    with .undefer or .act.

    Examples:

        .defer

        You can defer somebody else's turn.  This is intended for GMs managing
        NPCs.

        .defer @ Mook

    Tips:

        This command can NOT be targeted to a specific entity.  It always
        applies to the entity whose turn it is currently.

    """
    result = _issue_command({
        "command": "defer",
    })
    if await standard_abort(message, result):
        return

    order = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_order(order))


@cmds.register(r"[.](order_undefer|undefer|act)")
@targeted
async def _order_undefer(message, entity):
    """
    Aliases: order_undefer, undefer, act

    Targeted.  (See .target)

    If you have deferred your turn, this is how you re-enter the turn order at
    this time.

    Examples:

        .act

        .undefer

        .undefer @ Mook

    """
    result = _issue_command({
        "command": "undefer",
        "entity": entity,
    })
    if await standard_abort(message, result):
        return

    order = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_order(order))


@cmds.register(r"[.](order_list|order_show|order[?]|order\s*$)")
async def _order_list(message):
    """
    Aliases: order_list, order_show, order?, order [followed by nothing]

    Print out the current turn order.

    This will display the entities in the turn order so far while they are
    being added, and will display the turn order tracker once the turns have
    actually begun.
    """
    game = _get_game()
    await message.channel.send(pretty_print_order(game.get("order", {})))


@cmds.register(r"[.](create_entity|entity[+]|e[+])\s+(?P<props>.*)")
async def _create_entity(message, props):
    """
    Aliases: create_entity, entity+, e+

    Add a new entity to the bot.  Intended for managing NPCs.

    Provide stress track sizes, starting fate points, and refresh if
    applicable.

    Examples:

        Mook with 4 physical stress boxes, 2 mental, and one fate point:

        .e+ Mook p 4 m 2 f 1

        You can identify the properties with more explicit names:

        .e+ Mook2 physical 4 mental 2 fp 1

    Tips:

        For many bot commands you can add flavorful annotation to the command,
        but this is not one of those.

    """
    splitted = props.split(" ", 1)
    if len(splitted) == 1:
        (name, other) = (splitted[0], "")
    else:
        (name, other) = splitted

    _rev_canonical = {
        "fate": "fate fp f",
        "refresh": "refresh ref refsh r",
        "physical": "physical phys phy ph p",
        "mental": "mental mentl ment men m",
        "hunger": "hunger hungr hung hng hngr hr h",
        "social": "social soc",
    }
    _canonical = {}
    for (key, value_str) in _rev_canonical.items():
        for v in value_str.split():
            _canonical[v] = key
    other_map = {
        _canonical.get(key.lower(), key.lower()): value
        for (key, value) in sliding_window(2, other.split())
    }

    fate = int(other_map.get("fate", 0))
    refresh = int(other_map.get("refresh", 0))
    stress_maxes = {
        "physical": int(other_map.get("physical", 0)),
        "mental": int(other_map.get("mental", 0)),
        "hunger": int(other_map.get("hunger", 0)),
        "social": int(other_map.get("social", 0)),
    }
    result = _issue_command({
        "command": "create_entity",
        "name": name,
        "stress_maxes": stress_maxes,
        "refresh": refresh,
        "fate": fate,
    })
    if await standard_abort(message, result):
        return

    entity = get_in(["result", "result"], result)
    await message.channel.send(pretty_print_entity(entity))


@cmds.register(r"[.](remove_entity|entity[-]|e[-])(\s+(?P<entity>\w+))?")
@targeted
async def _remove_entity(message, entity=None):
    """
    Aliases: remove_entity, entity-, e-

    Targeted.  (See .target)

    Completely remove an entity from the bot.  Intended for managing NPCs.
    Don't do this with player characters!

    Examples:

        .e- Mook

        .e- @ Mook

        .entity- Warehouse

    """
    result = _issue_command({"command": "remove_entity", "entity": entity})
    if await standard_abort(message, result):
        return

    ent = get_in(["result", "result"], result)
    await message.channel.send(f"Removed entity {entity}")


@cmds.register(r"[.](help)(\s+(?P<command>\w+))?")
async def _help(message, command=None):
    """
    Aliases: help

    Get help on a command.  If you say .help without a command, it will list
    the available commands.

    Examples:

        .help

        .help add_aspect

    """
    if command is None:
        quoted_function_names = [
            f"`{name}`" for name in cmds.all_function_names()
        ]
        batches = partition_all(5, quoted_function_names)
        out_f = "\n".join(" ".join(batch) for batch in batches)
        await message.channel.send(out_f)

    else:
        with_dot = cmds.all_matches("." + command)
        without_dot = cmds.all_matches(command)
        as_function = cmds.search_by_function_name(command)
        matches = list(unique(with_dot + without_dot + as_function))

        exact = [m for m in matches if cmds.function_name(m) == command.strip()]
        if exact:
            matches = exact

        if len(matches) == 0:
            await message.channel.send(f"No commands matching '{command}'")
        elif len(matches) == 1:
            name = cmds.function_name(matches[0])
            if matches[0].__doc__ is None:
                docstring_raw = "(No help available)"
            else:
                docstring_raw = textwrap.dedent(
                        matches[0].__doc__
                        .replace("\n\n", "%DOUBLE_NEWLINE%")
                        .replace("\n", "")
                        .replace("%DOUBLE_NEWLINE%", "\n\n")
                        .replace("    ", " ")
                )
            docstring = f"```\n{docstring_raw}\n```"
            out_f = f"Help for command `{name}`:\n{docstring}"
            await message.channel.send(out_f)
        else:
            names = [cmds.function_name(m) for m in matches]
            quoted_names = [f"`{name}`" for name in names]
            names = " ".join(quoted_names)
            await message.channel.send(
                f"Ambiguous.  Did you mean one of the following?\n"
                f"{' '.join(quoted_names)}"
            )


#
# Entry point
#

if __name__ == "__main__":
    bot.run(config["token"])
