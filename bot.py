# -*- coding: utf-8 -*-
"""
DoorDash LA:RP Bot — FULL (all commands) — PATCH
- Only role 1420777206770171985 can /host_deployment
- /log_incident no longer pings anyone (embed only)
- Ticket close confirmation prompt is NOT ephemeral
"""

import os
import io
import json
import asyncio
from typing import Optional, Literal, Dict, Any, List, Tuple

import discord
from discord import app_commands, Interaction, Embed, ui
from discord.ext import commands
from aiohttp import web
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

# --------------------------------------------------------------------------------------
# Load environment
# --------------------------------------------------------------------------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# --------------------------------------------------------------------------------------
# IDs (Channels)
# --------------------------------------------------------------------------------------
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
CHAN_CUSTOMER_SERVICE= 1420784105448280136

# Ticket categories per-type
CAT_GS   = 1420967576153882655
CAT_MC   = 1421019719023984690
CAT_SHR  = 1421019777807290461

# --------------------------------------------------------------------------------------
# IDs (Roles)
# --------------------------------------------------------------------------------------
# Unified employee role that can use: /link, /log_delivery, /log_incident, /permission_request, /resignation_request
ROLE_EMPLOYEE_ALL    = 1420838579780714586

# Host deployment — ONLY THIS ROLE
ROLE_HOST_DEPLOY_CMD = 1420777206770171985

# Ticket pings/visibility
ROLE_TICKET_GS       = 1420721072197861446  # General Support team
ROLE_TICKET_MC       = 1420836510185554074  # Misconduct team
ROLE_TICKET_SHR      = 1420721073170677783  # Senior/High-Ranking
ROLE_SHR_PING_SMALL  = ROLE_TICKET_SHR      # used in small-text pings

# HR actions
ROLE_PROMOTE         = 1420836510185554074
ROLE_INFRACT         = 1420836510185554074

# Suggestions usage role
ROLE_SUGGEST         = 1420757573270769725

# Auto-welcome
ROLE_CUSTOMER_SERVICE= 1420721072197861446

# Staff for /sync
STAFF_ROLE_ID        = ROLE_TICKET_SHR

# --------------------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------------------
DEPLOYMENT_GIF = "https://cdn.discordapp.com/attachments/1420749680538816553/1420834953335275710/togif.gif"

# --------------------------------------------------------------------------------------
# Files (JSON persistence)
# --------------------------------------------------------------------------------------
LINKS_FILE      = "links.json"             # [{user, thread_id, thread_name}]
DELIVERIES_FILE = "deliveries.json"        # [ ... ]
TICKETS_FILE    = "tickets.json"           # [{ id, channel_id, type, number, opener_id, handler_id, status }]
BLACKLIST_FILE  = "blacklist.json"         # { user_id: [types] }
COUNTERS_FILE   = "ticket_counters.json"   # { "gs": n, "mc": n, "shr": n }
AUDIT_FILE      = "audit.jsonl"            # line-delimited events

def load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json(path: str, data) -> None:
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

def audit(event: str, payload: Dict[str, Any]) -> None:
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event, **payload}) + "\n")
    except Exception:
        pass

# --------------------------------------------------------------------------------------
# Bot setup
# --------------------------------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # for transcripts
client = commands.Bot(command_prefix="!", intents=intents)
GUILD_OBJ = discord.Object(id=GUILD_ID) if GUILD_ID else None

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def has_any_role(member: discord.Member, role_ids) -> bool:
    ids = set(role_ids if isinstance(role_ids, (list, tuple, set)) else [role_ids])
    return any(r.id in ids for r in member.roles)

def next_ticket_number(ttype: str) -> int:
    counters = load_json(COUNTERS_FILE, {"gs": 1, "mc": 1, "shr": 1})
    n = int(counters.get(ttype, 1))
    counters[ttype] = n + 1
    save_json(COUNTERS_FILE, counters)
    return n

def get_ticket(ticket_id: str) -> Optional[dict]:
    for t in load_json(TICKETS_FILE, []):
        if t["id"] == ticket_id:
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

def add_ticket(ticket: dict):
    tickets = load_json(TICKETS_FILE, [])
    tickets.append(ticket)
    save_json(TICKETS_FILE, tickets)

def blacklist_add(user_id: int, types: List[str]):
    bl = load_json(BLACKLIST_FILE, {})
    cur = set(bl.get(str(user_id), []))
    cur |= {t.lower() for t in types}
    bl[str(user_id)] = sorted(cur)
    save_json(BLACKLIST_FILE, bl)

def blacklist_remove(user_id: int, types: List[str]):
    bl = load_json(BLACKLIST_FILE, {})
    cur = set(bl.get(str(user_id), []))
    cur -= {t.lower() for t in types}
    bl[str(user_id)] = sorted(cur)
    save_json(BLACKLIST_FILE, bl)

def blacklist_has(user_id: int, ttype: str) -> bool:
    bl = load_json(BLACKLIST_FILE, {})
    return ttype.lower() in set(bl.get(str(user_id), []))

# --------------------------------------------------------------------------------------
# Transcript builder
# --------------------------------------------------------------------------------------
async def build_transcript_txt(channel: discord.TextChannel, ticket: dict) -> discord.File:
    lines = []
    header = [
        f"Ticket: {ticket.get('type','?').upper()} #{ticket.get('number','?')} ({channel.name})",
        f"Channel ID: {channel.id}",
        f"Opened by: {ticket.get('opener_id')}",
        f"Handler: {ticket.get('handler_id')}",
        f"Status: {ticket.get('status')}",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "-" * 60,
    ]
    lines.extend(header)

    async for m in channel.history(limit=1000, oldest_first=True):
        t = m.created_at.replace(tzinfo=timezone.utc).isoformat() if m.created_at else "?"
        author = f"{m.author} ({m.author.id})"
        content = (m.content or "").replace("\r", "")
        if content.strip():
            lines.append(f"[{t}] {author}: {content}")
        for a in m.attachments:
            lines.append(f"[{t}] {author} [attachment]: {a.url}")
        for e in m.embeds:
            lines.append(f"[{t}] {author} [embed]: {e.title or ''} {e.description or ''}".strip())
    data = "\n".join(lines) + "\n"
    return discord.File(fp=io.BytesIO(data.encode("utf-8")), filename=f"transcript-{channel.id}.txt")

async def send_transcript_and_delete(channel: discord.TextChannel, ticket: dict, reason: Optional[str], by: discord.abc.User):
    try:
        file = await build_transcript_txt(channel, ticket)
        embed = Embed(title="Ticket Closed", color=discord.Color.dark_grey())
        embed.add_field(name="Channel", value=f"{channel.name} (`{channel.id}`)", inline=False)
        embed.add_field(name="Type", value=ticket.get("type","?").upper(), inline=True)
        embed.add_field(name="Number", value=str(ticket.get("number","?")), inline=True)
        embed.add_field(name="Closed By", value=f"{by} (`{by.id}`)", inline=False)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        embed.timestamp = datetime.now(timezone.utc)
        trans = channel.guild.get_channel(CHAN_TRANSCRIPTS)
        if trans and isinstance(trans, (discord.TextChannel, discord.Thread)):
            await trans.send(embed=embed, file=file)
    except Exception:
        pass
    ticket["status"] = "closed"
    update_ticket(ticket)
    audit("ticket_closed", {"ticket": ticket, "reason": reason})
    try:
        await channel.delete(reason=reason or "Ticket closed")
    except Exception:
        pass

# --------------------------------------------------------------------------------------
# Ticket UI Views
# --------------------------------------------------------------------------------------
class ConfirmCloseView(ui.View):
    def __init__(self, opener_id: int, handler_id: Optional[int], ticket_id: str):
        super().__init__(timeout=180)
        self.opener_id = opener_id
        self.handler_id = handler_id
        self.ticket_id = ticket_id

    @ui.button(label="Yes, close", style=discord.ButtonStyle.danger, custom_id="close_yes")
    async def yes(self, interaction: Interaction, button: ui.Button):
        t = get_ticket(self.ticket_id)
        if not t:
            return await interaction.response.send_message("Ticket not found.", ephemeral=True)
        needed_role = ticket_role_for_claim(t["type"])
        if interaction.user.id not in {t["opener_id"], t.get("handler_id")} and not has_any_role(interaction.user, [needed_role]):
            return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
        await interaction.response.defer()
        await send_transcript_and_delete(interaction.channel, t, reason=None, by=interaction.user)

    @ui.button(label="No, cancel", style=discord.ButtonStyle.secondary, custom_id="close_no")
    async def no(self, interaction: Interaction, button: ui.Button):
        await interaction.response.send_message("Close cancelled.", ephemeral=True)
        self.stop()

class ReasonModal(ui.Modal, title="Close with Reason"):
    reason = ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, required=True, max_length=2000)

    def __init__(self, ticket: dict):
        super().__init__(timeout=180)
        self.ticket = ticket

    async def on_submit(self, interaction: Interaction):
        needed_role = ticket_role_for_claim(self.ticket["type"])
        if interaction.user.id not in {self.ticket["opener_id"], self.ticket.get("handler_id")} and not has_any_role(interaction.user, [needed_role]):
            return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
        await interaction.response.defer()
        await send_transcript_and_delete(interaction.channel, self.ticket, reason=str(self.reason), by=interaction.user)

class TicketActionView(ui.View):
    def __init__(self, ticket: dict):
        super().__init__(timeout=None)
        self.ticket = ticket

    @ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="ticket_claim")
    async def claim(self, interaction: Interaction, button: ui.Button):
        role_needed = ticket_role_for_claim(self.ticket["type"])
        if not has_any_role(interaction.user, [role_needed]):
            return await interaction.response.send_message("Only the pinged role can claim this ticket.", ephemeral=True)
        if self.ticket.get("handler_id"):
            return await interaction.response.send_message("This ticket is already claimed.", ephemeral=True)
        self.ticket["handler_id"] = interaction.user.id
        update_ticket(self.ticket)
        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.", ephemeral=False)

    @ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_btn(self, interaction: Interaction, button: ui.Button):
        t = self.ticket
        role_needed = ticket_role_for_claim(t["type"])
        if interaction.user.id not in {t["opener_id"], t.get("handler_id")} and not has_any_role(interaction.user, [role_needed]):
            return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
        view = ConfirmCloseView(t["opener_id"], t.get("handler_id"), t["id"])
        # NOT ephemeral (per request)
        await interaction.response.send_message("Are you sure you want to close the ticket?", view=view, ephemeral=False)

    @ui.button(label="Close w/ Reason", style=discord.ButtonStyle.secondary, custom_id="ticket_close_reason")
    async def close_reason(self, interaction: Interaction, button: ui.Button):
        t = self.ticket
        role_needed = ticket_role_for_claim(t["type"])
        if interaction.user.id not in {t["opener_id"], t.get("handler_id")} and not has_any_role(interaction.user, [role_needed]):
            return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
        await interaction.response.send_modal(ReasonModal(t))

# --------------------------------------------------------------------------------------
# Ticket helpers
# --------------------------------------------------------------------------------------
def ticket_type_meta(ttype: str) -> Dict[str, Any]:
    t = ttype.lower()
    if t == "gs":
        return {
            "label": "General Support",
            "ping_role": ROLE_TICKET_GS,
            "bio": ("General Support for any questions you may have regarding anything DoorDash related. "
                    "Our Support Team will be with you as soon as they can."),
            "visibility_role": None,
            "category_id": CAT_GS
        }
    if t == "mc":
        return {
            "label": "Misconduct",
            "ping_role": ROLE_TICKET_MC,
            "bio": "Misconduct ticket for reporting misconduct or issues regarding your food delivery.",
            "visibility_role": None,
            "category_id": CAT_MC
        }
    if t == "shr":
        return {
            "label": "Senior High Ranking",
            "ping_role": ROLE_TICKET_SHR,
            "bio": "Used to report customer support members, high ranking questions, or reporting NSFW.",
            "visibility_role": ROLE_TICKET_SHR,
            "category_id": CAT_SHR
        }
    raise ValueError("Unknown ticket type")

def ticket_role_for_claim(ttype: str) -> int:
    return ticket_type_meta(ttype)["ping_role"]

def make_ticket_embed(ttype: str, opener: discord.Member) -> Embed:
    meta = ticket_type_meta(ttype)
    e = Embed(title=f"{meta['label']} Ticket", description=meta["bio"], color=discord.Color.red())
    e.add_field(name="Opened by", value=opener.mention, inline=True)
    e.set_footer(text="Use the buttons to claim or close this ticket.")
    return e

def make_ticket_view(ticket: dict) -> TicketActionView:
    return TicketActionView(ticket)

async def create_ticket_channel(
    guild: discord.Guild,
    opener: discord.Member,
    ttype: Literal["gs", "mc", "shr"],
    subject: Optional[str] = None,
) -> discord.TextChannel:
    meta = ticket_type_meta(ttype)
    category = guild.get_channel(meta["category_id"])
    if not isinstance(category, discord.CategoryChannel):
        raise RuntimeError("Ticket category ID is not a valid CategoryChannel.")

    num = next_ticket_number(ttype)
    name = f"{ttype}-ticket-{num}"

    everyone = guild.default_role
    overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        opener: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
    }

    if meta["visibility_role"]:  # SHR: only opener + SHR staff
        staff_role = guild.get_role(meta["visibility_role"])
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
    else:  # GS/MC: allow ping role to view/claim
        ping_role = guild.get_role(meta["ping_role"])
        if ping_role:
            overwrites[ping_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

    channel = await guild.create_text_channel(
        name=name, category=category, overwrites=overwrites,
        topic=f"{meta['label']} ticket opened by {opener} ({opener.id})"
    )

    header_ping = f"-# <@&{meta['ping_role']}>"
    embed = make_ticket_embed(ttype, opener)
    if subject:
        embed.add_field(name="Subject", value=subject, inline=False)

    ticket = {
        "id": str(channel.id),
        "channel_id": channel.id,
        "guild_id": guild.id,
        "type": ttype,
        "number": num,
        "opener_id": opener.id,
        "handler_id": None,
        "status": "open"
    }
    add_ticket(ticket)
    audit("ticket_open", {"ticket": ticket})

    await channel.send(content=header_ping, embed=embed, view=make_ticket_view(ticket),
                       allowed_mentions=discord.AllowedMentions(roles=True, users=True))
    return channel

# --------------------------------------------------------------------------------------
# Ticket commands
# --------------------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="ticket_embed", description="Post the ticket dropdown panel (staff only).")
async def ticket_embed(interaction: Interaction):
    if not has_any_role(interaction.user, [ROLE_TICKET_SHR]):
        return await interaction.response.send_message("No permission.", ephemeral=True)

    embed = Embed(
        title="DoorDash Support Tickets",
        description=(
            "**Choose a ticket type from the menu below.**\n\n"
            "• **General Support** — Questions about anything DoorDash related. Our Support Team will assist you shortly.\n"
            "• **Misconduct** — Report misconduct or issues regarding your delivery.\n"
            "• **Senior High Ranking** — Report customer support members, high ranking questions, or NSFW.\n\n"
            "_After opening a ticket, use the buttons to **claim** or **close**._"
        ),
        color=discord.Color.red()
    )

    class TicketSelect(ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(label="General Support", description="Open a General Support ticket", value="gs",  emoji="💬"),
                discord.SelectOption(label="Misconduct",      description="Report misconduct about a delivery", value="mc", emoji="🚫"),
                discord.SelectOption(label="Senior High Ranking", description="Report staff/NSFW or ask HR questions", value="shr", emoji="🏛️"),
            ]
            super().__init__(placeholder="Select a ticket type...", min_values=1, max_values=1, options=options, custom_id="ticket_dropdown_main")

        async def callback(self, inter: Interaction):
            ttype = self.values[0]
            if blacklist_has(inter.user.id, ttype):
                return await inter.response.send_message("You are blacklisted from opening this type of ticket.", ephemeral=True)
            await create_ticket_channel(inter.guild, inter.user, ttype=ttype)
            await inter.response.send_message("Ticket created.", ephemeral=True)

    class TicketDropdownView(ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.add_item(TicketSelect())

    panel_channel = interaction.guild.get_channel(TICKET_PANEL_CH)
    if not panel_channel or not isinstance(panel_channel, discord.TextChannel):
        return await interaction.response.send_message("Configured ticket panel channel not found.", ephemeral=True)

    await panel_channel.send(embed=embed, view=TicketDropdownView())
    await interaction.response.send_message("Ticket panel posted.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="ticket_open", description="Open a ticket.")
@app_commands.describe(subject="Optional subject/topic for your ticket")
@app_commands.choices(ticket_type=[
    app_commands.Choice(name="General Support", value="gs"),
    app_commands.Choice(name="Misconduct", value="mc"),
    app_commands.Choice(name="Senior High Ranking", value="shr"),
])
async def ticket_open(interaction: Interaction, ticket_type: app_commands.Choice[str], subject: Optional[str] = None):
    ttype = ticket_type.value
    if blacklist_has(interaction.user.id, ttype):
        return await interaction.response.send_message("You are blacklisted from opening this type of ticket.", ephemeral=True)
    await create_ticket_channel(interaction.guild, interaction.user, ttype=ttype, subject=subject)
    await interaction.response.send_message("Ticket created.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="ticket_close", description="Close this ticket (asks for confirmation).")
async def ticket_close(interaction: Interaction):
    t = get_ticket(str(interaction.channel.id))
    if not t or t.get("status") == "closed":
        return await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True)
    needed_role = ticket_role_for_claim(t["type"])
    if interaction.user.id not in {t["opener_id"], t.get("handler_id")} and not has_any_role(interaction.user, [needed_role]):
        return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
    view = ConfirmCloseView(t["opener_id"], t.get("handler_id"), t["id"])
    # NOT ephemeral (per request)
    await interaction.response.send_message("Are you sure you want to close the ticket?", view=view, ephemeral=False)

@client.tree.command(guild=GUILD_OBJ, name="ticket_close_request", description="Handler requests close; opener must approve.")
async def ticket_close_request(interaction: Interaction):
    t = get_ticket(str(interaction.channel.id))
    if not t or t.get("status") == "closed":
        return await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True)
    needed_role = ticket_role_for_claim(t["type"])
    if interaction.user.id != t.get("handler_id") and not has_any_role(interaction.user, [needed_role]):
        return await interaction.response.send_message("Only the handler or staff can request close.", ephemeral=True)
    opener = interaction.guild.get_member(t["opener_id"])
    if not opener:
        return await interaction.response.send_message("Opener not found.", ephemeral=True)
    view = ui.View(timeout=300)
    async def approve_cb(inter: Interaction):
        if inter.user.id != t["opener_id"]:
            return await inter.response.send_message("Only the ticket opener can approve this.", ephemeral=True)
        await inter.response.defer()
        await send_transcript_and_delete(inter.channel, t, reason="Approved by opener", by=inter.user)
    async def decline_cb(inter: Interaction):
        if inter.user.id != t["opener_id"]:
            return await inter.response.send_message("Only the ticket opener can respond.", ephemeral=True)
        await inter.response.send_message("Close request declined.", ephemeral=True)
        view.stop()
    view.add_item(ui.Button(label="Approve Close", style=discord.ButtonStyle.success, custom_id="approve_close_req"))
    view.add_item(ui.Button(label="Decline", style=discord.ButtonStyle.secondary, custom_id="decline_close_req"))
    for child in view.children:
        if isinstance(child, ui.Button) and child.custom_id == "approve_close_req":
            child.callback = approve_cb
        if isinstance(child, ui.Button) and child.custom_id == "decline_close_req":
            child.callback = decline_cb
    await interaction.response.send_message(
        content=f"{opener.mention}, the handler is requesting to close this ticket. Do you approve?",
        view=view
    )

@client.tree.command(guild=GUILD_OBJ, name="ticket_blacklist", description="Blacklist or unblacklist users from ticket types. (Staff only)")
@app_commands.choices(action=[
    app_commands.Choice(name="Add", value="add"),
    app_commands.Choice(name="Remove", value="remove"),
])
@app_commands.choices(types=[
    app_commands.Choice(name="General Support", value="gs"),
    app_commands.Choice(name="Misconduct", value="mc"),
    app_commands.Choice(name="Senior High Ranking", value="shr"),
    app_commands.Choice(name="All Types", value="all"),
])
async def ticket_blacklist(interaction: Interaction, user: discord.Member, action: app_commands.Choice[str], types: app_commands.Choice[str]):
    if not has_any_role(interaction.user, [ROLE_TICKET_SHR]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    tlist = ["gs", "mc", "shr"] if types.value == "all" else [types.value]
    if action.value == "add":
        blacklist_add(user.id, tlist)
        audit("blacklist_add", {"by": interaction.user.id, "user": user.id, "types": tlist})
        return await interaction.response.send_message(f"Blacklisted {user.mention} from: {', '.join(tlist)}.", ephemeral=True)
    else:
        blacklist_remove(user.id, tlist)
        audit("blacklist_remove", {"by": interaction.user.id, "user": user.id, "types": tlist})
        return await interaction.response.send_message(f"Removed blacklist for {user.mention} on: {', '.join(tlist)}.", ephemeral=True)

# --------------------------------------------------------------------------------------
# /link — auto-find forum thread created by user in CHAN_FORUM
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
    except Exception:
        pass
    try:
        async for th, _ in forum.archived_threads(limit=100):
            if getattr(th, "owner_id", None) == user_id:
                candidates.append(th)
    except Exception:
        try:
            async for th, _ in forum.public_archived_threads(limit=100):
                if getattr(th, "owner_id", None) == user_id:
                    candidates.append(th)
        except Exception:
            pass
    if not candidates:
        return None
    candidates.sort(key=lambda t: (int(t.created_at.timestamp()) if t.created_at else 0, t.id), reverse=True)
    return candidates[0]

@client.tree.command(guild=GUILD_OBJ, name="link", description="Link your own forum thread automatically (no ID needed).")
async def link(interaction: Interaction):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_ALL]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    thread = await _find_user_forum_thread(interaction.guild, interaction.user.id)
    if not thread:
        return await interaction.followup.send("Could not find a forum thread you created in the configured forum.", ephemeral=True)
    links = [l for l in load_json(LINKS_FILE, []) if l.get("user") != interaction.user.id]
    links.append({"user": interaction.user.id, "thread_id": thread.id, "thread_name": thread.name})
    save_json(LINKS_FILE, links)
    audit("link_set", {"user": interaction.user.id, "thread_id": thread.id})
    await interaction.followup.send(f"Linked to your forum thread: **{thread.name}** (`{thread.id}`)", ephemeral=True)

# --------------------------------------------------------------------------------------
# Delivery / Incident / Resignation / Permission / Deployment
# --------------------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="log_delivery", description="Log a delivery (requires /link first)")
async def log_delivery(interaction: Interaction,
                       pickup: str,
                       items: str,
                       dropoff: str,
                       tipped: str,
                       duration: str,
                       customer: str,
                       method: str):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_ALL]):
        return await interaction.response.send_message("No permission.", ephemeral=True)

    # Require link BEFORE posting anywhere
    links = load_json(LINKS_FILE, [])
    user_link = next((l for l in links if l["user"] == interaction.user.id), None)
    if not user_link:
        return await interaction.response.send_message("⚠️ You must use **/link** first to attach your forum thread.", ephemeral=True)

    # Embed (square-ish look via image + many fields)
    embed = Embed(title="🚗 Delivery Log", color=discord.Color.green())
    embed.set_image(url=DEPLOYMENT_GIF)
    embed.add_field(name="Pickup", value=pickup, inline=True)
    embed.add_field(name="Items", value=items, inline=True)
    embed.add_field(name="Dropoff", value=dropoff, inline=True)
    embed.add_field(name="Tip", value=tipped, inline=True)
    embed.add_field(name="Duration", value=duration, inline=True)
    embed.add_field(name="Customer", value=customer, inline=True)
    embed.add_field(name="Requested Via", value=method, inline=True)
    embed.set_footer(text=f"Logged by {interaction.user}")

    # Send in delivery channel
    chan = client.get_channel(CHAN_DELIVERY)
    if chan:
        try:
            await chan.send(embed=embed)
        except Exception:
            pass

    # Send in user's linked forum thread; fetch if not cached
    thread = interaction.guild.get_thread(user_link["thread_id"])
    if thread is None:
        try:
            fetched = await interaction.guild.fetch_channel(user_link["thread_id"])
            if isinstance(fetched, discord.Thread):
                thread = fetched
        except Exception:
            thread = None
    if thread:
        try:
            await thread.send(embed=embed)
        except Exception:
            pass

    # Save JSON
    deliveries = load_json(DELIVERIES_FILE, [])
    payload = {
        "user": interaction.user.id, "pickup": pickup, "items": items,
        "dropoff": dropoff, "tipped": tipped, "duration": duration,
        "customer": customer, "method": method, "thread_id": user_link.get("thread_id")
    }
    deliveries.append(payload)
    save_json(DELIVERIES_FILE, deliveries)
    audit("delivery_log", {"by": interaction.user.id, "data": payload})
    await interaction.response.send_message("Delivery logged.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="log_incident", description="Log an incident")
async def log_incident(interaction: Interaction, location: str, incident_type: str, reason: str):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_ALL]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    embed = Embed(title="Incident Log", color=discord.Color.red())
    embed.add_field(name="Location", value=location, inline=False)
    embed.add_field(name="Type", value=incident_type, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    chan = client.get_channel(CHAN_INCIDENT)
    # NO PING — just send the embed
    await chan.send(embed=embed)
    audit("incident_log", {"by": interaction.user.id, "loc": location})
    await interaction.response.send_message("Incident logged.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="resignation_request", description="Submit a resignation")
async def resignation_request(interaction: Interaction, division: str, note: str, ping: str):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_ALL]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    embed = Embed(title="Resignation Request", color=discord.Color.orange())
    embed.add_field(name="Division", value=division, inline=False)
    embed.add_field(name="Final Note", value=note, inline=False)
    chan = client.get_channel(CHAN_RESIGNATION)
    await chan.send(content=ping, embed=embed)
    audit("resign", {"by": interaction.user.id, "division": division})
    await interaction.response.send_message("Resignation logged.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="permission_request", description="Submit a permission request")
async def permission_request(interaction: Interaction, permission: str, reason: str, signed: str):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_ALL]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    embed = Embed(title="<:DoorDash:1420463324713320469> Permission Request! <:DoorDash:1420463324713320469>", color=discord.Color.blue())
    embed.add_field(name="Permission", value=permission, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Signed", value=signed, inline=False)
    chan = client.get_channel(CHAN_PERMISSION)
    await chan.send(content=f"-# <@&{ROLE_SHR_PING_SMALL}>", embed=embed, allowed_mentions=discord.AllowedMentions(roles=True))
    audit("perm_request", {"by": interaction.user.id, "perm": permission})
    await interaction.response.send_message("Permission request sent.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="host_deployment", description="Host a deployment")
async def host_deployment(interaction: Interaction, reason: str, location: str, votes: str):
    # ONLY role 1420777206770171985
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
        async def up(self, inter: Interaction, b: ui.Button):
            if not has_any_role(inter.user, [ROLE_EMPLOYEE_ALL]):
                return await inter.response.send_message("Only employees can vote.", ephemeral=True)
            self.downvoters.discard(inter.user.id)
            self.upvoters.add(inter.user.id)
            await inter.response.send_message("Upvoted.", ephemeral=True)

        @ui.button(label="Downvote", style=discord.ButtonStyle.danger, custom_id="dep_down")
        async def down(self, inter: Interaction, b: ui.Button):
            if not has_any_role(inter.user, [ROLE_EMPLOYEE_ALL]):
                return await inter.response.send_message("Only employees can vote.", ephemeral=True)
            self.upvoters.discard(inter.user.id)
            self.downvoters.add(inter.user.id)
            await inter.response.send_message("Downvoted.", ephemeral=True)

        @ui.button(label="List Voters", style=discord.ButtonStyle.secondary, custom_id="dep_list")
        async def lst(self, inter: Interaction, b: ui.Button):
            if not has_any_role(inter.user, [ROLE_EMPLOYEE_ALL]):
                return await inter.response.send_message("Only employees can view.", ephemeral=True)
            up_list = ", ".join(f"<@{i}>" for i in self.upvoters) or "None"
            down_list = ", ".join(f"<@{i}>" for i in self.downvoters) or "None"
            emb = Embed(title="Deployment Votes", color=discord.Color.blurple())
            emb.add_field(name="Upvotes", value=up_list, inline=False)
            emb.add_field(name="Downvotes", value=down_list, inline=False)
            await inter.response.send_message(embed=emb, ephemeral=True)

    chan = client.get_channel(CHAN_DEPLOYMENT)
    await chan.send(embed=embed, view=DeployVoteView())
    audit("host_deploy", {"by": interaction.user.id, "reason": reason})
    await interaction.response.send_message("Deployment hosted.", ephemeral=True)

# --------------------------------------------------------------------------------------
# Promotions / Infractions
# --------------------------------------------------------------------------------------
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
    chan = client.get_channel(CHAN_PROMOTE)
    await chan.send(content=employee.mention, embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
    audit("promote", {"by": interaction.user.id, "emp": employee.id, "new": new_rank})
    await interaction.response.send_message("Promotion logged.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="infraction", description="Infract an employee")
@app_commands.describe(infraction_type="Notice, Warning, Strike, Demotion, Suspension, Termination, Blacklist, Termination + Blacklist")
@app_commands.choices(appealable=[
    app_commands.Choice(name="yes", value="yes"),
    app_commands.Choice(name="no",  value="no"),
])
async def infraction(interaction: Interaction, employee: discord.Member, reason: str, infraction_type: str, proof: str, notes: str, appealable: app_commands.Choice[str]):
    if not has_any_role(interaction.user, [ROLE_INFRACT]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    embed = Embed(title="Infraction", color=discord.Color.dark_red())
    embed.add_field(name="Employee", value=employee.mention, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Type", value=infraction_type, inline=True)
    embed.add_field(name="Proof", value=proof, inline=True)
    embed.add_field(name="Notes", value=notes, inline=False)
    embed.add_field(name="Appealable", value=appealable.value, inline=True)
    chan = client.get_channel(CHAN_INFRACT)
    await chan.send(content=employee.mention, embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
    audit("infraction", {"by": interaction.user.id, "emp": employee.id, "type": infraction_type})
    await interaction.response.send_message("Infraction logged.", ephemeral=True)

# --------------------------------------------------------------------------------------
# Suggestions (/suggest) — with votes + discussion thread
# --------------------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="suggest", description="Post a suggestion with details (creates a discussion thread).")
async def suggest(interaction: Interaction, suggestion: str, details: str):
    if not has_any_role(interaction.user, [ROLE_SUGGEST]):
        return await interaction.response.send_message("No permission.", ephemeral=True)

    embed = Embed(title="💡 New Suggestion", description=suggestion, color=discord.Color.brand_green())
    embed.add_field(name="Details", value=details, inline=False)
    embed.set_image(url=DEPLOYMENT_GIF)
    embed.set_footer(text=f"Suggested by {interaction.user}")

    class SuggestVoteView(ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.upvoters = set()
            self.downvoters = set()

        @ui.button(label="Upvote", style=discord.ButtonStyle.success, custom_id="sug_up")
        async def up(self, inter: Interaction, b: ui.Button):
            self.downvoters.discard(inter.user.id)
            self.upvoters.add(inter.user.id)
            await inter.response.send_message("Upvoted.", ephemeral=True)

        @ui.button(label="Downvote", style=discord.ButtonStyle.danger, custom_id="sug_down")
        async def down(self, inter: Interaction, b: ui.Button):
            self.upvoters.discard(inter.user.id)
            self.downvoters.add(inter.user.id)
            await inter.response.send_message("Downvoted.", ephemeral=True)

        @ui.button(label="List Voters", style=discord.ButtonStyle.secondary, custom_id="sug_list")
        async def list_btn(self, inter: Interaction, b: ui.Button):
            up_list = ", ".join(f"<@{i}>" for i in self.upvoters) or "None"
            down_list = ", ".join(f"<@{i}>" for i in self.downvoters) or "None"
            em = Embed(title="Suggestion Votes", color=discord.Color.blurple())
            em.add_field(name="Upvotes", value=up_list, inline=False)
            em.add_field(name="Downvotes", value=down_list, inline=False)
            await inter.response.send_message(embed=em, ephemeral=True)

    chan = client.get_channel(CHAN_SUGGESTIONS)
    if not isinstance(chan, discord.TextChannel):
        return await interaction.response.send_message("Suggestion channel not found.", ephemeral=True)

    msg = await chan.send(embed=embed, view=SuggestVoteView())

    # Create a thread for discussion (from the message)
    try:
        thread_name = f"suggest-{msg.id}"
        await msg.create_thread(name=thread_name, auto_archive_duration=4320)  # 72h
    except Exception:
        pass

    audit("suggest", {"by": interaction.user.id, "msg_id": msg.id})
    await interaction.response.send_message("Suggestion posted.", ephemeral=True)

# --------------------------------------------------------------------------------------
# /sync (staff-only, 5 minute cooldown)
# --------------------------------------------------------------------------------------
_last_sync_by_user: dict[int, datetime] = {}

@client.tree.command(name="sync", description="Force sync slash commands (staff only, 5 min cooldown)")
async def sync_cmd(interaction: Interaction):
    if not has_any_role(interaction.user, [STAFF_ROLE_ID]):
        return await interaction.response.send_message("You don’t have permission to use this.", ephemeral=True)

    now = datetime.utcnow()
    last = _last_sync_by_user.get(interaction.user.id)
    if last and (now - last) < timedelta(minutes=5):
        remain = 300 - int((now - last).total_seconds())
        return await interaction.response.send_message(f"On cooldown. Try again in ~{remain}s.", ephemeral=True)
    _last_sync_by_user[interaction.user.id] = now

    try:
        if GUILD_OBJ:
            cmds = await client.tree.sync(guild=GUILD_OBJ)
        else:
            cmds = await client.tree.sync()
        await interaction.response.send_message(f"Synced {len(cmds)} commands.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Sync failed: `{e}`", ephemeral=True)

# --------------------------------------------------------------------------------------
# Events (ready, role gain welcome)
# --------------------------------------------------------------------------------------
@client.event
async def on_ready():
    print(f"Bot ready as {client.user}")
    try:
        synced = await client.tree.sync(guild=GUILD_OBJ) if GUILD_OBJ else await client.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print("Sync failed:", e)

@client.event
async def on_member_update(before: discord.Member, after: discord.Member):
    before_roles = {r.id for r in before.roles}
    after_roles = {r.id for r in after.roles}
    if ROLE_CUSTOMER_SERVICE in (after_roles - before_roles):
        chan = client.get_channel(CHAN_CUSTOMER_SERVICE)
        if chan:
            embed = Embed(title="Welcome to Customer Services!", color=discord.Color.teal())
            embed.description = (
                "You have been handpicked by the Customer Service Commander.\n\n"
                "1) DM the Commander for a test.\n"
                "2) Pass a ticket test (pretend to handle a ticket).\n"
                "3) Trial for one week.\n\n"
                "We hope you enjoy your stay!"
            )
            await chan.send(content=after.mention, embed=embed)

# --------------------------------------------------------------------------------------
# Tiny aiohttp web server (non-blocking) for Render Web Service
# --------------------------------------------------------------------------------------
async def _health(request):
    return web.Response(text="ok")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)

    port_str = os.getenv("PORT") or "10000"
    port = int(port_str)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[web] listening on :{port} (PORT env: {os.getenv('PORT')})")

# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
async def main():
    asyncio.create_task(start_web_server())  # non-blocking web server
    await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
