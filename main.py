# main.py
# Requirements: discord.py==2.6.0, python-dateutil
import os
import json
import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dateutil import parser as date_parser

import discord
from discord import app_commands
from discord.ext import commands, tasks
import dotenv
from dotenv import load_dotenv

load_dotenv()


# ---- Config loading (env or config.json) ----
def load_config():
    cfg = {}

    # Load config.json first if it exists
    try:
        with open("config.json", "r") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    except Exception as e:
        print(f"Could not load config.json: {e}")
        cfg = {}

    # Environment variables override config.json
    env_map = {
        "GUILD_ID": "guild_id",
        "ROLE_CAPTAIN_ID": ("roles", "captain"),
        "ROLE_CO_CAPTAIN_ID": ("roles", "co_captain"),
        "ROLE_ADMIN_ID": ("roles", "admin"),
        "ROLE_HEAD_ID": ("roles", "head"),
        "ROLE_HEAD_CASTER_ID": ("roles", "head_caster"),
        "ROLE_REF_ID": ("roles", "referee"),
        "ROLE_CASTER_ID": ("roles", "caster"),
        "ROLE_BOARD_ID": ("roles", "board"),
        "CHANNEL_TRANSACTIONS_ID": "transactions_channel",
        "CHANNEL_MATCH_SCORES_ID": "match_scores_channel",
        "CHANNEL_MATCH_TIMES_ID": "match_times_channel",
        "CHANNEL_ASSIGNMENTS_ID": "assignments_channel",
        "DEFAULT_ROSTER_LIMIT": "roster_limit",
        "DEFAULT_TIMEZONE": "timezone"
    }


    for env, key in env_map.items():
        val = os.getenv(env)
        if val is None or val == "":
            continue

        if isinstance(key, tuple):
            section, subkey = key
            cfg.setdefault(section, {})
            cfg[section][subkey] = val
        else:
            cfg[key] = val

    # Defaults
    cfg.setdefault("roles", {})
    cfg.setdefault("roster_limit", 10)
    cfg.setdefault("timezone", "America/New_York")

    return cfg

CONFIG = load_config()

ROSTER_LOCK_ALL = False
TEAM_PLAYER_ROLE_NAME = "Team Player"


# ---- JSON storage helpers ----
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

def load_json(name, default):
    path = os.path.join(DATA_DIR, name + ".json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default

def save_json(name, data):
    path = os.path.join(DATA_DIR, name + ".json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

# Persistent stores
TEAMS = load_json("teams", {})          # team_id -> {name, role_id, captain_id, co_captains:[], players:[]}
INVITES = load_json("invites", {})      # invite_id -> {team_id, inviter_id, user_id, status}
MATCHES = load_json("matches", {})      # match_id -> {...}
TRANSACTIONS = load_json("transactions", [])  # list of transaction messages
WARNINGS = load_json("warnings", {})    # user_id -> list of warnings



def get_member_team(member: discord.Member):
    """
    Returns (team_id, team_data) for the member's team.
    Checks captain_id, co_captains, players, and team role.
    """
    for tid, team in TEAMS.items():
        ids = [str(team.get("captain_id"))]
        ids += [str(x) for x in team.get("co_captains", [])]
        ids += [str(x) for x in team.get("players", [])]

        if str(member.id) in ids:
            return tid, team

        role_id = team.get("role_id")
        if role_id:
            try:
                if any(r.id == int(role_id) for r in member.roles):
                    return tid, team
            except Exception:
                pass

    return None, None


async def get_or_create_team_player_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name=TEAM_PLAYER_ROLE_NAME)
    if role:
        return role

    try:
        role = await guild.create_role(
            name=TEAM_PLAYER_ROLE_NAME,
            reason="Created Team Player role for roster system"
        )
        return role
    except Exception:
        return None


async def add_player_to_team(guild: discord.Guild, member: discord.Member, team_id: str, team: dict):
    """
    Adds member to team data and gives roles.
    """
    team.setdefault("players", [])

    if str(member.id) not in [str(x) for x in team["players"]]:
        team["players"].append(str(member.id))

    # Team role
    role_id = team.get("role_id")
    if role_id:
        team_role = guild.get_role(int(role_id))
        if team_role and team_role not in member.roles:
            try:
                await member.add_roles(team_role, reason="Added to team roster")
            except Exception:
                pass

    # Team Player role
    team_player_role = await get_or_create_team_player_role(guild)
    if team_player_role and team_player_role not in member.roles:
        try:
            await member.add_roles(team_player_role, reason="Added Team Player role")
        except Exception:
            pass

    save_all()


async def remove_player_from_team(guild: discord.Guild, member: discord.Member, team_id: str, team: dict):
    """
    Removes member from team data and removes team/player/co-captain roles where needed.
    """
    uid = str(member.id)

    team["players"] = [str(p) for p in team.get("players", []) if str(p) != uid]
    team["co_captains"] = [str(c) for c in team.get("co_captains", []) if str(c) != uid]

    # Remove team role
    role_id = team.get("role_id")
    if role_id:
        team_role = guild.get_role(int(role_id))
        if team_role and team_role in member.roles:
            try:
                await member.remove_roles(team_role, reason="Removed from team roster")
            except Exception:
                pass

    # Remove Team Player role
    team_player_role = discord.utils.get(guild.roles, name=TEAM_PLAYER_ROLE_NAME)
    if team_player_role and team_player_role in member.roles:
        try:
            await member.remove_roles(team_player_role, reason="Removed Team Player role")
        except Exception:
            pass

    # Remove co-captain role
    co_role = get_role_by_cfg(guild, "co_captain")
    if co_role and co_role in member.roles:
        try:
            await member.remove_roles(co_role, reason="Removed from team/co-captain")
        except Exception:
            pass

    save_all()


def build_roster_text(team: dict):
    captain_id = team.get("captain_id")
    co_caps = team.get("co_captains", [])
    players = team.get("players", [])

    text = f"## Captain Panel — {team.get('name', 'Team')}\n\n"

    text += "**Captain:**\n"
    text += f"> <@{captain_id}>\n\n" if captain_id else "> None\n\n"

    text += "**Co-Captains:**\n"
    if co_caps:
        for cc in co_caps:
            text += f"> <@{cc}>\n"
    else:
        text += "> None\n"

    text += "\n**Players:**\n"
    if players:
        for p in players:
            text += f"> <@{p}>\n"
    else:
        text += "> None\n"

    text += f"\n**Roster Size:** {len(players)}/{CONFIG.get('roster_limit', 10)}"

    if ROSTER_LOCK_ALL:
        text += "\n\n**Roster Lock:** Enabled"

    return text





SETTINGS = load_json("settings", {
    "roster_lock_all": False
})

def is_roster_locked():
    return bool(SETTINGS.get("roster_lock_all", False))

def set_roster_lock(value: bool):
    SETTINGS["roster_lock_all"] = value
    save_json("settings", SETTINGS)


# ---- Bot / Intents ----
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# Utility helpers
def get_role_by_cfg(guild, role_key):
    role_id = None
    roles_cfg = CONFIG.get("roles", {})
    if roles_cfg and role_key in roles_cfg:
        try:
            role_id = int(roles_cfg[role_key])
        except:
            role_id = None
    if role_id:
        return guild.get_role(role_id)
    # fallback by common name
    names = {
        "captain": "Captain",
        "co_captain": "Co-Captain",
        "admin": "Admin",
        "head": "Head",
        "head_caster": "Head Caster",
        "referee": "Referee",
        "caster": "Caster",
        "board": "Board"
    }

    return discord.utils.get(guild.roles, name=names.get(role_key))

def log_transaction(guild, content):
    TRANSACTIONS.append({
        "time": datetime.now(timezone.utc).isoformat(),
        "content": content
    })
    save_json("transactions", TRANSACTIONS)
    # send to channel if configured
    ch_id = CONFIG.get("transactions_channel")
    if ch_id:
        try:
            ch = guild.get_channel(int(ch_id))
            if ch:
                asyncio.create_task(ch.send(content))
        except Exception:
            pass

def save_all():
    save_json("teams", TEAMS)
    save_json("invites", INVITES)
    save_json("matches", MATCHES)
    save_json("warnings", WARNINGS)
    save_json("transactions", TRANSACTIONS)
    save_json("settings", SETTINGS)

# Permission checks
def is_captain(member, team_id=None):
    # simple: check role
    role = get_role_by_cfg(member.guild, "captain")
    return role in member.roles

def is_co_captain(member):
    role = get_role_by_cfg(member.guild, "co_captain")
    return role in member.roles

def is_admin(member):
    role = get_role_by_cfg(member.guild, "admin")
    return role in member.roles or member.guild_permissions.administrator



def is_admin_or_above(member: discord.Member):
    if member.guild_permissions.administrator:
        return True

    allowed_keys = ["admin", "head", "board"]
    for key in allowed_keys:
        role = get_role_by_cfg(member.guild, key)
        if role and role in member.roles:
            return True

    return False


def get_team_player_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name="Team Player")
    return role


async def add_member_to_team_roles(member: discord.Member, team: dict, reason: str = None):
    roles_to_add = []

    if team.get("role_id"):
        team_role = member.guild.get_role(int(team["role_id"]))
        if team_role and team_role not in member.roles:
            roles_to_add.append(team_role)

    team_player_role = get_team_player_role(member.guild)
    if team_player_role and team_player_role not in member.roles:
        roles_to_add.append(team_player_role)

    if roles_to_add:
        await member.add_roles(*roles_to_add, reason=reason)


async def remove_member_from_team_roles(member: discord.Member, team: dict, reason: str = None):
    roles_to_remove = []

    if team.get("role_id"):
        team_role = member.guild.get_role(int(team["role_id"]))
        if team_role and team_role in member.roles:
            roles_to_remove.append(team_role)

    team_player_role = get_team_player_role(member.guild)
    if team_player_role and team_player_role in member.roles:
        roles_to_remove.append(team_player_role)

    if roles_to_remove:
        await member.remove_roles(*roles_to_remove, reason=reason)


def get_member_team_as_co_captain(member: discord.Member):
    for team_id, team in TEAMS.items():
        if str(member.id) in [str(x) for x in team.get("co_captains", [])]:
            return team_id, team
    return None, None


def is_user_on_any_team(user_id: int | str):
    user_id = str(user_id)

    for team in TEAMS.values():
        if str(team.get("captain_id")) == user_id:
            return True

        if user_id in [str(x) for x in team.get("co_captains", [])]:
            return True

        if user_id in [str(x) for x in team.get("players", [])]:
            return True

    return False



async def get_or_create_team_player_role(guild: discord.Guild):
    role = get_team_player_role(guild)
    if role:
        return role

    try:
        role = await guild.create_role(
            name="Team Player",
            reason="Created by bot for team roster system"
        )
        return role
    except Exception:
        return None


def find_team_by_role_id(role_id):
    for team_id, team in TEAMS.items():
        if str(team.get("role_id")) == str(role_id):
            return team_id, team
    return None, None


def clean_team_id(name: str):
    return name.lower().replace(" ", "_").replace("-", "_")


def parse_hex_color(hex_color: str):
    hex_color = hex_color.strip().replace("#", "")

    if len(hex_color) != 6:
        raise ValueError("Invalid hex color length")

    return discord.Color(int(hex_color, 16))


def is_admin_or_above(member: discord.Member):
    if member.guild_permissions.administrator:
        return True

    allowed_keys = ["admin", "head", "board"]
    for key in allowed_keys:
        role = get_role_by_cfg(member.guild, key)
        if role and role in member.roles:
            return True

    return False


def get_team_player_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name="Team Player")
    return role


async def get_or_create_team_player_role(guild: discord.Guild):
    role = get_team_player_role(guild)
    if role:
        return role

    try:
        role = await guild.create_role(
            name="Team Player",
            reason="Created by bot for team roster system"
        )
        return role
    except Exception:
        return None


def find_team_by_role_id(role_id):
    for team_id, team in TEAMS.items():
        if str(team.get("role_id")) == str(role_id):
            return team_id, team
    return None, None


def clean_team_id(name: str):
    return name.lower().replace(" ", "_").replace("-", "_")


def parse_hex_color(hex_color: str):
    hex_color = hex_color.strip().replace("#", "")

    if len(hex_color) != 6:
        raise ValueError("Invalid hex color length")

    return discord.Color(int(hex_color, 16))


async def remove_team_member_roles(member: discord.Member, team_role: discord.Role = None):
    roles_to_remove = []

    captain_role = get_role_by_cfg(member.guild, "captain")
    co_captain_role = get_role_by_cfg(member.guild, "co_captain")
    team_player_role = get_team_player_role(member.guild)

    for role in [captain_role, co_captain_role, team_player_role, team_role]:
        if role and role in member.roles:
            roles_to_remove.append(role)

    if roles_to_remove:
        try:
            await member.remove_roles(
                *roles_to_remove,
                reason="Team disband/admin roster update"
            )
        except Exception:
            pass




# ---- UI components helpers ----
def make_team_select(guild):
    options = []
    for team_id, t in TEAMS.items():
        options.append(discord.SelectOption(label=t.get("name","Team"), value=team_id))
    return options

# ---- Commands ----
@tree.command(name="roster", description="View a team's roster")
@app_commands.describe(team="Select a team (leave blank to see all teams)")
async def roster(interaction: discord.Interaction, team: str = None):
    await interaction.response.defer()
    # if team provided as ID or name
    if not team:
        # choose via select menu
        if not TEAMS:
            return await interaction.followup.send("No teams configured.")
        options = make_team_select(interaction.guild)
        select = discord.ui.Select(placeholder="Choose a roster", options=options, custom_id="roster_select")
        view = discord.ui.View(timeout=60)
        async def select_callback(select_inter):
            team_id = select_inter.data["values"][0]
            await show_roster(select_inter, team_id)
        select.callback = select_callback
        view.add_item(select)
        return await interaction.followup.send("Pick a team:", view=view, ephemeral=True)
    else:
        # try to find team by id or name
        team_obj = None
        if team in TEAMS:
            team_obj = TEAMS[team]
        else:
            for k,v in TEAMS.items():
                if v.get("name","").lower() == team.lower():
                    team_obj = v; team = k; break
        if not team_obj:
            return await interaction.followup.send("Team not found.")
        await show_roster(interaction, team)

async def show_roster(inter, team_id):
    t = TEAMS.get(team_id)
    if not t:
        return await inter.followup.send("Team not found.")
    captain = t.get("captain_id")
    co_caps = t.get("co_captains", [])
    players = t.get("players", [])
    content = f"Roster for {t.get('name','Team')}\n\nCaptain:\n> * <@{captain}>\n\nCo-Captains:\n"
    for cc in co_caps[:10]:
        content += f"> * <@{cc}>\n"
    content += "\nPlayers:\n"
    for p in players[:10]:
        content += f"> * <@{p}>\n"
    content += f"\n{len(players)}/{CONFIG.get('roster_limit')}\n"
    await inter.followup.send(content)

@tree.command(name="leave", description="Leave your team")
async def leave(interaction: discord.Interaction):
    member = interaction.user

    if is_roster_locked():
        return await interaction.response.send_message(
            "Rosters are currently locked.",
            ephemeral=True
        )
    # find team
    found = None
    for tid,t in TEAMS.items():
        if str(member.id) in [str(x) for x in t.get("players",[])]:
            found = (tid,t); break
    if not found:
        return await interaction.response.send_message("You are not on any team.", ephemeral=True)
    tid, team = found
    team["players"] = [p for p in team.get("players",[]) if str(p) != str(member.id)]
    # remove any role if team role was set
    if team.get("role_id"):
        role = interaction.guild.get_role(int(team["role_id"]))
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason="Left team via /leave")
            except:
                pass
        team_player_role = get_team_player_role(interaction.guild)
    if team_player_role and team_player_role in member.roles:
        try:
            await member.remove_roles(
                team_player_role,
                reason="Left team via /leave"
            )
        except Exception:
            pass

    save_all()
    tx = f"<@{member.id}> Has Left **{team.get('name','Team')}**"
    log_transaction(interaction.guild, tx)
    await interaction.response.send_message("You left the team.", ephemeral=True)

@tree.command(name="score", description="Report a match score (admin only)")
@app_commands.describe(winner="Winning team id or name", loser="Losing team id or name", score="Score (e.g., 2-0)")
async def score(interaction: discord.Interaction, winner: str, loser: str, score: str):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Admin perms required.", ephemeral=True)
    # find teams
    def find_team(x):
        if x in TEAMS: return TEAMS[x]
        for k,v in TEAMS.items():
            if v.get("name","").lower() == x.lower(): return v
        return None
    w = find_team(winner); l = find_team(loser)
    if not w or not l:
        return await interaction.response.send_message("Team(s) not found.", ephemeral=True)
    content = f"{w.get('name')} vs {l.get('name')}\n> Winner: {w.get('name')}\n> Score: {score}\n> Loser: {l.get('name')}"
    log_transaction(interaction.guild, content)
    await interaction.response.send_message("Score posted.", ephemeral=True)

# captain-panel
@tree.command(name="captain-panel", description="Captain panel")
async def captain_panel(interaction: discord.Interaction):
    global ROSTER_LOCK_ALL

    if not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

    guild = interaction.guild
    member = interaction.user

    # Must have captain role
    if not is_captain(member):
        return await interaction.response.send_message("Captain role required.", ephemeral=True)

    # Find team where user is captain
    team_id = None
    team = None

    for tid, t in TEAMS.items():
        if str(t.get("captain_id")) == str(member.id):
            team_id = tid
            team = t
            break

    if not team:
        return await interaction.response.send_message("No team found for you. Contact an admin.", ephemeral=True)

    roster_limit = int(CONFIG.get("roster_limit", 10))

    class CaptainPanelView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)

        async def interaction_check(self, btn_inter: discord.Interaction) -> bool:
            if str(btn_inter.user.id) != str(member.id):
                await btn_inter.response.send_message("Only this team's captain can use this panel.", ephemeral=True)
                return False
            return True

        @discord.ui.button(label="Invite", style=discord.ButtonStyle.primary)
        async def invite_button(self, btn_inter: discord.Interaction, button: discord.ui.Button):
            if ROSTER_LOCK_ALL:
                return await btn_inter.response.send_message("Rosters are currently locked.", ephemeral=True)

            current_size = len(team.get("players", []))
            if current_size >= roster_limit:
                return await btn_inter.response.send_message("Your roster is full.", ephemeral=True)

            options = []

            for m in guild.members:
                if m.bot:
                    continue

                # skip people already on a team
                existing_tid, existing_team = get_member_team(m)
                if existing_team:
                    continue

                options.append(discord.SelectOption(label=m.display_name[:100], value=str(m.id)))

                if len(options) >= 25:
                    break

            if not options:
                return await btn_inter.response.send_message("No available members found to invite.", ephemeral=True)

            select = discord.ui.Select(
                placeholder="Choose a member to invite",
                options=options,
                min_values=1,
                max_values=1
            )

            invite_view = discord.ui.View(timeout=60)

            async def select_callback(select_inter: discord.Interaction):
                user_id = select.values[0]
                invited_member = guild.get_member(int(user_id))

                if not invited_member:
                    return await select_inter.response.send_message("Member not found.", ephemeral=True)

                existing_tid, existing_team = get_member_team(invited_member)
                if existing_team:
                    return await select_inter.response.send_message("That member is already on a team.", ephemeral=True)

                if len(team.get("players", [])) >= roster_limit:
                    return await select_inter.response.send_message("Your roster is now full.", ephemeral=True)

                invite_id = secrets.token_hex(8)

                INVITES[invite_id] = {
                    "team_id": team_id,
                    "inviter_id": str(member.id),
                    "user_id": str(invited_member.id),
                    "status": "pending"
                }

                save_all()

                class InviteResponseView(discord.ui.View):
                    def __init__(self):
                        super().__init__(timeout=86400)

                    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
                    async def accept_invite(self, invite_inter: discord.Interaction, accept_button: discord.ui.Button):
                        if str(invite_inter.user.id) != str(invited_member.id):
                            return await invite_inter.response.send_message("This invite is not for you.", ephemeral=True)

                        if INVITES.get(invite_id, {}).get("status") != "pending":
                            return await invite_inter.response.send_message("This invite is no longer active.", ephemeral=True)

                        existing_tid, existing_team = get_member_team(invited_member)
                        if existing_team:
                            INVITES[invite_id]["status"] = "cancelled"
                            save_all()
                            return await invite_inter.response.edit_message(
                                content="You are already on a team, so this invite was cancelled.",
                                view=None
                            )

                        if len(team.get("players", [])) >= roster_limit:
                            INVITES[invite_id]["status"] = "cancelled"
                            save_all()
                            return await invite_inter.response.edit_message(
                                content="This team's roster is full.",
                                view=None
                            )

                        await add_player_to_team(guild, invited_member, team_id, team)

                        INVITES[invite_id]["status"] = "accepted"
                        save_all()

                        log_transaction(guild, f"<@{invited_member.id}> Has Joined **{team.get('name', 'Team')}**")

                        await invite_inter.response.edit_message(
                            content=f"You accepted the invite to **{team.get('name', 'Team')}**.",
                            view=None
                        )

                    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
                    async def decline_invite(self, invite_inter: discord.Interaction, decline_button: discord.ui.Button):
                        if str(invite_inter.user.id) != str(invited_member.id):
                            return await invite_inter.response.send_message("This invite is not for you.", ephemeral=True)

                        INVITES[invite_id]["status"] = "declined"
                        save_all()

                        await invite_inter.response.edit_message(
                            content=f"You declined the invite to **{team.get('name', 'Team')}**.",
                            view=None
                        )

                try:
                    await invited_member.send(
                        f"You've been invited to **{team.get('name', 'Team')}** by {member.mention}.\n"
                        f"Use the buttons below to accept or decline.",
                        view=InviteResponseView()
                    )

                    await select_inter.response.send_message(
                        f"Invite sent to {invited_member.mention}.",
                        ephemeral=True
                    )

                except Exception:
                    INVITES[invite_id]["status"] = "failed_dm"
                    save_all()

                    await select_inter.response.send_message(
                        "I could not DM that user. They may have DMs disabled.",
                        ephemeral=True
                    )

            select.callback = select_callback
            invite_view.add_item(select)

            await btn_inter.response.send_message("Select a member to invite:", view=invite_view, ephemeral=True)

        @discord.ui.button(label="Kick", style=discord.ButtonStyle.danger)
        async def kick_button(self, btn_inter: discord.Interaction, button: discord.ui.Button):
            if ROSTER_LOCK_ALL:
                return await btn_inter.response.send_message("Rosters are currently locked.", ephemeral=True)

            options = []

            all_ids = []

            for pid in team.get("players", []):
                if str(pid) not in all_ids:
                    all_ids.append(str(pid))

            for cc in team.get("co_captains", []):
                if str(cc) not in all_ids:
                    all_ids.append(str(cc))

            for uid in all_ids:
                m = guild.get_member(int(uid))
                if not m:
                    continue

                label = m.display_name
                if str(uid) in [str(x) for x in team.get("co_captains", [])]:
                    label += " — Co-Captain"

                options.append(discord.SelectOption(label=label[:100], value=str(uid)))

                if len(options) >= 25:
                    break

            if not options:
                return await btn_inter.response.send_message("There are no players to kick.", ephemeral=True)

            select = discord.ui.Select(
                placeholder="Choose a player to kick",
                options=options,
                min_values=1,
                max_values=1
            )

            kick_view = discord.ui.View(timeout=60)

            async def kick_callback(select_inter: discord.Interaction):
                user_id = select.values[0]
                kicked_member = guild.get_member(int(user_id))

                if not kicked_member:
                    return await select_inter.response.send_message("Member not found.", ephemeral=True)

                await remove_player_from_team(guild, kicked_member, team_id, team)

                log_transaction(
                    guild,
                    f"<@{kicked_member.id}> Has Been Kicked From **{team.get('name', 'Team')}**"
                )

                await select_inter.response.send_message(
                    f"{kicked_member.mention} was kicked from **{team.get('name', 'Team')}**.",
                    ephemeral=True
                )

            select.callback = kick_callback
            kick_view.add_item(select)

            await btn_inter.response.send_message("Select a player to kick:", view=kick_view, ephemeral=True)

        @discord.ui.button(label="Add Co-Captain", style=discord.ButtonStyle.secondary)
        async def add_co_button(self, btn_inter: discord.Interaction, button: discord.ui.Button):
            if ROSTER_LOCK_ALL:
                return await btn_inter.response.send_message("Rosters are currently locked.", ephemeral=True)

            options = []

            for pid in team.get("players", []):
                if str(pid) in [str(x) for x in team.get("co_captains", [])]:
                    continue

                m = guild.get_member(int(pid))
                if not m:
                    continue

                options.append(discord.SelectOption(label=m.display_name[:100], value=str(pid)))

                if len(options) >= 25:
                    break

            if not options:
                return await btn_inter.response.send_message("No eligible players to promote.", ephemeral=True)

            select = discord.ui.Select(
                placeholder="Choose a player to make Co-Captain",
                options=options,
                min_values=1,
                max_values=1
            )

            co_view = discord.ui.View(timeout=60)

            async def add_co_callback(select_inter: discord.Interaction):
                user_id = select.values[0]
                co_member = guild.get_member(int(user_id))

                if not co_member:
                    return await select_inter.response.send_message("Member not found.", ephemeral=True)

                team.setdefault("co_captains", [])

                if str(user_id) not in [str(x) for x in team["co_captains"]]:
                    team["co_captains"].append(str(user_id))

                co_role = get_role_by_cfg(guild, "co_captain")
                if co_role and co_role not in co_member.roles:
                    try:
                        await co_member.add_roles(co_role, reason="Promoted to Co-Captain")
                    except Exception:
                        pass

                save_all()

                log_transaction(
                    guild,
                    f"<@{co_member.id}> Has Been Promoted To Co-Captain Of **{team.get('name', 'Team')}**"
                )

                await select_inter.response.send_message(
                    f"{co_member.mention} is now a Co-Captain.",
                    ephemeral=True
                )

            select.callback = add_co_callback
            co_view.add_item(select)

            await btn_inter.response.send_message("Select a player to promote:", view=co_view, ephemeral=True)

        @discord.ui.button(label="Remove Co-Captain", style=discord.ButtonStyle.secondary)
        async def remove_co_button(self, btn_inter: discord.Interaction, button: discord.ui.Button):
            if ROSTER_LOCK_ALL:
                return await btn_inter.response.send_message("Rosters are currently locked.", ephemeral=True)

            options = []

            for cc in team.get("co_captains", []):
                m = guild.get_member(int(cc))
                if not m:
                    continue

                options.append(discord.SelectOption(label=m.display_name[:100], value=str(cc)))

                if len(options) >= 25:
                    break

            if not options:
                return await btn_inter.response.send_message("There are no Co-Captains to remove.", ephemeral=True)

            select = discord.ui.Select(
                placeholder="Choose a Co-Captain to remove",
                options=options,
                min_values=1,
                max_values=1
            )

            remove_view = discord.ui.View(timeout=60)

            async def remove_co_callback(select_inter: discord.Interaction):
                user_id = select.values[0]
                co_member = guild.get_member(int(user_id))

                team["co_captains"] = [
                    str(c) for c in team.get("co_captains", [])
                    if str(c) != str(user_id)
                ]

                if co_member:
                    co_role = get_role_by_cfg(guild, "co_captain")
                    if co_role and co_role in co_member.roles:
                        try:
                            await co_member.remove_roles(co_role, reason="Removed Co-Captain")
                        except Exception:
                            pass

                save_all()

                log_transaction(
                    guild,
                    f"<@{user_id}> Has Been Removed As Co-Captain Of **{team.get('name', 'Team')}**"
                )

                await select_inter.response.send_message(
                    f"<@{user_id}> is no longer a Co-Captain.",
                    ephemeral=True
                )

            select.callback = remove_co_callback
            remove_view.add_item(select)

            await btn_inter.response.send_message("Select a Co-Captain to remove:", view=remove_view, ephemeral=True)

        @discord.ui.button(label="Transfer Captain", style=discord.ButtonStyle.secondary)
        async def transfer_button(self, btn_inter: discord.Interaction, button: discord.ui.Button):
            if ROSTER_LOCK_ALL:
                return await btn_inter.response.send_message("Rosters are currently locked.", ephemeral=True)

            options = []

            possible_ids = []

            for pid in team.get("players", []):
                if str(pid) not in possible_ids:
                    possible_ids.append(str(pid))

            for cc in team.get("co_captains", []):
                if str(cc) not in possible_ids:
                    possible_ids.append(str(cc))

            for uid in possible_ids:
                if str(uid) == str(member.id):
                    continue

                m = guild.get_member(int(uid))
                if not m:
                    continue

                options.append(discord.SelectOption(label=m.display_name[:100], value=str(uid)))

                if len(options) >= 25:
                    break

            if not options:
                return await btn_inter.response.send_message("No eligible members to transfer captain to.", ephemeral=True)

            select = discord.ui.Select(
                placeholder="Choose the new Captain",
                options=options,
                min_values=1,
                max_values=1
            )

            transfer_view = discord.ui.View(timeout=60)

            async def transfer_callback(select_inter: discord.Interaction):
                new_captain_id = select.values[0]
                old_captain_id = str(team.get("captain_id"))

                new_captain = guild.get_member(int(new_captain_id))
                old_captain = guild.get_member(int(old_captain_id))

                if not new_captain:
                    return await select_inter.response.send_message("New captain member not found.", ephemeral=True)

                captain_role = get_role_by_cfg(guild, "captain")
                co_role = get_role_by_cfg(guild, "co_captain")

                # Update data
                team["captain_id"] = str(new_captain_id)

                # New captain should not be listed as normal player/co-captain
                team["players"] = [
                    str(p) for p in team.get("players", [])
                    if str(p) != str(new_captain_id)
                ]

                team["co_captains"] = [
                    str(c) for c in team.get("co_captains", [])
                    if str(c) != str(new_captain_id)
                ]

                # Old captain becomes player
                if old_captain_id and old_captain_id not in [str(p) for p in team.get("players", [])]:
                    team.setdefault("players", []).append(old_captain_id)

                # Role changes
                if captain_role:
                    try:
                        if old_captain and captain_role in old_captain.roles:
                            await old_captain.remove_roles(captain_role, reason="Captain transferred")
                    except Exception:
                        pass

                    try:
                        if captain_role not in new_captain.roles:
                            await new_captain.add_roles(captain_role, reason="Captain transferred")
                    except Exception:
                        pass

                if co_role and co_role in new_captain.roles:
                    try:
                        await new_captain.remove_roles(co_role, reason="Captain transferred")
                    except Exception:
                        pass

                # Ensure old captain has team role and Team Player role
                if old_captain:
                    await add_player_to_team(guild, old_captain, team_id, team)

                save_all()

                log_transaction(
                    guild,
                    f"<@{old_captain_id}> Has Transferred Captain Of **{team.get('name', 'Team')}** To <@{new_captain_id}>"
                )

                await select_inter.response.send_message(
                    f"Captain transferred to <@{new_captain_id}>.",
                    ephemeral=True
                )

            select.callback = transfer_callback
            transfer_view.add_item(select)

            await btn_inter.response.send_message("Select the new Captain:", view=transfer_view, ephemeral=True)

    await interaction.response.send_message(
        build_roster_text(team),
        view=CaptainPanelView(),
        ephemeral=True
    )

# co-captain-panel: similar but fewer buttons
@tree.command(name="co-captain-panel", description="Co-captain panel (co-captains only)")
async def co_captain_panel(interaction: discord.Interaction):
    global ROSTER_LOCK_ALL

    if ROSTER_LOCK_ALL:
        return await interaction.response.send_message(
            "Rosters are currently locked by staff.",
            ephemeral=True
        )

    if not is_co_captain(interaction.user):
        return await interaction.response.send_message(
            "Co-Captain role required.",
            ephemeral=True
        )

    team_id, team = get_member_team_as_co_captain(interaction.user)

    if not team:
        return await interaction.response.send_message(
            "No team found for you.",
            ephemeral=True
        )

    guild = interaction.guild
    roster_limit = int(CONFIG.get("roster_limit", 10))

    def build_panel_text():
        captain_id = team.get("captain_id")
        co_captains = team.get("co_captains", [])
        players = team.get("players", [])

        text = f"**Co-Captain Panel — {team.get('name', 'Team')}**\n\n"

        text += "**Captain:**\n"
        text += f"> <@{captain_id}>\n\n" if captain_id else "> None\n\n"

        text += "**Co-Captains:**\n"
        if co_captains:
            for cc in co_captains:
                text += f"> <@{cc}>\n"
        else:
            text += "> None\n"

        text += "\n**Players:**\n"
        if players:
            for player_id in players:
                text += f"> <@{player_id}>\n"
        else:
            text += "> None\n"

        text += f"\n**Roster:** {len(players)}/{roster_limit}"

        return text

    class CoCaptainInviteButton(discord.ui.Button):
        def __init__(self):
            super().__init__(
                label="Invite Player",
                style=discord.ButtonStyle.primary
            )

        async def callback(self, button_interaction: discord.Interaction):
            global ROSTER_LOCK_ALL

            if ROSTER_LOCK_ALL:
                return await button_interaction.response.send_message(
                    "Rosters are currently locked by staff.",
                    ephemeral=True
                )

            if button_interaction.user.id != interaction.user.id:
                return await button_interaction.response.send_message(
                    "This is not your panel.",
                    ephemeral=True
                )

            current_players = team.get("players", [])

            if len(current_players) >= roster_limit:
                return await button_interaction.response.send_message(
                    "Your roster is full.",
                    ephemeral=True
                )

            options = []

            for member in guild.members:
                if member.bot:
                    continue

                if member.id == interaction.user.id:
                    continue

                if is_user_on_any_team(member.id):
                    continue

                options.append(
                    discord.SelectOption(
                        label=member.display_name[:100],
                        value=str(member.id)
                    )
                )

                if len(options) >= 25:
                    break

            if not options:
                return await button_interaction.response.send_message(
                    "No available members found to invite.",
                    ephemeral=True
                )

            select = discord.ui.Select(
                placeholder="Select a player to invite...",
                options=options,
                min_values=1,
                max_values=1
            )

            invite_view = discord.ui.View(timeout=60)

            async def select_callback(select_interaction: discord.Interaction):
                selected_user_id = select_interaction.data["values"][0]
                selected_member = guild.get_member(int(selected_user_id))

                if not selected_member:
                    return await select_interaction.response.send_message(
                        "That member could not be found.",
                        ephemeral=True
                    )

                if is_user_on_any_team(selected_member.id):
                    return await select_interaction.response.send_message(
                        "That member is already on a team.",
                        ephemeral=True
                    )

                invite_id = secrets.token_hex(8)

                INVITES[invite_id] = {
                    "team_id": team_id,
                    "inviter_id": str(select_interaction.user.id),
                    "user_id": str(selected_member.id),
                    "status": "pending"
                }

                save_all()

                class InviteResponseView(discord.ui.View):
                    def __init__(self):
                        super().__init__(timeout=None)

                    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
                    async def accept_invite(
                        self,
                        dm_interaction: discord.Interaction,
                        button: discord.ui.Button
                    ):
                        global ROSTER_LOCK_ALL

                        if ROSTER_LOCK_ALL:
                            return await dm_interaction.response.send_message(
                                "Rosters are currently locked by staff.",
                                ephemeral=True
                            )

                        invite = INVITES.get(invite_id)

                        if not invite or invite.get("status") != "pending":
                            return await dm_interaction.response.send_message(
                                "This invite is no longer valid.",
                                ephemeral=True
                            )

                        if is_user_on_any_team(selected_member.id):
                            invite["status"] = "expired"
                            save_all()

                            return await dm_interaction.response.send_message(
                                "You are already on a team.",
                                ephemeral=True
                            )

                        if len(team.get("players", [])) >= roster_limit:
                            invite["status"] = "expired"
                            save_all()

                            return await dm_interaction.response.send_message(
                                "That team's roster is now full.",
                                ephemeral=True
                            )

                        team.setdefault("players", [])

                        if str(selected_member.id) not in [str(x) for x in team["players"]]:
                            team["players"].append(str(selected_member.id))

                        invite["status"] = "accepted"

                        try:
                            await add_member_to_team_roles(
                                selected_member,
                                team,
                                reason="Accepted team invite"
                            )
                        except Exception:
                            pass

                        save_all()

                        log_transaction(
                            guild,
                            f"<@{selected_member.id}> Has Joined **{team.get('name', 'Team')}**"
                        )

                        await dm_interaction.response.edit_message(
                            content=f"You accepted the invite to **{team.get('name', 'Team')}**.",
                            view=None
                        )

                    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
                    async def decline_invite(
                        self,
                        dm_interaction: discord.Interaction,
                        button: discord.ui.Button
                    ):
                        invite = INVITES.get(invite_id)

                        if invite:
                            invite["status"] = "declined"
                            save_all()

                        await dm_interaction.response.edit_message(
                            content=f"You declined the invite to **{team.get('name', 'Team')}**.",
                            view=None
                        )

                try:
                    await selected_member.send(
                        f"You have been invited to join **{team.get('name', 'Team')}**.\n"
                        f"Invited by: {select_interaction.user.mention}\n\n"
                        f"Use the buttons below to accept or decline.",
                        view=InviteResponseView()
                    )

                    await select_interaction.response.send_message(
                        f"Invite sent to {selected_member.mention}.",
                        ephemeral=True
                    )

                except Exception:
                    await select_interaction.response.send_message(
                        "Could not DM that member. They may have DMs closed.",
                        ephemeral=True
                    )

            select.callback = select_callback
            invite_view.add_item(select)

            await button_interaction.response.send_message(
                "Choose a player to invite:",
                view=invite_view,
                ephemeral=True
            )

    class CoCaptainKickButton(discord.ui.Button):
        def __init__(self):
            super().__init__(
                label="Kick Player",
                style=discord.ButtonStyle.danger
            )

        async def callback(self, button_interaction: discord.Interaction):
            global ROSTER_LOCK_ALL

            if ROSTER_LOCK_ALL:
                return await button_interaction.response.send_message(
                    "Rosters are currently locked by staff.",
                    ephemeral=True
                )

            if button_interaction.user.id != interaction.user.id:
                return await button_interaction.response.send_message(
                    "This is not your panel.",
                    ephemeral=True
                )

            players = team.get("players", [])

            if not players:
                return await button_interaction.response.send_message(
                    "There are no players to kick.",
                    ephemeral=True
                )

            options = []

            for player_id in players:
                member = guild.get_member(int(player_id))

                if member:
                    options.append(
                        discord.SelectOption(
                            label=member.display_name[:100],
                            value=str(member.id)
                        )
                    )

            if not options:
                return await button_interaction.response.send_message(
                    "No valid players found to kick.",
                    ephemeral=True
                )

            select = discord.ui.Select(
                placeholder="Select a player to kick...",
                options=options,
                min_values=1,
                max_values=1
            )

            kick_view = discord.ui.View(timeout=60)

            async def select_callback(select_interaction: discord.Interaction):
                selected_user_id = select_interaction.data["values"][0]
                selected_member = guild.get_member(int(selected_user_id))

                team["players"] = [
                    p for p in team.get("players", [])
                    if str(p) != str(selected_user_id)
                ]

                try:
                    if selected_member:
                        await remove_member_from_team_roles(
                            selected_member,
                            team,
                            reason="Kicked by co-captain"
                        )
                except Exception:
                    pass

                save_all()

                log_transaction(
                    guild,
                    f"<@{selected_user_id}> Has Been kicked from **{team.get('name', 'Team')}**"
                )

                await select_interaction.response.send_message(
                    f"<@{selected_user_id}> has been kicked from **{team.get('name', 'Team')}**.",
                    ephemeral=True
                )

            select.callback = select_callback
            kick_view.add_item(select)

            await button_interaction.response.send_message(
                "Choose a player to kick:",
                view=kick_view,
                ephemeral=True
            )

    view = discord.ui.View(timeout=180)
    view.add_item(CoCaptainInviteButton())
    view.add_item(CoCaptainKickButton())

    await interaction.response.send_message(
        build_panel_text(),
        view=view,
        ephemeral=True
    )


@tree.command(name="create-team", description="Create a new team and assign its captain")
@app_commands.describe(
    captain="The captain of the new team",
    team_name="The team name",
    hex_color="Team role color, example: #ff0000"
)
async def create_team(
    interaction: discord.Interaction,
    captain: discord.Member,
    team_name: str,
    hex_color: str
):
    if not is_admin_or_above(interaction.user):
        return await interaction.response.send_message(
            "Admin permissions required.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild

    existing_role = discord.utils.get(guild.roles, name=team_name)
    if existing_role:
        return await interaction.followup.send(
            "A role with that team name already exists.",
            ephemeral=True
        )

    try:
        color = parse_hex_color(hex_color)
    except Exception:
        return await interaction.followup.send(
            "Invalid hex color. Use something like `#ff0000`.",
            ephemeral=True
        )

    try:
        team_role = await guild.create_role(
            name=team_name,
            color=color,
            reason=f"Team created by {interaction.user}"
        )
    except Exception as e:
        return await interaction.followup.send(
            f"Failed to create team role: `{e}`",
            ephemeral=True
        )

    captain_role = get_role_by_cfg(guild, "captain")
    team_player_role = await get_or_create_team_player_role(guild)

    roles_to_add = [team_role]

    if captain_role:
        roles_to_add.append(captain_role)

    if team_player_role:
        roles_to_add.append(team_player_role)

    try:
        await captain.add_roles(
            *roles_to_add,
            reason="Assigned as team captain"
        )
    except Exception:
        pass

    team_id = clean_team_id(team_name)

    # Avoid overwriting if duplicate clean IDs happen
    original_team_id = team_id
    counter = 1
    while team_id in TEAMS:
        counter += 1
        team_id = f"{original_team_id}_{counter}"

    TEAMS[team_id] = {
        "name": team_name,
        "role_id": str(team_role.id),
        "captain_id": str(captain.id),
        "co_captains": [],
        "players": []
    }

    save_all()

    tx = f"{captain.mention} Has Become Captain Of **{team_name}**"
    log_transaction(guild, tx)

    await interaction.followup.send(
        f"Created **{team_name}** and assigned {captain.mention} as captain.",
        ephemeral=True
    )


@tree.command(name="disband", description="Disband one team")
@app_commands.describe(team_role="The team role to disband")
async def disband(interaction: discord.Interaction, team_role: discord.Role):
    if not is_admin_or_above(interaction.user):
        return await interaction.response.send_message(
            "Admin permissions required.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    team_id, team = find_team_by_role_id(team_role.id)

    if not team:
        return await interaction.followup.send(
            "That role is not registered as a team role.",
            ephemeral=True
        )

    team_name = team.get("name", team_role.name)

    members_to_clean = []

    for member in guild.members:
        if team_role in member.roles:
            members_to_clean.append(member)

    captain_id = team.get("captain_id")
    if captain_id:
        captain_member = guild.get_member(int(captain_id))
        if captain_member and captain_member not in members_to_clean:
            members_to_clean.append(captain_member)

    for cc_id in team.get("co_captains", []):
        member = guild.get_member(int(cc_id))
        if member and member not in members_to_clean:
            members_to_clean.append(member)

    for player_id in team.get("players", []):
        member = guild.get_member(int(player_id))
        if member and member not in members_to_clean:
            members_to_clean.append(member)

    for member in members_to_clean:
        await remove_team_member_roles(member, team_role)

    try:
        await team_role.delete(reason=f"Team disbanded by {interaction.user}")
    except Exception:
        pass

    TEAMS.pop(team_id, None)
    save_all()

    tx = f"**{team_name}** Has Been Disbanded"
    log_transaction(guild, tx)

    await interaction.followup.send(
        f"Disbanded **{team_name}**.",
        ephemeral=True
    )


@tree.command(name="disband-all", description="Disband all teams")
async def disband_all(interaction: discord.Interaction):
    if not is_admin_or_above(interaction.user):
        return await interaction.response.send_message(
            "Admin permissions required.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild

    if not TEAMS:
        return await interaction.followup.send(
            "There are no teams to disband.",
            ephemeral=True
        )

    teams_copy = list(TEAMS.items())

    for team_id, team in teams_copy:
        role = None

        if team.get("role_id"):
            role = guild.get_role(int(team["role_id"]))

        members_to_clean = []

        if role:
            for member in guild.members:
                if role in member.roles:
                    members_to_clean.append(member)

        captain_id = team.get("captain_id")
        if captain_id:
            member = guild.get_member(int(captain_id))
            if member and member not in members_to_clean:
                members_to_clean.append(member)

        for cc_id in team.get("co_captains", []):
            member = guild.get_member(int(cc_id))
            if member and member not in members_to_clean:
                members_to_clean.append(member)

        for player_id in team.get("players", []):
            member = guild.get_member(int(player_id))
            if member and member not in members_to_clean:
                members_to_clean.append(member)

        for member in members_to_clean:
            await remove_team_member_roles(member, role)

        if role:
            try:
                await role.delete(reason=f"All teams disbanded by {interaction.user}")
            except Exception:
                pass

    TEAMS.clear()
    save_all()

    tx = "**All Teams Have Been Disbanded**"
    log_transaction(guild, tx)

    await interaction.followup.send(
        "All teams have been disbanded.",
        ephemeral=True
    )


@tree.command(name="roster-lock-all", description="Lock all rosters")
async def roster_lock_all(interaction: discord.Interaction):
    global ROSTER_LOCK_ALL

    if not is_admin(interaction.user):
        return await interaction.response.send_message("Admin perms required.", ephemeral=True)

    ROSTER_LOCK_ALL = True

    log_transaction(interaction.guild, "**All Rosters Have Been Locked**")

    await interaction.response.send_message("All rosters are now locked.", ephemeral=True)


@tree.command(name="unroster-lock-all", description="Unlock all rosters")
async def unroster_lock_all(interaction: discord.Interaction):
    global ROSTER_LOCK_ALL

    if not is_admin(interaction.user):
        return await interaction.response.send_message("Admin perms required.", ephemeral=True)

    ROSTER_LOCK_ALL = False

    log_transaction(interaction.guild, "**All Rosters Have Been Unlocked**")

    await interaction.response.send_message("All rosters are now unlocked.", ephemeral=True)


@tree.command(name="admin-add", description="Admin add a player to a team")
@app_commands.describe(
    team_role="The team role",
    user="The user to add"
)
async def admin_add(
    interaction: discord.Interaction,
    team_role: discord.Role,
    user: discord.Member
):
    if not is_admin_or_above(interaction.user):
        return await interaction.response.send_message(
            "Admin permissions required.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    team_id, team = find_team_by_role_id(team_role.id)

    if not team:
        return await interaction.followup.send(
            "That role is not registered as a team role.",
            ephemeral=True
        )

    team_player_role = await get_or_create_team_player_role(interaction.guild)

    roles_to_add = [team_role]

    if team_player_role:
        roles_to_add.append(team_player_role)

    try:
        await user.add_roles(
            *roles_to_add,
            reason=f"Admin added to team by {interaction.user}"
        )
    except Exception as e:
        return await interaction.followup.send(
            f"Failed to add roles: `{e}`",
            ephemeral=True
        )

    if str(user.id) != str(team.get("captain_id")):
        if str(user.id) not in [str(x) for x in team.get("players", [])]:
            team.setdefault("players", []).append(str(user.id))

    save_all()

    tx = f"{user.mention} Has Joined **{team.get('name', team_role.name)}**"
    log_transaction(interaction.guild, tx)

    await interaction.followup.send(
        f"Added {user.mention} to **{team.get('name', team_role.name)}**.",
        ephemeral=True
    )


@tree.command(name="admin-kick", description="Admin remove a player from a team")
@app_commands.describe(
    team_role="The team role",
    user="The user to remove"
)
async def admin_kick(
    interaction: discord.Interaction,
    team_role: discord.Role,
    user: discord.Member
):
    if not is_admin_or_above(interaction.user):
        return await interaction.response.send_message(
            "Admin permissions required.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    team_id, team = find_team_by_role_id(team_role.id)

    if not team:
        return await interaction.followup.send(
            "That role is not registered as a team role.",
            ephemeral=True
        )

    team_player_role = get_team_player_role(interaction.guild)

    roles_to_remove = []

    if team_role in user.roles:
        roles_to_remove.append(team_role)

    if team_player_role and team_player_role in user.roles:
        roles_to_remove.append(team_player_role)

    if roles_to_remove:
        try:
            await user.remove_roles(
                *roles_to_remove,
                reason=f"Admin kicked from team by {interaction.user}"
            )
        except Exception as e:
            return await interaction.followup.send(
                f"Failed to remove roles: `{e}`",
                ephemeral=True
            )

    team["players"] = [
        p for p in team.get("players", [])
        if str(p) != str(user.id)
    ]

    team["co_captains"] = [
        c for c in team.get("co_captains", [])
        if str(c) != str(user.id)
    ]

    save_all()

    tx = f"{user.mention} Has Been Kicked From **{team.get('name', team_role.name)}**"
    log_transaction(interaction.guild, tx)

    await interaction.followup.send(
        f"Removed {user.mention} from **{team.get('name', team_role.name)}**.",
        ephemeral=True
    )



# submit-time
@tree.command(name="submit-time", description="Submit a match time")
@app_commands.describe(week="Week label", time="Time string (e.g., 7/21 at 5PM EST)", team1="Team 1 id or name", team2="Team 2 id or name")
async def submit_time(interaction: discord.Interaction, week: str, time: str, team1: str, team2: str):
    await interaction.response.defer()
    # find teams
    def find_team(x):
        if x in TEAMS: return TEAMS[x]
        for k,v in TEAMS.items():
            if v.get("name","").lower() == x.lower(): return v
        return None
    t1 = find_team(team1); t2 = find_team(team2)
    if not t1 or not t2:
        return await interaction.followup.send("Team(s) not found.", ephemeral=True)
    # parse time into timezone-aware datetime
    tz = ZoneInfo(CONFIG.get("timezone", "America/New_York"))
    try:
        dt = date_parser.parse(time, fuzzy=True)
        if dt.tzinfo is None:
            # assume configured timezone
            dt = dt.replace(tzinfo=tz)
        else:
            dt = dt.astimezone(tz)
    except Exception as e:
        return await interaction.followup.send("Couldn't parse time. Use format like '7/21 at 5PM EST' or ISO.", ephemeral=True)
    ts = int(dt.timestamp())
    match_id = secrets.token_hex(8)
    MATCHES[match_id] = {
        "week": week,
        "time_str": time,
        "timestamp": ts,
        "team1_id": next((k for k,v in TEAMS.items() if v==t1), None),
        "team2_id": next((k for k,v in TEAMS.items() if v==t2), None),
        "accepted": { "team1": False, "team2": False },
        "assignments_post_id": None,
        "claims": {"caster": None, "referee": None},
        "notified_5min": False
    }
    save_all()

    # prepare message content and view with accept buttons
    content = f"<@&{t1.get('role_id')}> vs <@&{t2.get('role_id')}>\nTeam staff must accept this match.\n> WEEK: {week}\n> Time: <t:{ts}:F>\n> Team 1: {t1.get('name')}\n> Team 2: {t2.get('name')}"
    class AcceptButton(discord.ui.Button):
        def __init__(self, which):
            label = f"Accept for {which}"
            super().__init__(style=discord.ButtonStyle.success, label=label)
            self.which = which
        async def callback(self, btn_inter):
            user = btn_inter.user
            # check role: only captains/co-captains
            if not (is_captain(user) or is_co_captain(user)):
                return await btn_inter.response.send_message("Only caps/co-caps can accept.", ephemeral=True)
            mid = match_id
            m = MATCHES.get(mid)
            if not m: return await btn_inter.response.send_message("Match not found.", ephemeral=True)
            # check which team the user belongs to
            team_key = None
            team1_role = t1.get("role_id")
            team2_role = t2.get("role_id")
            # determine user's team based on roles
            if team1_role and any(r.id == int(team1_role) for r in user.roles):
                team_key = "team1"
            elif team2_role and any(r.id == int(team2_role) for r in user.roles):
                team_key = "team2"
            else:
                # fallback: check membership lists
                if str(user.id) in [str(x) for x in t1.get("players",[])] + [str(t1.get('captain_id'))] + [str(x) for x in t1.get("co_captains",[])]:
                    team_key = "team1"
                elif str(user.id) in [str(x) for x in t2.get("players",[])] + [str(t2.get('captain_id'))] + [str(x) for x in t2.get("co_captains",[])]:
                    team_key = "team2"
            if not team_key:
                return await btn_inter.response.send_message("You are not part of the teams involved.", ephemeral=True)
            # set accepted
            if team_key == "team1":
                m["accepted"]["team1"] = True
            else:
                m["accepted"]["team2"] = True
            save_all()
            # respond ephemeral and update original message check (optional)
            await btn_inter.response.send_message(f"Accepted ✅ by {user.mention}", ephemeral=True)
            # if both accepted, post assignments
            if m["accepted"]["team1"] and m["accepted"]["team2"]:
                await post_assignments(interaction.guild, mid)
    view = discord.ui.View(timeout=None)
    view.add_item(AcceptButton("Team 1"))
    view.add_item(AcceptButton("Team 2"))
    # send to a channel: try current channel
    try:
        msg = await interaction.followup.send(content, view=view)
        MATCHES[match_id]["post_channel"] = str(interaction.channel_id)
        MATCHES[match_id]["post_message_id"] = str(msg.id)
        save_all()
    except Exception:
        await interaction.followup.send(content, view=view, ephemeral=True)

    await interaction.followup.send("Match submitted. Teams must accept.", ephemeral=True)

async def post_assignments(guild: discord.Guild, match_id: str):
    m = MATCHES.get(match_id)
    if not m:
        return
    t1 = TEAMS.get(m["team1_id"])
    t2 = TEAMS.get(m["team2_id"])

    # mention staff roles: Head Ref, Head Caster, Referee, Caster
    head_role = get_role_by_cfg(guild, "head")               # Head Ref / Head Staff
    head_caster_role = get_role_by_cfg(guild, "head_caster") # Head Caster
    caster_role = get_role_by_cfg(guild, "caster")
    ref_role = get_role_by_cfg(guild, "referee")

    head_mention = f"<@&{head_role.id}>" if head_role else "@Head Referee"
    head_caster_mention = f"<@&{head_caster_role.id}>" if head_caster_role else "@Head Caster"
    caster_mention = f"<@&{caster_role.id}>" if caster_role else "@Caster"
    ref_mention = f"<@&{ref_role.id}>" if ref_role else "@Referee"

    content = (
        f"{head_mention} {ref_mention} {head_caster_mention} {caster_mention}\n"
        f"{t1.get('name')} vs {t2.get('name')}\n"
        f"> WEEK: {m.get('week')}\n"
        f"> Time: <t:{m.get('timestamp')}:F>\n"
        f"> Referee: {('<@'+m['claims']['referee']+'>') if m['claims'].get('referee') else 'Unassigned'}\n"
        f"> Caster: {('<@'+m['claims']['caster']+'>') if m['claims'].get('caster') else 'Unassigned'}"
    )

    # buttons: Claim Caster, Claim Referee, Unclaim
    class ClaimButton(discord.ui.Button):
        def __init__(self, role):
            super().__init__(style=discord.ButtonStyle.primary, label=f"Claim {role.capitalize()}")
            self.role = role

        async def callback(self, btn_int):
            user = btn_int.user
            mid = match_id
            mm = MATCHES.get(mid)
            if not mm:
                return await btn_int.response.send_message("Match not found.", ephemeral=True)
            # set claim if not already or if same user
            if mm["claims"].get(self.role) and mm["claims"].get(self.role) != str(user.id):
                return await btn_int.response.send_message(f"{self.role.capitalize()} already claimed.", ephemeral=True)
            mm["claims"][self.role] = str(user.id)
            save_all()
            await btn_int.response.send_message(f"You claimed {self.role}.", ephemeral=True)
            # update assignments message if exists
            await update_match_posts(guild, mid)

    class UnclaimButton(discord.ui.Button):
        def __init__(self):
            super().__init__(style=discord.ButtonStyle.secondary, label="Unclaim")

        async def callback(self, btn_int):
            user = btn_int.user
            mid = match_id
            mm = MATCHES.get(mid)
            if not mm:
                return await btn_int.response.send_message("Match not found.", ephemeral=True)
            changed = False
            for role in ["caster", "referee"]:
                if mm["claims"].get(role) == str(user.id):
                    mm["claims"][role] = None
                    changed = True
            if changed:
                save_all()
                await btn_int.response.send_message("Your claim(s) removed.", ephemeral=True)
                await update_match_posts(guild, mid)
            else:
                await btn_int.response.send_message("You have no claims on this match.", ephemeral=True)

    view = discord.ui.View(timeout=None)
    view.add_item(ClaimButton("caster"))
    view.add_item(ClaimButton("referee"))
    view.add_item(UnclaimButton())

    # post to a designated assignments channel or fallback to original channel
    channel = None
    if m.get("post_channel"):
        try:
            channel = guild.get_channel(int(m.get("post_channel")))
        except:
            channel = None
    if channel is None:
        # try transactions channel
        ch_id = CONFIG.get("transactions_channel")
        if ch_id:
            channel = guild.get_channel(int(ch_id))
    if channel is None:
        # fallback to first text channel
        channel = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
    if not channel:
        return

    msg = await channel.send(content, view=view)
    m["assignments_post_id"] = str(msg.id)
    save_all()

async def update_match_posts(guild: discord.Guild, match_id: str):
    m = MATCHES.get(match_id)
    if not m:
        return

    ch = None
    if m.get("post_channel"):
        ch = guild.get_channel(int(m.get("post_channel")))
    if not ch:
        ch_id = CONFIG.get("transactions_channel")
        if ch_id:
            ch = guild.get_channel(int(ch_id))
    if not ch:
        return

    try:
        msg = await ch.fetch_message(int(m.get("assignments_post_id")))
    except Exception:
        return

    t1 = TEAMS.get(m["team1_id"])
    t2 = TEAMS.get(m["team2_id"])

    head_role = get_role_by_cfg(guild, "head")
    head_caster_role = get_role_by_cfg(guild, "head_caster")
    caster_role = get_role_by_cfg(guild, "caster")
    ref_role = get_role_by_cfg(guild, "referee")

    head_mention = f"<@&{head_role.id}>" if head_role else "@Head Referee"
    head_caster_mention = f"<@&{head_caster_role.id}>" if head_caster_role else "@Head Caster"
    caster_mention = f"<@&{caster_role.id}>" if caster_role else "@Caster"
    ref_mention = f"<@&{ref_role.id}>" if ref_role else "@Referee"

    content = (
        f"{head_mention} {ref_mention} {head_caster_mention} {caster_mention}\n"
        f"{t1.get('name')} vs {t2.get('name')}\n"
        f"> WEEK: {m.get('week')}\n"
        f"> Time: <t:{m.get('timestamp')}:F>\n"
        f"> Referee: {('<@'+m['claims']['referee']+'>') if m['claims'].get('referee') else 'Unassigned'}\n"
        f"> Caster: {('<@'+m['claims']['caster']+'>') if m['claims'].get('caster') else 'Unassigned'}"
    )

    try:
        await msg.edit(content=content)
    except Exception:
        pass

# scheduled task: check matches for 5-minute notifications and send codes
@tasks.loop(seconds=30)
async def match_notifier():
    await bot.wait_until_ready()
    guild = None
    try:
        gid = int(CONFIG.get("guild_id")) if CONFIG.get("guild_id") else None
        guild = bot.get_guild(gid) if gid else next(iter(bot.guilds), None)
    except:
        guild = next(iter(bot.guilds), None)
    if not guild:
        return

    now_ts = int(datetime.now(tz=ZoneInfo(CONFIG.get("timezone", "America/New_York"))).timestamp())
    for mid, m in list(MATCHES.items()):
        if m.get("notified_5min"):
            continue
        ts = int(m.get("timestamp"))
        # 5 min before, with a small grace window
        if ts - now_ts <= 300 and ts - now_ts > -60:
            # generate code and mark notified
            code = secrets.token_urlsafe(6).upper()
            m["code"] = code
            m["notified_5min"] = True
            save_all()

            # DM to caster and ref claims if present
            for role in ["caster", "referee"]:
                uid = m.get("claims", {}).get(role)
                if uid:
                    member = guild.get_member(int(uid))
                    if member:
                        try:
                            await member.send(
                                "# The Code Is:\n\n"
                                "# THE CODE\n\n"
                                "***DO NOT SHARE THIS TO ANYONE. IF YOU DO, YOU WILL BE DEMOTED.***\n\n"
                                f"Code: ||{code}||"
                            )
                        except:
                            pass

            # Post masked code to the match channel for teams + staff
            t1 = TEAMS.get(m.get("team1_id"))
            t2 = TEAMS.get(m.get("team2_id"))

            ch = None
            if m.get("post_channel"):
                ch = guild.get_channel(int(m.get("post_channel")))
            if not ch:
                ch_id = CONFIG.get("transactions_channel")
                if ch_id:
                    ch = guild.get_channel(int(ch_id))
            if ch:
                try:
                    head_role = get_role_by_cfg(guild, "head")
                    head_caster_role = get_role_by_cfg(guild, "head_caster")
                    caster_role = get_role_by_cfg(guild, "caster")

                    head_mention = f"<@&{head_role.id}>" if head_role else ""
                    head_caster_mention = f"<@&{head_caster_role.id}>" if head_caster_role else ""
                    caster_mention = f"<@&{caster_role.id}>" if caster_role else ""

                    await ch.send(
                        f"{head_mention} {head_caster_mention} {caster_mention} "
                        f"<@&{t1.get('role_id')}> and <@&{t2.get('role_id')}> \n"
                        f"# CODE IS ||{code}||"
                    )
                except:
                    pass

# addscrim
@tree.command(name="addscrim", description="Add a scrim (creates channel and schedule flow)")
@app_commands.describe(team1="Team 1 id or name", team2="Team 2 id or name")
async def addscrim(interaction: discord.Interaction, team1: str, team2: str):
    if not is_admin(interaction.user) and not is_captain(interaction.user) and not is_co_captain(interaction.user):
        return await interaction.response.send_message("You don't have permission to create scrims.", ephemeral=True)
    # find teams
    def find_team(x):
        if x in TEAMS: return (x,TEAMS[x])
        for k,v in TEAMS.items():
            if v.get("name","").lower() == x.lower(): return (k,v)
        return (None,None)
    id1,t1 = find_team(team1); id2,t2 = find_team(team2)
    if not t1 or not t2:
        return await interaction.response.send_message("Team(s) not found.", ephemeral=True)
    # create channel
    guild = interaction.guild
    chan_name = f"{t1.get('name')}-vs-{t2.get('name')}".lower().replace(" ", "-")[:90]
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
    }
    # allow team roles and staff to view
    if t1.get("role_id"):
        role = guild.get_role(int(t1["role_id"]))
        if role: overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    if t2.get("role_id"):
        role = guild.get_role(int(t2["role_id"]))
        if role: overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    for staff_role_key in ["board","head","admin"]:
        role = get_role_by_cfg(guild, staff_role_key)
        if role: overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    try:
        ch = await guild.create_text_channel(chan_name, overwrites=overwrites, reason="Scrim created via bot")
    except Exception as e:
        return await interaction.response.send_message("Failed to create channel.", ephemeral=True)
    # send welcome message
    await ch.send(f"<@&{t1.get('role_id')}> vs <@&{t2.get('role_id')}>\n\n# Welcome to EGC Bracket\n> 🗓️ You guys will have 3 day to schedule\n> ⚔️ And 4 days to play\n> Ping a staff member when you're ready to schedule or have any questions!")
    # schedule a check in 3 days to propose a time
    schedule_id = secrets.token_hex(8)
    MATCHES[schedule_id] = {
        "type": "scrim",
        "channel_id": str(ch.id),
        "team1_id": id1,
        "team2_id": id2,
        "created_at": int(datetime.now(tz=ZoneInfo(CONFIG.get("timezone","America/New_York"))).timestamp()),
        "proposed_time": None,
        "staff_accepted": False
    }
    save_all()
    await interaction.response.send_message(f"Scrim channel created: {ch.mention}", ephemeral=True)

# Auto Warn simple endpoint: add warning and log
def auto_warn(guild, user_id, reason):
    WARNINGS.setdefault(str(user_id), []).append({"time": datetime.now(timezone.utc).isoformat(), "reason": reason})
    save_json("warnings", WARNINGS)
    save_all()
    log_transaction(guild, f"<@{user_id}> was auto-warned: {reason}")

# on_ready and run tasks
@bot.event
async def on_ready():
    try:
        gid = int(CONFIG.get("guild_id")) if CONFIG.get("guild_id") else None
        if gid:
            guild_obj = discord.Object(id=gid)
            await tree.sync(guild=guild_obj)
            print(f"Synced commands to guild {gid}")
        else:
            await tree.sync()
            print("Synced commands globally")
    except Exception as e:
        print(f"Command sync failed: {e}")

    print(f"Bot ready. Logged in as {bot.user}")

    if not match_notifier.is_running():
        match_notifier.start()


# graceful shutdown save
@bot.event
async def on_disconnect():
    save_all()

# run bot
if __name__ == "__main__":
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        print("Set BOT_TOKEN env var.")
    else:
        try:
            bot.run(TOKEN)
        finally:
            save_all()
