# -*- coding: utf-8 -*-
"""
DoorDash LA:RP Bot — FULL
- Ticket system with persistent dropdown panel and per-ticket buttons: Claim, Close, Close w/ Reason (+ Close Request with Approve/Decline)
- Blacklist & unblacklist with log-channel enforcement (adds + revocations) and cache rebuild on startup
- /link AUTO-DETECTS user's forum thread in CHAN_FORUM + /unlink
- Delivery/Incident logging (incident has NO ping), Promotions, Infractions
- Host Deployment (restricted to ROLE_HOST_DEPLOY_CMD)
- Customer Service Welcome (manual command -> posts in CHAN_CUSTOMER_SERVICE)
- All action logs are EMBEDS in LOG_CHAN
- Ticket transcripts posted as message; fallback to file if too long
- Persistent aiohttp web server (for Render); binds $PORT
Put DISCORD_TOKEN and GUILD_ID in your .env
"""

import os, io, json, asyncio
from typing import Optional, Literal, Dict, Any, List, Tuple
import discord
from discord import app_commands, Interaction, Embed, ui
from discord.ext import commands
from aiohttp import web
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

# --------------------------------------------------------------------------------------
# Load env
# --------------------------------------------------------------------------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# --------------------------------------------------------------------------------------
# IDs (channels/roles)
# --------------------------------------------------------------------------------------
# Channels
CHAN_INCIDENT        = 1420773317949784124
CHAN_DELIVERY        = 1420773516512460840
CHAN_FORUM           = 1420780863632965763
CHAN_RESIGNATION     = 1420810835508330557
CHAN_PERMISSION      = 1420811205114859732
CHAN_DEPLOYMENT      = 1420778159879753800
CHAN_PROMOTE         = 1420819381788741653
CHAN_INFRACT         = 1420820006530191511
CHAN_SUGGESTIONS     = 1421029829096243247
CHAN_TRANSCRIPTS     = 1420970087376093214
TICKET_PANEL_CH      = 1420723037015113749
LOG_CHAN             = 1421124715707367434  # general action log (embeds)
BLACKLIST_LOG_CH     = 1421125894168379422  # dedicated blacklist log channel (embeds)
CHAN_CUSTOMER_SERVICE = 1420784105448280136 # manual welcome goes here

# Ticket categories (separate per type)
CAT_GS   = 1420967576153882655
CAT_MC   = 1421019719023984690
CAT_SHR  = 1421019777807290461

# Roles
ROLE_EMPLOYEE_ALL          = 1420838579780714586   # unified employee role for core cmds
ROLE_HOST_DEPLOY_CMD       = 1420777206770171985   # ONLY this role can host deployments
ROLE_TICKET_GS             = 1420721072197861446
ROLE_TICKET_MC             = 1420836510185554074
ROLE_TICKET_SHR            = 1420721073170677783   # staff (also has blacklist perms)
ROLE_PROMOTE               = 1420836510185554074
ROLE_INFRACT               = 1420836510185554074
ROLE_SUGGEST               = 1420757573270769725   # (command disabled but kept)
ROLE_CUST_SERVICE_WELCOMER = 1421102646366044221
STAFF_ROLE_ID              = ROLE_TICKET_SHR

# Assets
DEPLOYMENT_GIF = "https://cdn.discordapp.com/attachments/1420749680538816553/1420834953335275710/togif.gif"

# --------------------------------------------------------------------------------------
# Persistence files
# --------------------------------------------------------------------------------------
LINKS_FILE      = "links.json"            # [{user, forum, thread_id, thread_name}]
DELIVERIES_FILE = "deliveries.json"
TICKETS_FILE    = "tickets.json"
BLACKLIST_FILE  = "blacklist.json"        # { "<user_id>": ["gs","mc","shr"] }
COUNTERS_FILE   = "ticket_counters.json"  # {"gs":n,"mc":n,"shr":n}
AUDIT_FILE      = "audit.jsonl"

def load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return default

def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def ensure_files():
    defaults = {
        LINKS_FILE: [],
        DELIVERIES_FILE: [],
        TICKETS_FILE: [],
        BLACKLIST_FILE: {},
        COUNTERS_FILE: {"gs": 1, "mc": 1, "shr": 1},
    }
    for p, d in defaults.items():
        if not os.path.exists(p):
            save_json(p, d)
ensure_files()

def audit(event: str, payload: dict):
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event, **payload}) + "\n")
    except:
        pass

# --------------------------------------------------------------------------------------
# Bot setup
# --------------------------------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
client = commands.Bot(command_prefix="!", intents=intents)
GUILD_OBJ = discord.Object(id=GUILD_ID) if GUILD_ID else None

def has_any_role(member: discord.Member, role_ids) -> bool:
    ids = set(role_ids if isinstance(role_ids, (list, tuple, set)) else [role_ids])
    return any(r.id in ids for r in member.roles)

# --------------------------------------------------------------------------------------
# Embedded logging
# --------------------------------------------------------------------------------------
async def send_log_embed(guild: discord.Guild, title: str, description: str, color: discord.Color = discord.Color.blurple(), fields: Optional[List[Tuple[str,str,bool]]] = None):
    ch = guild.get_channel(LOG_CHAN)
    if not ch or not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return
    e = Embed(title=title, description=description, color=color)
    e.timestamp = datetime.now(timezone.utc)
    if fields:
        for n,v,inline in fields:
            e.add_field(name=n, value=v, inline=inline)
    try:
        await ch.send(embed=e)
    except:
        pass

# --------------------------------------------------------------------------------------
# Blacklist cache helpers
# --------------------------------------------------------------------------------------
def bl_add_to_cache(uid: int, types: List[str]):
    bl = load_json(BLACKLIST_FILE, {})
    cur = set(bl.get(str(uid), []))
    cur |= set(t.lower() for t in types)
    bl[str(uid)] = sorted(cur)
    save_json(BLACKLIST_FILE, bl)

def bl_remove_from_cache(uid: int, types: List[str]):
    bl = load_json(BLACKLIST_FILE, {})
    cur = set(bl.get(str(uid), []))
    cur -= set(t.lower() for t in types)
    if cur:
        bl[str(uid)] = sorted(cur)
    else:
        bl.pop(str(uid), None)
    save_json(BLACKLIST_FILE, bl)

def bl_has(uid: int, ttype: str) -> bool:
    bl = load_json(BLACKLIST_FILE, {})
    return ttype.lower() in set(bl.get(str(uid), []))

# --------------------------------------------------------------------------------------
# Blacklist log I/O (embeds) + rebuild on startup with revocations support
# --------------------------------------------------------------------------------------
def _types_field_str(types: List[str]) -> str:
    return "all" if set(types) == {"gs","mc","shr"} else ", ".join(types)

def make_blacklist_embed(user_id: int, types: List[str], source: str, actor: Optional[discord.abc.User]) -> Embed:
    e = Embed(title="Blacklist Log", color=discord.Color.dark_red())
    e.add_field(name="User ID", value=str(user_id), inline=False)
    e.add_field(name="Types", value=_types_field_str(types), inline=False)
    e.add_field(name="Source", value=source, inline=True)
    if actor:
        e.add_field(name="By", value=f"{actor} ({actor.id})", inline=True)
    e.timestamp = datetime.now(timezone.utc)
    return e

def make_revocation_embed(user_id: int, types: List[str], actor: Optional[discord.abc.User]) -> Embed:
    e = Embed(title="Blacklist Revoked", color=discord.Color.dark_green())
    e.add_field(name="User ID", value=str(user_id), inline=False)
    e.add_field(name="Types", value=_types_field_str(types), inline=False)
    if actor:
        e.add_field(name="By", value=f"{actor} ({actor.id})", inline=True)
    e.timestamp = datetime.now(timezone.utc)
    return e

async def post_blacklist_log(guild: discord.Guild, user_id: int, types: List[str], source: str, actor: Optional[discord.Member]) -> discord.Message | None:
    ch = guild.get_channel(BLACKLIST_LOG_CH)
    if not ch or not isinstance(ch, discord.TextChannel):
        return None
    emb = make_blacklist_embed(user_id, types, source, actor)
    try:
        return await ch.send(embed=emb)
    except:
        return None

async def find_last_blacklist_message(guild: discord.Guild, user_id: int, match_types: Optional[List[str]] = None, source: Optional[str] = None) -> Optional[discord.Message]:
    ch = guild.get_channel(BLACKLIST_LOG_CH)
    if not ch or not isinstance(ch, discord.TextChannel):
        return None
    async for msg in ch.history(limit=200, oldest_first=False):
        for emb in msg.embeds:
            if (emb.title or "").strip().lower() != "blacklist log":
                continue
            uid = None; types_val = None; src = None
            for f in emb.fields:
                n = (f.name or "").strip().lower()
                if n == "user id":
                    try: uid = int((f.value or "").strip().replace("`",""))
                    except: uid = None
                elif n == "types":
                    types_val = (f.value or "").strip().lower()
                elif n == "source":
                    src = (f.value or "").strip().lower()
            if uid != user_id: continue
            if source and (src or "").lower() != source.lower(): continue
            if match_types:
                if types_val == "all": return msg
                logged = {t.strip() for t in (types_val or "").split(",")}
                if set(t.lower() for t in match_types).issubset(logged): return msg
            else:
                return msg
    return None

async def reply_blacklist_revoked(guild: discord.Guild, target_msg: discord.Message, user_id: int, types: List[str], actor: Optional[discord.Member]):
    try:
        emb = make_revocation_embed(user_id, types, actor)
        await target_msg.reply(embed=emb, mention_author=False)
    except:
        pass

async def rebuild_blacklists_from_log(guild: discord.Guild):
    ch = guild.get_channel(BLACKLIST_LOG_CH)
    if not ch or not isinstance(ch, discord.TextChannel):
        return
    adds: Dict[str, set] = {}
    async for msg in ch.history(limit=400, oldest_first=True):
        for emb in msg.embeds:
            title = (emb.title or "").strip().lower()
            if title == "blacklist log":
                uid = None; types_val = None
                for f in emb.fields:
                    n = (f.name or "").strip().lower()
                    if n == "user id":
                        try: uid = int((f.value or "").strip().replace("`",""))
                        except: uid = None
                    elif n == "types":
                        types_val = (f.value or "").strip().lower()
                if uid is None or not types_val: continue
                tset = {"gs","mc","shr"} if types_val == "all" else {t.strip() for t in types_val.split(",") if t.strip() in {"gs","mc","shr"}}
                if not tset: continue
                s = adds.get(str(uid), set())
                s |= tset
                adds[str(uid)] = s
    async for msg in ch.history(limit=400, oldest_first=True):
        for emb in msg.embeds:
            if (emb.title or "").strip().lower() != "blacklist revoked":
                continue
            uid = None; types_val = None
            for f in emb.fields:
                n = (f.name or "").strip().lower()
                if n == "user id":
                    try: uid = int((f.value or "").strip().replace("`",""))
                    except: uid = None
                elif n == "types":
                    types_val = (f.value or "").strip().lower()
            if uid is None or not types_val: continue
            tset = {"gs","mc","shr"} if types_val == "all" else {t.strip() for t in types_val.split(",") if t.strip() in {"gs","mc","shr"}}
            if not tset: continue
            if str(uid) in adds:
                adds[str(uid)] -= tset
                if not adds[str(uid)]: adds.pop(str(uid), None)
    out = {k: sorted(v) for k,v in adds.items()}
    save_json(BLACKLIST_FILE, out)

# --------------------------------------------------------------------------------------
# Ticket helpers & embeds
# --------------------------------------------------------------------------------------
def next_ticket_number(ttype: str) -> int:
    c = load_json(COUNTERS_FILE, {"gs": 1, "mc": 1, "shr": 1})
    n = int(c.get(ttype, 1))
    c[ttype] = n + 1
    save_json(COUNTERS_FILE, c)
    return n

def ticket_type_meta(ttype: str) -> dict:
    if ttype == "gs":
        return {"label": "General Support", "ping_role": ROLE_TICKET_GS, "bio": "General Support for any questions you may have regarding anything DoorDash related. Our Support Team will be with you as soon as they can.", "visibility_role": None, "category_id": CAT_GS}
    if ttype == "mc":
        return {"label": "Misconduct", "ping_role": ROLE_TICKET_MC, "bio": "Misconduct ticket for reporting misconduct or issues regarding your food delivery.", "visibility_role": None, "category_id": CAT_MC}
    if ttype == "shr":
        return {"label": "Senior High Ranking", "ping_role": ROLE_TICKET_SHR, "bio": "Used to report customer support members, high ranking questions, or reporting NSFW.", "visibility_role": ROLE_TICKET_SHR, "category_id": CAT_SHR}
    raise ValueError("Unknown ticket type")

def make_ticket_embed(ttype: str, opener: discord.Member) -> Embed:
    meta = ticket_type_meta(ttype)
    e = Embed(title=f"{meta['label']} Ticket", description=meta["bio"], color=discord.Color.red())
    e.add_field(name="Opened by", value=opener.mention, inline=True)
    e.set_footer(text="Use the buttons to claim or close this ticket.")
    return e

# --------------------------------------------------------------------------------------
# Ticket storage helpers
# --------------------------------------------------------------------------------------
def get_ticket(ch_id: int) -> Optional[dict]:
    tickets = load_json(TICKETS_FILE, [])
    for t in tickets:
        if str(ch_id) == t.get("id"):
            return t
    return None

def update_ticket(updated: dict):
    tickets = load_json(TICKETS_FILE, [])
    for i, t in enumerate(tickets):
        if t["id"] == updated["id"]:
            tickets[i] = updated
            save_json(TICKETS_FILE, tickets)
            return
    tickets.append(updated)
    save_json(TICKETS_FILE, tickets)

# --------------------------------------------------------------------------------------
# Transcripts (message-first, else file)
# --------------------------------------------------------------------------------------
async def send_transcript(channel: discord.TextChannel, ticket: dict, reason: Optional[str], by: discord.abc.User):
    lines = [
        f"Transcript for {channel.name} (Ticket {ticket.get('type')} #{ticket.get('number')})",
        f"Closed by: {by} ({by.id})",
        f"Reason: {reason or 'None'}",
        "-" * 50
    ]
    async for m in channel.history(limit=1000, oldest_first=True):
        t = m.created_at.replace(tzinfo=timezone.utc).isoformat()
        base = f"[{t}] {m.author} ({m.author.id}): {m.content}"
        lines.append(base)
        for a in m.attachments:
            lines.append(f"[{t}] {m.author} ({m.author.id}) [file]: {a.url}")
    transcript = "\n".join(lines)

    trans = channel.guild.get_channel(CHAN_TRANSCRIPTS)
    if trans and isinstance(trans, (discord.TextChannel, discord.Thread)):
        if len(transcript) < 1900:
            await trans.send(f"```\n{transcript}\n```")
        else:
            file = discord.File(io.BytesIO(transcript.encode()), filename=f"transcript-{channel.id}.txt")
            await trans.send("Transcript too long, sent as file:", file=file)

    ticket["status"] = "closed"
    update_ticket(ticket)

    await send_log_embed(
        channel.guild,
        title="Ticket Closed",
        description=f"{channel.name} closed by <@{by.id}>",
        color=discord.Color.dark_grey(),
        fields=[
            ("Type/Number", f"{ticket.get('type','?').upper()} #{ticket.get('number','?')}", True),
            ("Channel ID", f"`{channel.id}`", True),
            ("Reason", reason or "None", False),
        ],
    )
    try:
        await channel.delete()
    except:
        pass

# --------------------------------------------------------------------------------------
# Ticket button views
# --------------------------------------------------------------------------------------
class TicketActionView(ui.View):
    def __init__(self, ticket: dict):
        super().__init__(timeout=None)  # persistent
        self.ticket = ticket

    @ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="ticket_claim")
    async def claim(self, interaction: Interaction, _):
        meta = ticket_type_meta(self.ticket["type"])
        if not has_any_role(interaction.user, [meta["ping_role"]]):
            return await interaction.response.send_message("Only the pinged role can claim this ticket.", ephemeral=True)
        if self.ticket.get("handler_id"):
            return await interaction.response.send_message("This ticket is already claimed.", ephemeral=True)
        self.ticket["handler_id"] = interaction.user.id
        update_ticket(self.ticket)
        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.", ephemeral=False)
        await send_log_embed(interaction.guild, "Ticket Claimed",
                             f"{interaction.user.mention} claimed {interaction.channel.mention}",
                             color=discord.Color.green(),
                             fields=[("Ticket", f"{self.ticket['type'].upper()} #{self.ticket['number']}", True)])

    @ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_btn(self, interaction: Interaction, _):
        # NOT ephemeral confirmation
        await interaction.response.send_message("Are you sure you want to close the ticket?", view=ConfirmCloseView(self.ticket))

    @ui.button(label="Close w/ Reason", style=discord.ButtonStyle.secondary, custom_id="ticket_close_reason")
    async def close_reason(self, interaction: Interaction, _):
        await interaction.response.send_modal(ReasonModal(self.ticket))

class ConfirmCloseView(ui.View):
    def __init__(self, ticket: dict):
        super().__init__(timeout=180)
        self.ticket = ticket

    @ui.button(label="Yes, close", style=discord.ButtonStyle.danger, custom_id="close_yes")
    async def yes(self, interaction: Interaction, _):
        t = self.ticket
        meta = ticket_type_meta(t["type"])
        if interaction.user.id not in {t.get("opener_id"), t.get("handler_id")} and not has_any_role(interaction.user, [meta["ping_role"], ROLE_TICKET_SHR]):
            return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
        await interaction.response.defer()
        await send_transcript(interaction.channel, t, reason=None, by=interaction.user)

    @ui.button(label="No, cancel", style=discord.ButtonStyle.secondary, custom_id="close_no")
    async def no(self, interaction: Interaction, _):
        await interaction.response.send_message("Close cancelled.", ephemeral=True)
        self.stop()

class ReasonModal(ui.Modal, title="Close with Reason"):
    reason = ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, required=True, max_length=2000)
    def __init__(self, ticket: dict):
        super().__init__(timeout=180)
        self.ticket = ticket
    async def on_submit(self, interaction: Interaction):
        t = self.ticket
        meta = ticket_type_meta(t["type"])
        if interaction.user.id not in {t.get("opener_id"), t.get("handler_id")} and not has_any_role(interaction.user, [meta["ping_role"], ROLE_TICKET_SHR]):
            return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
        await interaction.response.defer()
        await send_transcript(interaction.channel, t, reason=str(self.reason), by=interaction.user)

class CloseRequestView(ui.View):
    def __init__(self, ticket: dict):
        super().__init__(timeout=300)
        self.ticket = ticket

    @ui.button(label="Approve Close", style=discord.ButtonStyle.success, custom_id="approve_close_req")
    async def approve(self, interaction: Interaction, _):
        if interaction.user.id != self.ticket["opener_id"]:
            return await interaction.response.send_message("Only the ticket opener can approve this.", ephemeral=True)
        await interaction.response.defer()
        await send_transcript(interaction.channel, self.ticket, reason="Approved by opener", by=interaction.user)

    @ui.button(label="Decline", style=discord.ButtonStyle.secondary, custom_id="decline_close_req")
    async def decline(self, interaction: Interaction, _):
        if interaction.user.id != self.ticket["opener_id"]:
            return await interaction.response.send_message("Only the ticket opener can respond.", ephemeral=True)
        await interaction.response.send_message("Close request declined.", ephemeral=True)
        self.stop()

# --------------------------------------------------------------------------------------
# Ticket creation & panel
# --------------------------------------------------------------------------------------
async def create_ticket_channel(guild: discord.Guild, opener: discord.Member, ttype: Literal["gs", "mc", "shr"]) -> discord.TextChannel:
    if bl_has(opener.id, ttype):
        raise app_commands.CheckFailure("User is blacklisted from this ticket type.")
    meta = ticket_type_meta(ttype)
    category = guild.get_channel(meta["category_id"])
    if not isinstance(category, discord.CategoryChannel):
        raise RuntimeError("Configured category not found.")

    num = next_ticket_number(ttype)
    name = f"{ttype}-ticket-{num}"

    overw = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        opener: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
    }
    if meta["visibility_role"]:
        rr = guild.get_role(meta["visibility_role"])
        if rr:
            overw[rr] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
    else:
        rr = guild.get_role(meta["ping_role"])
        if rr:
            overw[rr] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

    ch = await guild.create_text_channel(name=name, category=category, overwrites=overw,
                                         topic=f"{meta['label']} ticket opened by {opener} ({opener.id})")

    ticket = {"id": str(ch.id), "type": ttype, "number": num, "opener_id": opener.id, "handler_id": None, "status": "open"}
    tickets = load_json(TICKETS_FILE, [])
    tickets.append(ticket)
    save_json(TICKETS_FILE, tickets)

    header = f"-# <@&{meta['ping_role']}>"
    embed = make_ticket_embed(ttype, opener)
    await ch.send(content=header, embed=embed, view=TicketActionView(ticket),
                  allowed_mentions=discord.AllowedMentions(roles=True, users=True))

    await send_log_embed(guild, "Ticket Created",
                         f"{ch.mention} for {opener.mention}",
                         color=discord.Color.green(),
                         fields=[("Type/Number", f"{ttype.upper()} #{num}", True), ("Channel ID", f"`{ch.id}`", True)])
    return ch

class TicketDropdown(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(self.Select())

    class Select(ui.Select):
        def __init__(self):
            opts = [
                discord.SelectOption(label="General Support", value="gs", description="DoorDash questions"),
                discord.SelectOption(label="Misconduct", value="mc", description="Report misconduct"),
                discord.SelectOption(label="Senior High Ranking", value="shr", description="Staff/NSFW/HR"),
            ]
            super().__init__(placeholder="Select a ticket type…", options=opts, custom_id="ticket_dropdown")

        async def callback(self, interaction: Interaction):
            ttype = self.values[0]
            if bl_has(interaction.user.id, ttype):
                return await interaction.response.send_message("You are blacklisted from opening this ticket type.", ephemeral=True)
            ch = await create_ticket_channel(interaction.guild, interaction.user, ttype)
            await interaction.response.send_message(f"Ticket created: {ch.mention}", ephemeral=True)

async def ensure_ticket_panel(guild: discord.Guild):
    ch = guild.get_channel(TICKET_PANEL_CH)
    if not ch or not isinstance(ch, discord.TextChannel):
        return
    async for msg in ch.history(limit=25):
        if msg.author == guild.me and msg.components:
            return
    embed = Embed(
        title="DoorDash Support Tickets",
        description="Use the dropdown below to open a ticket.\n• **General Support**\n• **Misconduct**\n• **Senior High Ranking**",
        color=discord.Color.red()
    )
    await ch.send(embed=embed, view=TicketDropdown())

# --------------------------------------------------------------------------------------
# Forum thread auto-detect for /link
# --------------------------------------------------------------------------------------
async def _find_user_forum_thread(guild: discord.Guild, user_id: int) -> Optional[discord.Thread]:
    forum = guild.get_channel(CHAN_FORUM)
    if not forum or not isinstance(forum, discord.ForumChannel):
        return None

    candidates: List[discord.Thread] = []

    try:
        for th in forum.threads:
            if getattr(th, "owner_id", None) == user_id:
                candidates.append(th)
    except:
        pass

    # Try archived APIs available in discord.py 2.x
    try:
        async for th, _ in forum.archived_threads(limit=100):
            if getattr(th, "owner_id", None) == user_id:
                candidates.append(th)
    except:
        try:
            async for th, _ in forum.public_archived_threads(limit=100):
                if getattr(th, "owner_id", None) == user_id:
                    candidates.append(th)
        except:
            pass

    if not candidates:
        return None

    def _key(t: discord.Thread) -> Tuple[int, int]:
        ts = int(t.created_at.timestamp()) if t.created_at else 0
        return (ts, t.id)

    candidates.sort(key=_key, reverse=True)
    return candidates[0]

# --------------------------------------------------------------------------------------
# Commands — blacklist add/remove, tickets, link/unlink, delivery, incident, deployments, HR, sync, disabled cmds, welcome
# --------------------------------------------------------------------------------------
# Blacklist add
@client.tree.command(guild=GUILD_OBJ, name="ticket_blacklist", description="Blacklist a user from a ticket type (staff only)")
@app_commands.describe(ttype="gs / mc / shr / all")
async def ticket_blacklist(interaction: Interaction, user: discord.Member, ttype: str):
    if not has_any_role(interaction.user, [ROLE_TICKET_SHR]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if user.id == interaction.user.id:
        return await interaction.response.send_message("You cannot blacklist yourself.", ephemeral=True)
    ttype = ttype.lower()
    if ttype == "all":
        types = ["gs","mc","shr"]
    elif ttype in {"gs","mc","shr"}:
        types = [ttype]
    else:
        return await interaction.response.send_message("Type must be: gs, mc, shr, or all.", ephemeral=True)
    bl_add_to_cache(user.id, types)
    await post_blacklist_log(interaction.guild, user.id, types, source="ticket_blacklist", actor=interaction.user)
    await send_log_embed(interaction.guild, "Blacklist Logged", f"{user.mention} blacklisted", color=discord.Color.dark_red(),
                         fields=[("Types", _types_field_str(types).upper(), True), ("By", interaction.user.mention, True)])
    await interaction.response.send_message(f"Logged blacklist for {user.mention} ({_types_field_str(types).upper()}).", ephemeral=True)

# Blacklist revoke (ticket)
@client.tree.command(guild=GUILD_OBJ, name="ticket_unblacklist", description="Revoke a ticket blacklist (staff only)")
@app_commands.describe(ttype="gs / mc / shr / all")
async def ticket_unblacklist(interaction: Interaction, user: discord.Member, ttype: str):
    if not has_any_role(interaction.user, [ROLE_TICKET_SHR]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    ttype = ttype.lower()
    if ttype == "all":
        types = ["gs","mc","shr"]
    elif ttype in {"gs","mc","shr"}:
        types = [ttype]
    else:
        return await interaction.response.send_message("Type must be: gs, mc, shr, or all.", ephemeral=True)
    bl_remove_from_cache(user.id, types)
    msg = await find_last_blacklist_message(interaction.guild, user.id, match_types=types)
    if msg:
        await reply_blacklist_revoked(interaction.guild, msg, user.id, types, actor=interaction.user)
    else:
        ch = interaction.guild.get_channel(BLACKLIST_LOG_CH)
        if isinstance(ch, discord.TextChannel):
            await ch.send(embed=make_revocation_embed(user.id, types, interaction.user))
    await send_log_embed(interaction.guild, "Blacklist Revoked", f"{user.mention}", color=discord.Color.dark_green(),
                         fields=[("Types", _types_field_str(types).upper(), True), ("By", interaction.user.mention, True)])
    await interaction.response.send_message(f"Revoked blacklist for {user.mention} ({_types_field_str(types).upper()}).", ephemeral=True)

# Driver blacklist — ONLY logs (no roles change)
@client.tree.command(guild=GUILD_OBJ, name="driver_blacklist", description="Log a driver blacklist (staff only)")
async def driver_blacklist(interaction: Interaction, user: discord.Member):
    if not has_any_role(interaction.user, [ROLE_TICKET_SHR]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    types = ["gs","mc","shr"]
    await post_blacklist_log(interaction.guild, user.id, types, source="driver_blacklist", actor=interaction.user)
    await send_log_embed(interaction.guild, "Driver Blacklist Logged", f"{user.mention}", color=discord.Color.dark_red(),
                         fields=[("Types", "ALL", True), ("By", interaction.user.mention, True)])
    await interaction.response.send_message(f"Driver blacklist logged for {user.mention}.", ephemeral=True)

# Driver unblacklist — revoke all
@client.tree.command(guild=GUILD_OBJ, name="driver_unblacklist", description="Revoke a driver blacklist (staff only)")
async def driver_unblacklist(interaction: Interaction, user: discord.Member):
    if not has_any_role(interaction.user, [ROLE_TICKET_SHR]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    types = ["gs","mc","shr"]
    bl_remove_from_cache(user.id, types)
    msg = await find_last_blacklist_message(interaction.guild, user.id, match_types=types, source="driver_blacklist")
    if msg:
        await reply_blacklist_revoked(interaction.guild, msg, user.id, types, actor=interaction.user)
    else:
        ch = interaction.guild.get_channel(BLACKLIST_LOG_CH)
        if isinstance(ch, discord.TextChannel):
            await ch.send(embed=make_revocation_embed(user.id, types, interaction.user))
    await send_log_embed(interaction.guild, "Driver Blacklist Revoked", f"{user.mention}", color=discord.Color.dark_green(),
                         fields=[("Types", "ALL", True), ("By", interaction.user.mention, True)])
    await interaction.response.send_message(f"Driver blacklist revoked for {user.mention}.", ephemeral=True)

# Ticket panel (persistent)
@client.tree.command(guild=GUILD_OBJ, name="ticket_embed", description="Post/ensure the ticket dropdown panel (staff only)")
async def ticket_embed(interaction: Interaction):
    if not has_any_role(interaction.user, [ROLE_TICKET_SHR]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    await ensure_ticket_panel(interaction.guild)
    await send_log_embed(interaction.guild, "Ticket Panel Ensured", f"By {interaction.user.mention} in <#{TICKET_PANEL_CH}>")
    return await interaction.response.send_message("Ticket panel ensured.", ephemeral=True)

# Open ticket (slash)
@client.tree.command(guild=GUILD_OBJ, name="ticket_open", description="Open a ticket.")
@app_commands.choices(ticket_type=[
    app_commands.Choice(name="General Support", value="gs"),
    app_commands.Choice(name="Misconduct", value="mc"),
    app_commands.Choice(name="Senior High Ranking", value="shr"),
])
async def ticket_open(interaction: Interaction, ticket_type: app_commands.Choice[str]):
    ttype = ticket_type.value
    if bl_has(interaction.user.id, ttype):
        return await interaction.response.send_message("You are blacklisted from this ticket type.", ephemeral=True)
    ch = await create_ticket_channel(interaction.guild, interaction.user, ttype)
    await interaction.response.send_message(f"Ticket created: {ch.mention}", ephemeral=True)

# Close ticket (confirm message NOT ephemeral)
@client.tree.command(guild=GUILD_OBJ, name="ticket_close", description="Close this ticket (asks for confirmation).")
async def ticket_close(interaction: Interaction):
    ticket = get_ticket(interaction.channel.id)
    if not ticket or ticket.get("status") == "closed":
        return await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True)
    await interaction.response.send_message("Are you sure you want to close the ticket?", view=ConfirmCloseView(ticket))

# Close request (handler asks opener)
@client.tree.command(guild=GUILD_OBJ, name="ticket_close_request", description="Handler requests close; opener must approve.")
async def ticket_close_request(interaction: Interaction):
    ticket = get_ticket(interaction.channel.id)
    if not ticket or ticket.get("status") == "closed":
        return await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True)
    opener = interaction.guild.get_member(ticket["opener_id"])
    if not opener:
        return await interaction.response.send_message("Opener not found.", ephemeral=True)
    await interaction.response.send_message(
        content=f"{opener.mention}, the handler is requesting to close this ticket. Do you approve?",
        view=CloseRequestView(ticket)
    )

# /link — AUTO-DETECT user's forum thread in CHAN_FORUM
@client.tree.command(guild=GUILD_OBJ, name="link", description="Auto-link your forum thread from the forum channel.")
async def link(interaction: Interaction):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_ALL]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    thread = await _find_user_forum_thread(interaction.guild, interaction.user.id)
    if not thread:
        return await interaction.followup.send("Couldn't find a forum thread you created in the configured forum.", ephemeral=True)

    links = load_json(LINKS_FILE, [])
    links = [l for l in links if l.get("user") != interaction.user.id]
    links.append({"user": interaction.user.id, "forum": CHAN_FORUM, "thread_id": thread.id, "thread_name": thread.name})
    save_json(LINKS_FILE, links)

    await send_log_embed(interaction.guild, "Linked Forum", f"{interaction.user.mention} linked to **{thread.name}** (`{thread.id}`)")
    await interaction.followup.send(f"Linked to your forum thread: **{thread.name}** (`{thread.id}`)", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="unlink", description="Unlink your forum thread (employee role only).")
async def unlink(interaction: Interaction):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_ALL]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    links = load_json(LINKS_FILE, [])
    before = len(links)
    links = [l for l in links if l.get("user") != interaction.user.id]
    after = len(links)
    if before == after:
        return await interaction.response.send_message("You have no linked forum thread.", ephemeral=True)
    save_json(LINKS_FILE, links)
    await send_log_embed(interaction.guild, "Unlinked Forum", f"{interaction.user.mention} removed their link.")
    await interaction.response.send_message("Your forum link has been removed.", ephemeral=True)

# Delivery logging (requires linked) — ADDS proof argument
@client.tree.command(guild=GUILD_OBJ, name="log_delivery", description="Log a delivery (requires you to be linked).")
async def log_delivery(interaction: Interaction,
                       pickup: str, items: str, dropoff: str, tipped: str,
                       duration: str, customer: str, method: str, proof: str):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_ALL]):
        return await interaction.response.send_message("No permission.", ephemeral=True)

    links = load_json(LINKS_FILE, [])
    user_link = next((l for l in links if l.get("user") == interaction.user.id), None)
    if not user_link:
        return await interaction.response.send_message("You must /link first (auto-detects your forum thread).", ephemeral=True)

    embed = Embed(title="Delivery Log", color=discord.Color.green())
    embed.set_image(url=DEPLOYMENT_GIF)
    embed.add_field(name="Pickup", value=pickup, inline=True)
    embed.add_field(name="Items", value=items, inline=True)
    embed.add_field(name="Dropoff", value=dropoff, inline=True)
    embed.add_field(name="Tip", value=tipped, inline=True)
    embed.add_field(name="Duration", value=duration, inline=True)
    embed.add_field(name="Customer", value=customer, inline=True)
    embed.add_field(name="Requested Via", value=method, inline=True)
    embed.add_field(name="Proof", value=proof, inline=False)

    # channel log
    chan = interaction.guild.get_channel(CHAN_DELIVERY)
    await chan.send(embed=embed)

    # also post in the user's forum thread if present
    thread_id = user_link.get("thread_id")
    if thread_id:
        thread = interaction.guild.get_channel(thread_id)
        if isinstance(thread, (discord.Thread, discord.TextChannel)):
            try:
                await thread.send(embed=embed)
            except:
                pass

    deliveries = load_json(DELIVERIES_FILE, [])
    deliveries.append({
        "user": interaction.user.id, "pickup": pickup, "items": items,
        "dropoff": dropoff, "tipped": tipped, "duration": duration,
        "customer": customer, "method": method, "proof": proof, "thread_id": thread_id
    })
    save_json(DELIVERIES_FILE, deliveries)

    await send_log_embed(interaction.guild, "Delivery Logged", f"By {interaction.user.mention}",
                         color=discord.Color.green(),
                         fields=[("Pickup → Dropoff", f"{pickup} → {dropoff}", False)])
    await interaction.response.send_message("Delivery logged.", ephemeral=True)

# Incident logging (NO pings)
@client.tree.command(guild=GUILD_OBJ, name="log_incident", description="Log an incident (no ping).")
async def log_incident(interaction: Interaction, location: str, incident_type: str, reason: str):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_ALL]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    embed = Embed(title="Incident Log", color=discord.Color.red())
    embed.add_field(name="Location", value=location, inline=False)
    embed.add_field(name="Type", value=incident_type, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    chan = interaction.guild.get_channel(CHAN_INCIDENT)
    await chan.send(embed=embed)
    await send_log_embed(interaction.guild, "Incident Logged",
                         f"{interaction.user.mention} logged an incident.",
                         color=discord.Color.red(),
                         fields=[("Type @ Location", f"{incident_type} @ {location}", False)])
    await interaction.response.send_message("Incident logged.", ephemeral=True)

# Deployment (only ROLE_HOST_DEPLOY_CMD can use)
@client.tree.command(guild=GUILD_OBJ, name="host_deployment", description="Host a deployment (role-restricted).")
async def host_deployment(interaction: Interaction, reason: str, location: str, votes: str):
    if not has_any_role(interaction.user, [ROLE_HOST_DEPLOY_CMD]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    embed = Embed(title="Deployment Hosted", color=discord.Color.purple())
    embed.set_image(url=DEPLOYMENT_GIF)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Location", value=location, inline=False)
    embed.add_field(name="Votes", value=votes, inline=False)

    class DeployVoteView(ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.upvoters = set()
            self.downvoters = set()

        @ui.button(label="Upvote", style=discord.ButtonStyle.success, custom_id="dep_up")
        async def up(self, inter: Interaction, _):
            if not has_any_role(inter.user, [ROLE_EMPLOYEE_ALL]):
                return await inter.response.send_message("Employees only.", ephemeral=True)
            self.downvoters.discard(inter.user.id); self.upvoters.add(inter.user.id)
            await inter.response.send_message("Upvoted.", ephemeral=True)

        @ui.button(label="Downvote", style=discord.ButtonStyle.danger, custom_id="dep_down")
        async def down(self, inter: Interaction, _):
            if not has_any_role(inter.user, [ROLE_EMPLOYEE_ALL]):
                return await inter.response.send_message("Employees only.", ephemeral=True)
            self.upvoters.discard(inter.user.id); self.downvoters.add(inter.user.id)
            await inter.response.send_message("Downvoted.", ephemeral=True)

        @ui.button(label="List Voters", style=discord.ButtonStyle.secondary, custom_id="dep_list")
        async def lst(self, inter: Interaction, _):
            if not has_any_role(inter.user, [ROLE_EMPLOYEE_ALL]):
                return await inter.response.send_message("Employees only.", ephemeral=True)
            up_list = ", ".join(f"<@{i}>" for i in self.upvoters) or "None"
            down_list = ", ".join(f"<@{i}>" for i in self.downvoters) or "None"
            em = Embed(title="Deployment Votes", color=discord.Color.blurple())
            em.add_field(name="Upvotes", value=up_list, inline=False)
            em.add_field(name="Downvotes", value=down_list, inline=False)
            await inter.response.send_message(embed=em, ephemeral=True)

    chan = interaction.guild.get_channel(CHAN_DEPLOYMENT)
    await chan.send(embed=embed, view=DeployVoteView())
    await send_log_embed(interaction.guild, "Deployment Hosted",
                         f"By {interaction.user.mention}",
                         color=discord.Color.purple(),
                         fields=[("Location", location, True), ("Reason", reason, True)])
    await interaction.response.send_message("Deployment hosted.", ephemeral=True)

# HR actions
@client.tree.command(guild=GUILD_OBJ, name="promote", description="Promote an employee")
async def promote(interaction: Interaction, employee: discord.Member, old_rank: str, new_rank: str, reason: str, notes: str):
    if not has_any_role(interaction.user, [ROLE_PROMOTE]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    embed = Embed(title="Promotion", color=discord.Color.gold())
    embed.add_field(name="Employee", value=employee.mention, inline=False)
    embed.add_field(name="Old Rank", value=old_rank, inline=True)
    embed.add_field(name="New Rank", value=new_rank, inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Notes", value=notes, inline=False)
    chan = interaction.guild.get_channel(CHAN_PROMOTE)
    await chan.send(content=employee.mention, embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
    await send_log_embed(interaction.guild, "Promotion",
                         f"{interaction.user.mention} promoted {employee.mention}",
                         color=discord.Color.gold(),
                         fields=[("Old → New", f"{old_rank} → {new_rank}", True)])
    await interaction.response.send_message("Promotion logged.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="infraction", description="Infract an employee")
@app_commands.describe(infraction_type="Notice, Warning, Strike, Demotion, Suspension, Termination, Blacklist, Termination+Blacklist")
async def infraction(interaction: Interaction, employee: discord.Member, reason: str, infraction_type: str, proof: str, notes: str, appealable: Literal["yes", "no"]):
    if not has_any_role(interaction.user, [ROLE_INFRACT]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    embed = Embed(title="Infraction", color=discord.Color.dark_red())
    embed.add_field(name="Employee", value=employee.mention, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Type", value=infraction_type, inline=True)
    embed.add_field(name="Proof", value=proof, inline=True)
    embed.add_field(name="Notes", value=notes, inline=False)
    embed.add_field(name="Appealable", value=appealable, inline=True)
    chan = interaction.guild.get_channel(CHAN_INFRACT)
    await chan.send(content=employee.mention, embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
    await send_log_embed(interaction.guild, "Infraction",
                         f"{interaction.user.mention} → {employee.mention}",
                         color=discord.Color.dark_red(),
                         fields=[("Type", infraction_type, True)])
    await interaction.response.send_message("Infraction logged.", ephemeral=True)

# Disabled (kept in code)
@client.tree.command(guild=GUILD_OBJ, name="suggest", description="Command disabled.")
async def suggest(interaction: Interaction, suggestion: str, details: str):
    await interaction.response.send_message("This command is currently disabled.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="permission_request", description="Command disabled.")
async def permission_request(interaction: Interaction, permission: str, reason: str, signed: str):
    await interaction.response.send_message("This command is currently disabled.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="resignation_request", description="Command disabled.")
async def resignation_request(interaction: Interaction, division: str, note: str, ping: str):
    await interaction.response.send_message("This command is currently disabled.", ephemeral=True)

# Customer Service welcome (manual → sends in CHAN_CUSTOMER_SERVICE)
@client.tree.command(guild=GUILD_OBJ, name="customer_service_welcome", description="Welcome a new Customer Service member (staff only).")
async def customer_service_welcome(interaction: Interaction, user: discord.Member):
    if not has_any_role(interaction.user, [ROLE_CUST_SERVICE_WELCOMER]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    embed = Embed(
        title="Welcome to Customer Service!",
        description=(
            "Welcome aboard! Please review our guidelines and get ready to assist our community. "
            "A lead will reach out with next steps shortly."
        ),
        color=discord.Color.teal()
    )
    ch = interaction.guild.get_channel(CHAN_CUSTOMER_SERVICE)
    if isinstance(ch, (discord.TextChannel, discord.Thread)):
        await ch.send(content=user.mention, embed=embed)
    await send_log_embed(interaction.guild, "Customer Service Welcome", f"{interaction.user.mention} welcomed {user.mention}", color=discord.Color.teal())
    await interaction.response.send_message("Welcome sent.", ephemeral=True)

# /sync (staff-only, 5-min cooldown)
_last_sync: dict[int, datetime] = {}
@client.tree.command(name="sync", description="Sync slash commands (staff only, 5-minute cooldown)")
async def sync_cmd(interaction: Interaction):
    if not has_any_role(interaction.user, [STAFF_ROLE_ID]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    now = datetime.utcnow()
    last = _last_sync.get(interaction.user.id)
    if last and (now - last) < timedelta(minutes=5):
        remain = 300 - int((now - last).total_seconds())
        return await interaction.response.send_message(f"On cooldown. Try again in ~{remain}s.", ephemeral=True)
    _last_sync[interaction.user.id] = now
    cmds = await client.tree.sync(guild=GUILD_OBJ) if GUILD_OBJ else await client.tree.sync()
    await send_log_embed(interaction.guild, "Slash Commands Synced", f"{interaction.user.mention} synced ({len(cmds)})")
    await interaction.response.send_message(f"Synced {len(cmds)} commands.", ephemeral=True)

# --------------------------------------------------------------------------------------
# on_ready
# --------------------------------------------------------------------------------------
@client.event
async def on_ready():
    print(f"Ready as {client.user} (guild={GUILD_ID})")
    try:
        await client.tree.sync(guild=GUILD_OBJ) if GUILD_OBJ else await client.tree.sync()
    except Exception as e:
        print("Initial sync failed:", e)
    guild = client.get_guild(GUILD_ID)
    if guild:
        try:
            await rebuild_blacklists_from_log(guild)
            print("[blacklist] cache rebuilt from log channel (with revocations)")
        except Exception as e:
            print("[blacklist] rebuild failed:", e)
        await ensure_ticket_panel(guild)
        await send_log_embed(guild, "Bot Online", "Blacklist cache rebuilt; ticket panel verified.", color=discord.Color.green())

# --------------------------------------------------------------------------------------
# Web server (Render)
# --------------------------------------------------------------------------------------
async def _health(_): return web.Response(text="ok")
async def start_web():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()

# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
async def main():
    asyncio.create_task(start_web())
    await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
