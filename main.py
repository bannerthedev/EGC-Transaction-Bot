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

# ---- Config loading (env or config.json) ----
def load_config():
    cfg = {}
    if os.path.exists("config.json"):
        with open("config.json", "r") as f:
            cfg = json.load(f)
    # Override with env vars if provided
    env_map = {
        "GUILD_ID": "guild_id",
        "ROLE_CAPTAIN_ID": ("roles", "captain"),
        "ROLE_CO_CAPTAIN_ID": ("roles", "co_captain"),
        "ROLE_ADMIN_ID": ("roles", "admin"),
        "ROLE_HEAD_ID": ("roles", "head"),
        "ROLE_REF_ID": ("roles", "referee"),
        "ROLE_CASTER_ID": ("roles", "caster"),
        "ROLE_BOARD_ID": ("roles", "board"),
        "CHANNEL_TRANSACTIONS_ID": "transactions_channel",
        "DEFAULT_ROSTER_LIMIT": "roster_limit",
        "DEFAULT_TIMEZONE": "timezone"
    }
    for env, key in env_map.items():
        v = os.getenv(env)
        if not v:
            continue
        if isinstance(key, tuple):
            cfg.setdefault(key[0], {})
            cfg[key[0]][key[1]] = v
        else:
            cfg[key] = v
    # normalize types
    if "roster_limit" in cfg:
        cfg["roster_limit"] = int(cfg["roster_limit"])
    cfg.setdefault("roster_limit", 10)
    cfg.setdefault("timezone", "America/New_York")
    return cfg

CONFIG = load_config()

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
@tree.command(name="captain-panel", description="Captain panel (captains only)")
async def captain_panel(interaction: discord.Interaction):
    if not is_captain(interaction.user):
        return await interaction.response.send_message("Captain role required.", ephemeral=True)
    # Build roster display + buttons
    # We'll assume team lookup by captain role presence or a configured team for this captain.
    team = None
    for tid,t in TEAMS.items():
        if str(t.get("captain_id")) == str(interaction.user.id):
            team = (tid,t); break
    if not team:
        return await interaction.response.send_message("No team found for you. Contact admin.", ephemeral=True)
    tid, t = team
    # create view
    view = discord.ui.View(timeout=None)
    # Invite button
    class InviteButton(discord.ui.Button):
        def __init__(self): super().__init__(style=discord.ButtonStyle.primary, label="Invite")
        async def callback(self, button_inter):
            # present select of guild members
            options = []
            for m in interaction.guild.members:
                # skip bots
                if m.bot: continue
                options.append(discord.SelectOption(label=m.display_name, value=str(m.id)))
                if len(options) >= 25: break
            if not options:
                return await button_inter.response.send_message("No members to invite.", ephemeral=True)
            select = discord.ui.Select(placeholder="Invite member...", options=options, min_values=1, max_values=1)
            sel_view = discord.ui.View(timeout=60)
            async def select_cb(sel_inter):
                user_id = sel_inter.data["values"][0]
                inviter = button_inter.user
                invite_id = secrets.token_hex(8)
                INVITES[invite_id] = {"team_id": tid, "inviter_id": str(inviter.id), "user_id": str(user_id), "status":"pending"}
                save_all()
                # DM the user
                member = interaction.guild.get_member(int(user_id))
                if member:
                    try:
                        dm_v = discord.ui.View()
                        async def accept_cb(i):
                            # add to team
                            t.setdefault("players",[]).append(str(user_id))
                            INVITES[invite_id]["status"] = "accepted"
                            save_all()
                            log_transaction(interaction.guild, f"<@{user_id}> Has Joined **{t.get('name')}**")
                            await i.response.edit_message(content="You accepted invite.", view=None)
                        async def decline_cb(i):
                            INVITES[invite_id]["status"] = "declined"
                            save_all()
                            await i.response.edit_message(content="You declined invite.", view=None)
                        b_accept = discord.ui.Button(label="Accept", style=discord.ButtonStyle.success)
                        b_accept.callback = accept_cb
                        b_decline = discord.ui.Button(label="Decline", style=discord.ButtonStyle.danger)
                        b_decline.callback = decline_cb
                        dm_v.add_item(b_accept)
                        dm_v.add_item(b_decline)
                        await member.send(f"You've been invited to **{t.get('name')}**\n{interaction.user.mention} invited you to join **{t.get('name')}**. Use the buttons below to respond:", view=dm_v)
                    except Exception:
                        pass
                await sel_inter.response.send_message("Invite sent (if DM allowed).", ephemeral=True)
            select.callback = select_cb
            sel_view.add_item(select)
            await button_inter.response.send_message("Choose a member to invite:", view=sel_view, ephemeral=True)

    class KickButton(discord.ui.Button):
        def __init__(self): super().__init__(style=discord.ButtonStyle.danger, label="Kick")
        async def callback(self, button_inter):
            # Show roster select (players + co-caps)
            opts = []
            for pid in t.get("players",[]):
                member = interaction.guild.get_member(int(pid))
                if member:
                    opts.append(discord.SelectOption(label=member.display_name, value=str(pid)))
            for cc in t.get("co_captains",[]):
                member = interaction.guild.get_member(int(cc))
                if member:
                    opts.append(discord.SelectOption(label=member.display_name+" (co-captain)", value=str(cc)))
            if not opts:
                return await button_inter.response.send_message("No players to kick.", ephemeral=True)
            sel = discord.ui.Select(placeholder="Select to kick", options=opts)
            v = discord.ui.View(timeout=60)
            async def sel_cb(si):
                uid = si.data["values"][0]
                t["players"] = [p for p in t.get("players",[]) if str(p) != str(uid)]
                t["co_captains"] = [c for c in t.get("co_captains",[]) if str(c) != str(uid)]
                save_all()
                log_transaction(button_inter.guild, f"<@{uid}> Has Been kicked from **{t.get('name')}**")
                await si.response.send_message("User kicked.", ephemeral=True)
            sel.callback = sel_cb
            v.add_item(sel)
            await button_inter.response.send_message("Choose player to kick:", view=v, ephemeral=True)

    class AddCoButton(discord.ui.Button):
        def __init__(self): super().__init__(style=discord.ButtonStyle.secondary, label="Add Co-Captain")
        async def callback(self, button_inter):
            opts = []
            for pid in t.get("players",[]):
                member = interaction.guild.get_member(int(pid))
                if member:
                    opts.append(discord.SelectOption(label=member.display_name, value=str(pid)))
            if not opts:
                return await button_inter.response.send_message("No players available.", ephemeral=True)
            sel = discord.ui.Select(placeholder="Promote to Co-Captain", options=opts)
            v = discord.ui.View()
            async def cb(si):
                uid = si.data["values"][0]
                if uid not in [str(x) for x in t.get("co_captains",[])]:
                    t.setdefault("co_captains",[]).append(str(uid))
                    save_all()
                    log_transaction(button_inter.guild, f"<@{uid}> Has Been promoted to Co-Captain")
                await si.response.send_message("Promoted.", ephemeral=True)
            sel.callback = cb
            v.add_item(sel)
            await button_inter.response.send_message("Choose player to promote:", view=v, ephemeral=True)

    class RemoveCoButton(discord.ui.Button):
        def __init__(self): super().__init__(style=discord.ButtonStyle.secondary, label="Remove Co-Captain")
        async def callback(self, button_inter):
            opts = []
            for cc in t.get("co_captains",[]):
                member = interaction.guild.get_member(int(cc))
                if member:
                    opts.append(discord.SelectOption(label=member.display_name, value=str(cc)))
            if not opts:
                return await button_inter.response.send_message("No co-captains.", ephemeral=True)
            sel = discord.ui.Select(placeholder="Remove Co-Captain", options=opts)
            v = discord.ui.View()
            async def cb(si):
                uid = si.data["values"][0]
                t["co_captains"] = [c for c in t.get("co_captains",[]) if str(c) != str(uid)]
                save_all()
                log_transaction(button_inter.guild, f"<@{uid}> Has Been demoted from Co-Captain")
                await si.response.send_message("Removed.", ephemeral=True)
            sel.callback = cb
            v.add_item(sel)
            await button_inter.response.send_message("Choose co-captain to remove:", view=v, ephemeral=True)

    class TransferButton(discord.ui.Button):
        def __init__(self): super().__init__(style=discord.ButtonStyle.secondary, label="Transfer Captain")
        async def callback(self, button_inter):
            opts = []
            for pid in t.get("players",[])+[t.get("captain_id")] + t.get("co_captains",[]):
                m = interaction.guild.get_member(int(pid))
                if m:
                    opts.append(discord.SelectOption(label=m.display_name, value=str(pid)))
            sel = discord.ui.Select(placeholder="Transfer to...", options=opts)
            v = discord.ui.View()
            async def cb(si):
                new_id = si.data["values"][0]
                old = t.get("captain_id")
                t["captain_id"] = str(new_id)
                # optionally demote old to player
                if old and str(old) not in t.get("players",[]):
                    t.setdefault("players",[]).append(str(old))
                save_all()
                log_transaction(button_inter.guild, f"<@{old}> Has Transferred Captain to <@{new_id}>")
                await si.response.send_message("Captain transferred.", ephemeral=True)
            sel.callback = cb
            v.add_item(sel)
            await button_inter.response.send_message("Choose new captain:", view=v, ephemeral=True)

    # add buttons to view
    view.add_item(InviteButton())
    view.add_item(KickButton())
    view.add_item(AddCoButton())
    view.add_item(RemoveCoButton())
    view.add_item(TransferButton())

    roster_text = f"Team {t.get('name')}\nPlayers: {len(t.get('players',[]))}/{CONFIG.get('roster_limit')}"
    await interaction.response.send_message(roster_text, view=view, ephemeral=True)

# co-captain-panel: similar but fewer buttons
@tree.command(name="co-captain-panel", description="Co-captain panel (co-captains only)")
async def co_captain_panel(interaction: discord.Interaction):
    if not is_co_captain(interaction.user):
        return await interaction.response.send_message("Co-Captain role required.", ephemeral=True)
    # find a team this user is co-captain of
    team = None
    for tid,t in TEAMS.items():
        if str(interaction.user.id) in [str(x) for x in t.get("co_captains",[])]:
            team = (tid,t); break
    if not team:
        return await interaction.response.send_message("No team found for you.", ephemeral=True)
    tid,t = team
    view = discord.ui.View(timeout=None)
    # Invite and Kick only
    class SimpleInvite(discord.ui.Button):
        def __init__(self): super().__init__(label="Invite", style=discord.ButtonStyle.primary)
        async def callback(self, btn_int):
            await captain_panel.callback.__wrapped__(interaction)  # reuse flow roughly
    class SimpleKick(discord.ui.Button):
        def __init__(self): super().__init__(label="Kick", style=discord.ButtonStyle.danger)
        async def callback(self, btn_int):
            await interaction.response.send_message("Please use the captain to kick.", ephemeral=True)
    view.add_item(SimpleInvite())
    view.add_item(SimpleKick())
    await interaction.response.send_message(f"Co-Captain panel for {t.get('name')}", view=view, ephemeral=True)

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
                if str(user.id) in [str(x) for x in t1.get("players",[])] + [str(t1.get("captain_id"))] + [str(x) for x in t1.get("co_captains",[])]:
                    team_key = "team1"
                elif str(user.id) in [str(x) for x in t2.get("players",[])] + [str(t2.get("captain_id"))] + [str(x) for x in t2.get("co_captains",[])]:
                    team_key = "team2"
            if not team_key:
                return await btn_inter.response.send_message("You are not part of the teams involved.", ephemeral=True)
            # set accepted
            if team_key == "team1":
                m["accepted"]["team1"] = True
            else:
                m["accepted"]["team2"] = True
            save_all()
            # edit original message to show check
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
    if not m: return
    t1 = TEAMS.get(m["team1_id"])
    t2 = TEAMS.get(m["team2_id"])
    # find head/refs/casters mentions
    # For simplicity use role mentions from config if present
    ref_role = get_role_by_cfg(guild, "referee")
    caster_role = get_role_by_cfg(guild, "caster")
    head_role = get_role_by_cfg(guild, "head")
    ref_mention = f"<@&{ref_role.id}>" if ref_role else "@Referee"
    caster_mention = f"<@&{caster_role.id}>" if caster_role else "@Caster"
    head_mention = f"<@&{head_role.id}>" if head_role else "@Head"

    content = f"{head_mention} {ref_mention} {caster_mention}\n{t1.get('name')} vs {t2.get('name')}\n> WEEK: {m.get('week')}\n> Time: <t:{m.get('timestamp')}:F>\n> Referee: {m.get('claims',{}).get('referee') or 'Unassigned'}\n> Caster: {m.get('claims',{}).get('caster') or 'Unassigned'}"
    # buttons: Claim Caster, Claim Referee, Unclaim
    class ClaimButton(discord.ui.Button):
        def __init__(self, role):
            super().__init__(style=discord.ButtonStyle.primary, label=f"Claim {role}")
            self.role = role
        async def callback(self, btn_int):
            user = btn_int.user
            mid = match_id
            mm = MATCHES.get(mid)
            if not mm: return await btn_int.response.send_message("Match not found.", ephemeral=True)
            # set claim if not already
            if mm["claims"].get(self.role) and mm["claims"].get(self.role) != str(user.id):
                return await btn_int.response.send_message(f"{self.role} already claimed.", ephemeral=True)
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
            if not mm: return await btn_int.response.send_message("Match not found.", ephemeral=True)
            changed = False
            for role in ["caster","referee"]:
                if mm["claims"].get(role) == str(user.id):
                    mm["claims"][role] = None
                    changed = True
            if changed:
                save_all()
                await btn_int.response.send_message("Your claim(s) removed.", ephemeral=True)
                await update_match_posts(guild, mid)
            else:
                await btn_int.response.send_message("You have no claims.", ephemeral=True)

    view = discord.ui.View(timeout=None)
    view.add_item(ClaimButton("caster"))
    view.add_item(ClaimButton("referee"))
    view.add_item(UnclaimButton())

    # post to a designated assignments channel or fallback to original channel
    channel = None
    if m.get("post_channel"):
        try:
            channel = guild.get_channel(int(m["post_channel"]))
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
    if not m: return
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
    t1 = TEAMS.get(m["team1_id"]); t2 = TEAMS.get(m["team2_id"])
    content = f"{get_role_by_cfg(guild,'head') and '<@&'+str(get_role_by_cfg(guild,'head').id)+'>' or '@Head'} {get_role_by_cfg(guild,'referee') and '<@&'+str(get_role_by_cfg(guild,'referee').id)+'>' or '@Referee'} {get_role_by_cfg(guild,'caster') and '<@&'+str(get_role_by_cfg(guild,'caster').id)+'>' or '@Caster'}\n{t1.get('name')} vs {t2.get('name')}\n> WEEK: {m.get('week')}\n> Time: <t:{m.get('timestamp')}:F>\n> Referee: {('<@'+m['claims']['referee']+'>') if m['claims'].get('referee') else 'Unassigned'}\n> Caster: {('<@'+m['claims']['caster']+'>') if m['claims'].get('caster') else 'Unassigned'}"
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
    now_ts = int(datetime.now(tz=ZoneInfo(CONFIG.get("timezone","America/New_York"))).timestamp())
    for mid, m in list(MATCHES.items()):
        if m.get("notified_5min"): continue
        ts = int(m.get("timestamp"))
        if ts - now_ts <= 300 and ts - now_ts > -60:
            # prepare code and DM to refs/casters and post masked code to teams
            code = secrets.token_urlsafe(6).upper()
            m["code"] = code
            m["notified_5min"] = True
            save_all()
            # DM to caster and ref claims if present
            for role in ["caster","referee"]:
                uid = m.get("claims",{}).get(role)
                if uid:
                    member = guild.get_member(int(uid))
                    if member:
                        try:
                            await member.send("# The Code Is:\n\n# THE CODE\n\n***DO NOT SHARE THIS TO ANYONE. IF YOU DO, YOU WILL BE DEMOTED.***\n\nCode: ||"+code+"||")
                        except:
                            pass
            # DM teams in match channel: masked
            t1 = TEAMS.get(m.get("team1_id")); t2 = TEAMS.get(m.get("team2_id"))
            # post in match channel or transactions channel
            ch = None
            if m.get("post_channel"):
                ch = guild.get_channel(int(m.get("post_channel")))
            if not ch:
                ch_id = CONFIG.get("transactions_channel")
                if ch_id:
                    ch = guild.get_channel(int(ch_id))
            if ch:
                try:
                    await ch.send(f"<@&{t1.get('role_id')}> and <@&{t2.get('role_id')}> \n# CODE IS ||{code}||")
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
            await tree.sync(guild=discord.Object(id=gid))
        else:
            await tree.sync()
    except Exception:
        pass
    print(f"Bot ready. Logged in as {bot.user}")
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