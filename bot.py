# -*- coding: utf-8 -*-
"""
DoorDash LA:RP Bot — FULL (restart-safe)

Fixes:
- Delivery Request status updates on the SAME embed; auto thread; proper claim/end rules
- Promotion sends only one embed
- Tickets: pin main embed, edit Status field in-place on claim/close; store message_id
- On startup: restore ticket panel components + restore all open ticket views
"""

import os, io, json, asyncio
from typing import Optional, Literal, Dict, Any, List, Tuple
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands, Interaction, Embed, ui
from discord.ext import commands
from aiohttp import web
from dotenv import load_dotenv

# --------------------------------------------------------------------------------------
# ENV
# --------------------------------------------------------------------------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# --------------------------------------------------------------------------------------
# CONFIG (update if needed)
# --------------------------------------------------------------------------------------
# Channels
CHAN_INCIDENT              = 1420773317949784124
CHAN_DELIVERY              = 1420773516512460840
CHAN_FORUM                 = 1420780863632965763
CHAN_RESIGNATION           = 1420810835508330557
CHAN_PERMISSION            = 1420811205114859732
CHAN_DEPLOYMENT            = 1420778159879753800
CHAN_PROMOTE               = 1420819381788741653
CHAN_INFRACT               = 1420820006530191511
CHAN_CUSTOMER_SERVICE_WAVE = 1420784105448280136
TICKET_CATEGORY_GS         = 1420967576153882655
TICKET_CATEGORY_MC         = 1421019719023984690
TICKET_CATEGORY_SHR        = 1421019777807290461
TICKET_PANEL_CHANNEL_ID    = 1420723037015113749
CHAN_TRANSCRIPTS           = 1420970087376093214
CHAN_AUDIT_LOG             = 1421124715707367434
CHAN_TICKET_BL_LOG         = 1421125894168379422
CHAN_DELIVERY_REQUESTS     = 1421199639255973908

# Roles
ROLE_EMPLOYEE_CORE         = 1420838579780714586
ROLE_SHR_STAFF             = 1420721073170677783
ROLE_CUSTOMER_SERVICE      = 1420721072197861446
ROLE_PROMOTE               = 1420836510185554074
ROLE_INFRACT               = 1421386330335608873
ROLE_DEPLOY_HOST_ONLY      = 1420777206770171985
ROLE_DELIVERY_REQ_PING     = 1420838579780714586  # ping + permission for delivery request buttons
ROLE_CS_WELCOME_CAN_USE    = 1421102646366044221

# Staff for /sync cooldown
STAFF_ROLE_ID = ROLE_SHR_STAFF

# Assets
DD_EMOJI_THUMB = "https://cdn.discordapp.com/emojis/1420463324713320469.png?size=256&quality=lossless"
PROMO_INFRA_GIF = "https://cdn.discordapp.com/attachments/1371481729310785687/1421444708323950673/attachment.gif"
DEPLOYMENT_GIF  = "https://cdn.discordapp.com/attachments/1420749680538816553/1420834953335275710/togif.gif"
DIVIDER = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"

# --------------------------------------------------------------------------------------
# FILES
# --------------------------------------------------------------------------------------
LINKS_FILE = "links.json"              # [{user,thread_id,thread_name,forum}]
DELIVERIES_FILE = "deliveries.json"
TICKETS_FILE = "tickets.json"          # [{..., "message_id": int (pinned ticket embed)}]
BLACKLIST_FILE = "blacklist.json"      # {user_id: [types]}
COUNTERS_FILE = "ticket_counters.json" # {"gs":n,"mc":n,"shr":n}
AUDIT_FILE = "audit.jsonl"
PERSIST_FILE = "panel.json"            # {"message_id": int}

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
        PERSIST_FILE: {},
    }
    for p, d in defaults.items():
        if not os.path.exists(p): save_json(p, d)
ensure_files()

def audit_file(event: str, payload: Dict[str, Any]) -> None:
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event, **payload}) + "\n")
    except Exception:
        pass

# --------------------------------------------------------------------------------------
# BOT
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
# BLACKLIST HELPERS
# --------------------------------------------------------------------------------------
def bl_add(user_id: int, types: List[str]):
    bl = load_json(BLACKLIST_FILE, {})
    cur = set(bl.get(str(user_id), []))
    cur |= {t.lower() for t in types}
    bl[str(user_id)] = sorted(cur)
    save_json(BLACKLIST_FILE, bl)

def bl_remove(user_id: int, types: List[str]):
    bl = load_json(BLACKLIST_FILE, {})
    cur = set(bl.get(str(user_id), []))
    cur -= {t.lower() for t in types}
    bl[str(user_id)] = sorted(cur)
    save_json(BLACKLIST_FILE, bl)

def bl_has(user_id: int, ttype: str) -> bool:
    bl = load_json(BLACKLIST_FILE, {})
    return ttype.lower() in set(bl.get(str(user_id), []))

# --------------------------------------------------------------------------------------
# TICKETS
# --------------------------------------------------------------------------------------
def ticket_meta(ttype: str) -> Dict[str, Any]:
    t = ttype.lower()
    if t == "gs":
        return {"label": "General Support", "cat": TICKET_CATEGORY_GS,  "ping_role": ROLE_CUSTOMER_SERVICE, "restrict_role": None,
                "bio": "General Support for any DoorDash-related questions. Our Support Team will assist you shortly."}
    if t == "mc":
        return {"label": "Misconduct", "cat": TICKET_CATEGORY_MC, "ping_role": ROLE_PROMOTE, "restrict_role": None,
                "bio": "Report misconduct or issues regarding your food delivery."}
    if t == "shr":
        return {"label": "Senior High Ranking", "cat": TICKET_CATEGORY_SHR, "ping_role": ROLE_SHR_STAFF, "restrict_role": ROLE_SHR_STAFF,
                "bio": "Report customer support members, ask high-ranking questions, or report NSFW."}
    raise ValueError("bad ticket type")

def next_ticket_number(ttype: str) -> int:
    counters = load_json(COUNTERS_FILE, {"gs": 1, "mc": 1, "shr": 1})
    n = int(counters.get(ttype, 1))
    counters[ttype] = n + 1
    save_json(COUNTERS_FILE, counters)
    return n

def get_ticket_by_channel(cid: int) -> Optional[dict]:
    for t in load_json(TICKETS_FILE, []):
        if t["channel_id"] == cid:
            return t
    return None

def save_ticket(ticket: dict):
    arr = load_json(TICKETS_FILE, [])
    for i, t in enumerate(arr):
        if t["id"] == ticket["id"]:
            arr[i] = ticket
            save_json(TICKETS_FILE, arr)
            return
    arr.append(ticket)
    save_json(TICKETS_FILE, arr)

def add_ticket(ticket: dict):
    arr = load_json(TICKETS_FILE, [])
    arr.append(ticket)
    save_json(TICKETS_FILE, arr)

def status_text(ticket: dict) -> str:
    h = ticket.get("handler_id")
    who = f"<@{h}>" if h else "Unclaimed"
    ongoing = "Yes" if h and ticket.get("status") == "open" else "No"
    ended = "Yes" if ticket.get("status") == "closed" else "No"
    return f"**Status**\nClaimed By: {who}\nOngoing: {ongoing}\nEnded: {ended}"

def set_or_update_field(embed: Embed, name: str, value: str, inline: bool = False):
    # replace existing field by name, or add new if missing
    for i, f in enumerate(embed.fields):
        if (f.name or "").strip() == name.strip():
            embed.set_field_at(i, name=name, value=value, inline=inline)
            return
    embed.add_field(name=name, value=value, inline=inline)

async def edit_ticket_embed_status(channel: discord.TextChannel, ticket: dict):
    """Edit the pinned ticket embed message to reflect current status."""
    msg_id = ticket.get("message_id")
    if not msg_id:
        return
    try:
        msg = await channel.fetch_message(msg_id)
        if not msg.embeds:
            return
        emb = msg.embeds[0]
        set_or_update_field(emb, "Status", status_text(ticket), inline=False)
        await msg.edit(embed=emb, view=TicketActionView(ticket))
    except Exception:
        pass

class ConfirmCloseView(ui.View):
    def __init__(self, opener_id: int, handler_id: Optional[int], ticket_id: str):
        super().__init__(timeout=180)
        self.opener_id = opener_id
        self.handler_id = handler_id
        self.ticket_id = ticket_id

    @ui.button(label="Yes, close", style=discord.ButtonStyle.danger, custom_id="close_yes")
    async def yes(self, interaction: Interaction, button: ui.Button):
        t = get_ticket_by_channel(interaction.channel.id)
        if not t or t["id"] != self.ticket_id:
            await interaction.response.send_message("Ticket not found.", ephemeral=True); return
        needed_role = ticket_meta(t["type"])["ping_role"]
        if interaction.user.id not in {t["opener_id"], t.get("handler_id")} and not has_any_role(interaction.user, [needed_role]):
            await interaction.response.send_message("You cannot close this ticket.", ephemeral=True); return
        await interaction.response.defer()
        await close_and_transcript(interaction.guild, interaction.channel, t, reason=None, by=interaction.user)

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
        needed_role = ticket_meta(self.ticket["type"])["ping_role"]
        if interaction.user.id not in {self.ticket["opener_id"], self.ticket.get("handler_id")} and not has_any_role(interaction.user, [needed_role]):
            await interaction.response.send_message("You cannot close this ticket.", ephemeral=True); return
        await interaction.response.defer()
        await close_and_transcript(interaction.guild, interaction.channel, self.ticket, reason=str(self.reason), by=interaction.user)

class TicketActionView(ui.View):
    def __init__(self, ticket: dict):
        super().__init__(timeout=None)
        self.ticket = ticket

    @ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="ticket_claim")
    async def claim(self, interaction: Interaction, button: ui.Button):
        meta = ticket_meta(self.ticket["type"])
        if not has_any_role(interaction.user, [meta["ping_role"]]):
            await interaction.response.send_message("Only the pinged role can claim.", ephemeral=True); return
        if self.ticket.get("handler_id"):
            await interaction.response.send_message("Already claimed.", ephemeral=True); return
        self.ticket["handler_id"] = interaction.user.id
        save_ticket(self.ticket)
        # disable button and update pinned embed's Status field
        button.disabled = True
        await edit_ticket_embed_status(interaction.channel, self.ticket)
        opener = interaction.guild.get_member(self.ticket["opener_id"])
        if opener:
            await interaction.channel.send(f"{opener.mention}, your ticket will be handled by {interaction.user.mention}.")
        await interaction.response.send_message(f"{interaction.user.mention} claimed this ticket.", ephemeral=False)

    @ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_btn(self, interaction: Interaction, button: ui.Button):
        t = self.ticket
        meta = ticket_meta(t["type"])
        if interaction.user.id not in {t["opener_id"], t.get("handler_id")} and not has_any_role(interaction.user, [meta["ping_role"]]):
            await interaction.response.send_message("You cannot close this ticket.", ephemeral=True); return
        view = ConfirmCloseView(t["opener_id"], t.get("handler_id"), t["id"])
        await interaction.response.send_message("Are you sure you want to close the ticket?", view=view, ephemeral=False)

    @ui.button(label="Close w/ Reason", style=discord.ButtonStyle.secondary, custom_id="ticket_close_reason")
    async def close_reason(self, interaction: Interaction, button: ui.Button):
        t = self.ticket
        meta = ticket_meta(t["type"])
        if interaction.user.id not in {t["opener_id"], t.get("handler_id")} and not has_any_role(interaction.user, [meta["ping_role"]]):
            await interaction.response.send_message("You cannot close this ticket.", ephemeral=True); return
        await interaction.response.send_modal(ReasonModal(t))

async def transcript_text(channel: discord.TextChannel, ticket: dict) -> str:
    lines = [
        f"Ticket: {ticket.get('type','?').upper()} #{ticket.get('number','?')} ({channel.name})",
        f"Channel ID: {channel.id}",
        f"Opened by: {ticket.get('opener_id')}",
        f"Handler: {ticket.get('handler_id')}",
        f"Status: {ticket.get('status')}",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "-" * 60,
    ]
    async for m in channel.history(limit=2000, oldest_first=True):
        t = m.created_at.replace(tzinfo=timezone.utc).isoformat() if m.created_at else "?"
        author = f"{m.author} ({m.author.id})"
        content = (m.content or "").replace("\r", "")
        if content.strip(): lines.append(f"[{t}] {author}: {content}")
        for a in m.attachments: lines.append(f"[{t}] {author} [attachment]: {a.url}")
        for e in m.embeds: lines.append(f"[{t}] {author} [embed]: {(e.title or '').strip()} {(e.description or '').strip()}".strip())
    return "\n".join(lines)

async def close_and_transcript(guild: discord.Guild, channel: discord.TextChannel, ticket: dict, reason: Optional[str], by: discord.abc.User):
    text = await transcript_text(channel, ticket)
    trans = guild.get_channel(CHAN_TRANSCRIPTS)
    if trans:
        if len(text) <= 1800:
            emb = Embed(title="Ticket Closed", color=discord.Color.dark_grey())
            emb.add_field(name="Channel", value=f"{channel.name} (`{channel.id}`)", inline=False)
            emb.add_field(name="Type", value=ticket.get("type","?").upper(), inline=True)
            emb.add_field(name="Number", value=str(ticket.get("number","?")), inline=True)
            emb.add_field(name="Closed By", value=f"{by} (`{by.id}`)", inline=False)
            if reason: emb.add_field(name="Reason", value=reason, inline=False)
            emb.description = f"```txt\n{text}\n```"
            await trans.send(embed=emb)
        else:
            await trans.send("Transcript too long — uploading as file.")
            await trans.send(file=discord.File(io.BytesIO(text.encode("utf-8")), filename=f"transcript-{channel.id}.txt"))
    ticket["status"] = "closed"
    save_ticket(ticket)
    try:
        await channel.delete(reason=reason or "Ticket closed")
    except Exception:
        pass

def base_ticket_embed(ttype: str, opener: discord.Member, subject: Optional[str], status_block: str) -> Embed:
    meta = ticket_meta(ttype)
    e = Embed(title=f"{meta['label']} Ticket", description=meta["bio"], color=discord.Color.red())
    e.add_field(name="Opened by", value=opener.mention, inline=False)
    if subject: e.add_field(name="Subject", value=subject, inline=False)
    e.add_field(name="Status", value=status_block, inline=False)
    e.set_footer(text="Use the buttons to claim or close this ticket.")
    return e

def pick_category(guild: discord.Guild, ttype: str) -> discord.CategoryChannel:
    cat_id = ticket_meta(ttype)["cat"]
    cat = guild.get_channel(cat_id)
    if not isinstance(cat, discord.CategoryChannel):
        raise RuntimeError("Ticket category invalid.")
    return cat

async def create_ticket(guild: discord.Guild, opener: discord.Member, ttype: Literal["gs", "mc", "shr"], subject: Optional[str] = None) -> discord.TextChannel:
    meta = ticket_meta(ttype)
    category = pick_category(guild, ttype)
    num = next_ticket_number(ttype)
    name = f"{ttype}-ticket-{num}"

    everyone = guild.default_role
    overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        opener: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
    }
    if ttype == "shr":
        role = guild.get_role(ROLE_SHR_STAFF)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
    else:
        role = guild.get_role(meta["ping_role"])
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

    ch = await guild.create_text_channel(
        name=name, category=category, overwrites=overwrites,
        topic=f"{meta['label']} ticket opened by {opener} ({opener.id})"
    )
    ticket = {
        "id": str(ch.id),
        "channel_id": ch.id,
        "guild_id": guild.id,
        "type": ttype,
        "number": num,
        "opener_id": opener.id,
        "handler_id": None,
        "status": "open",
        "subject": subject or "",
        "message_id": None,
    }
    add_ticket(ticket)

    emb = base_ticket_embed(ttype, opener, subject, status_text(ticket))
    view = TicketActionView(ticket)
    header_ping = f"-# <@&{meta['ping_role']}>"
    msg = await ch.send(content=header_ping, embed=emb, view=view,
                        allowed_mentions=discord.AllowedMentions(roles=True, users=True))
    try:
        await msg.pin()
    except Exception:
        pass
    # store the pinned message id
    ticket["message_id"] = msg.id
    save_ticket(ticket)
    return ch

# Ticket Panel (persistent)
class TicketDropdown(ui.Select):
    def __init__(self):
        opts = [
            discord.SelectOption(label="General Support", value="gs", description="DoorDash questions", emoji="💬"),
            discord.SelectOption(label="Misconduct", value="mc", description="Report misconduct", emoji="🚫"),
            discord.SelectOption(label="Senior High Ranking", value="shr", description="Report staff / HR", emoji="🏛️"),
        ]
        super().__init__(placeholder="Select a ticket type…", min_values=1, max_values=1, options=opts, custom_id="ticket_dropdown_main")

    async def callback(self, interaction: Interaction):
        ttype = self.values[0]
        if bl_has(interaction.user.id, ttype):
            await interaction.response.send_message("You are blacklisted from this ticket type.", ephemeral=True); return
        await create_ticket(interaction.guild, interaction.user, ttype=ttype)
        await interaction.response.send_message("Ticket created.", ephemeral=True)

class TicketPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketDropdown())

async def ensure_ticket_panel(guild: discord.Guild):
    panel_data = load_json(PERSIST_FILE, {})
    ch = guild.get_channel(TICKET_PANEL_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel): return
    msg_id = panel_data.get("message_id")
    view = TicketPanelView()
    if msg_id:
        try:
            msg = await ch.fetch_message(msg_id)
            await msg.edit(view=view)
            return
        except Exception:
            pass
    embed = Embed(
        title="DoorDash Support Tickets",
        description=(
            "**Choose a ticket type from the menu below.**\n\n"
            "• **General Support** — Questions about anything DoorDash related.\n"
            "• **Misconduct** — Report misconduct or issues regarding your delivery.\n"
            "• **Senior High Ranking** — Report staff/NSFW or ask HR questions.\n\n"
            "_After opening, use the buttons to **claim** or **close**._"
        ),
        color=discord.Color.red()
    )
    msg = await ch.send(embed=embed, view=view)
    save_json(PERSIST_FILE, {"message_id": msg.id})

# Commands for tickets
@client.tree.command(guild=GUILD_OBJ, name="ticket_embed", description="Post/refresh the ticket panel (staff only).")
async def ticket_embed(interaction: Interaction):
    if not has_any_role(interaction.user, [ROLE_SHR_STAFF]):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    await ensure_ticket_panel(interaction.guild)
    await interaction.response.send_message("Ticket panel ensured.", ephemeral=True)

@app_commands.choices(ticket_type=[
    app_commands.Choice(name="General Support", value="gs"),
    app_commands.Choice(name="Misconduct", value="mc"),
    app_commands.Choice(name="Senior High Ranking", value="shr"),
])
@client.tree.command(guild=GUILD_OBJ, name="ticket_open", description="Open a ticket.")
async def ticket_open(interaction: Interaction, ticket_type: app_commands.Choice[str], subject: Optional[str] = None):
    ttype = ticket_type.value
    if bl_has(interaction.user.id, ttype):
        await interaction.response.send_message("You are blacklisted from this ticket type.", ephemeral=True); return
    await create_ticket(interaction.guild, interaction.user, ttype=ttype, subject=subject)
    await interaction.response.send_message("Ticket created.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="ticket_close", description="Close this ticket (asks for confirmation).")
async def ticket_close(interaction: Interaction):
    t = get_ticket_by_channel(interaction.channel.id)
    if not t or t.get("status") == "closed":
        await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True); return
    needed_role = ticket_meta(t["type"])["ping_role"]
    if interaction.user.id not in {t["opener_id"], t.get("handler_id")} and not has_any_role(interaction.user, [needed_role]):
        await interaction.response.send_message("You cannot close this ticket.", ephemeral=True); return
    view = ConfirmCloseView(t["opener_id"], t.get("handler_id"), t["id"])
    await interaction.response.send_message("Are you sure you want to close the ticket?", view=view, ephemeral=False)

@client.tree.command(guild=GUILD_OBJ, name="ticket_close_request", description="Handler requests close; opener must approve.")
async def ticket_close_request(interaction: Interaction):
    t = get_ticket_by_channel(interaction.channel.id)
    if not t or t.get("status") == "closed":
        await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True); return
    needed_role = ticket_meta(t["type"])["ping_role"]
    if interaction.user.id != t.get("handler_id") and not has_any_role(interaction.user, [needed_role]):
        await interaction.response.send_message("Only the handler or staff can request close.", ephemeral=True); return
    opener = interaction.guild.get_member(t["opener_id"])
    if not opener:
        await interaction.response.send_message("Opener not found.", ephemeral=True); return
    view = ui.View(timeout=300)
    async def approve(inter: Interaction):
        if inter.user.id != t["opener_id"]:
            await inter.response.send_message("Only the ticket opener can approve.", ephemeral=True); return
        await inter.response.defer()
        await close_and_transcript(inter.guild, inter.channel, t, reason="Approved by opener", by=inter.user)
    async def decline(inter: Interaction):
        if inter.user.id != t["opener_id"]:
            await inter.response.send_message("Only the ticket opener can respond.", ephemeral=True); return
        await inter.response.send_message("Close request declined.", ephemeral=True)
    view.add_item(ui.Button(label="Approve Close", style=discord.ButtonStyle.success))
    view.children[0].callback = approve
    view.add_item(ui.Button(label="Decline", style=discord.ButtonStyle.secondary))
    view.children[1].callback = decline
    await interaction.response.send_message(content=f"{opener.mention}, the handler is requesting to close this ticket. Do you approve?", view=view)

@client.tree.command(guild=GUILD_OBJ, name="ticket_add", description="Add a user to this ticket (handler only).")
async def ticket_add(interaction: Interaction, user: discord.Member):
    t = get_ticket_by_channel(interaction.channel.id)
    if not t or t.get("status") == "closed":
        await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True); return
    if interaction.user.id != t.get("handler_id"):
        await interaction.response.send_message("Only the handler can add users.", ephemeral=True); return
    await interaction.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
    await edit_ticket_embed_status(interaction.channel, t)
    await interaction.response.send_message(f"Added {user.mention} to the ticket.", ephemeral=False)

@client.tree.command(guild=GUILD_OBJ, name="ticket_remove", description="Remove a user from this ticket (handler only).")
async def ticket_remove(interaction: Interaction, user: discord.Member):
    t = get_ticket_by_channel(interaction.channel.id)
    if not t or t.get("status") == "closed":
        await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True); return
    if interaction.user.id != t.get("handler_id"):
        await interaction.response.send_message("Only the handler can remove users.", ephemeral=True); return
    await interaction.channel.set_permissions(user, overwrite=None)
    await interaction.response.send_message(f"Removed {user.mention} from the ticket.", ephemeral=False)

# Ticket Blacklist (log to CHAN_TICKET_BL_LOG; unblacklist replies to last msg)
@client.tree.command(guild=GUILD_OBJ, name="ticket_blacklist", description="Blacklist a user from ticket types (SHR only).")
async def ticket_blacklist(interaction: Interaction, user: discord.Member, types: str):
    if not has_any_role(interaction.user, [ROLE_SHR_STAFF]):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    if user.id == interaction.user.id:
        await interaction.response.send_message("You cannot blacklist yourself.", ephemeral=True); return
    tlist = [t.strip().lower() for t in types.split(",") if t.strip()]
    if "all" in tlist: tlist = ["gs","mc","shr"]
    bl_add(user.id, tlist)
    emb = Embed(title="Ticket Blacklist", color=discord.Color.dark_red())
    emb.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=False)
    emb.add_field(name="Types", value=", ".join(tlist), inline=False)
    ch = interaction.guild.get_channel(CHAN_TICKET_BL_LOG)
    if ch: await ch.send(embed=emb, content=user.mention, allowed_mentions=discord.AllowedMentions(users=True))
    await interaction.response.send_message(f"Blacklisted {user.mention} from: {', '.join(tlist)}.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="ticket_unblacklist", description="Remove blacklist for a user (SHR only).")
async def ticket_unblacklist(interaction: Interaction, user: discord.Member, types: str):
    if not has_any_role(interaction.user, [ROLE_SHR_STAFF]):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    tlist = [t.strip().lower() for t in types.split(",") if t.strip()]
    if "all" in tlist: tlist = ["gs","mc","shr"]
    bl_remove(user.id, tlist)
    await interaction.response.send_message(f"Unblacklisted {user.mention} on: {', '.join(tlist)}.", ephemeral=True)
    ch = interaction.guild.get_channel(CHAN_TICKET_BL_LOG)
    if ch:
        try:
            async for m in ch.history(limit=100):
                if m.author.id == client.user.id and m.embeds:
                    e = m.embeds[0]
                    if "Ticket Blacklist" in (e.title or "") and str(user.id) in "".join((f.value or "") for f in e.fields):
                        await m.reply(f"Blacklist revoked by {interaction.user.mention}.")
                        break
        except Exception:
            pass

# Driver blacklist (log-only)
@client.tree.command(guild=GUILD_OBJ, name="driver_blacklist", description="Log a driver blacklist (SHR only).")
async def driver_blacklist(interaction: Interaction, user: discord.Member, reason: str):
    if not has_any_role(interaction.user, [ROLE_SHR_STAFF]):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    ch = interaction.guild.get_channel(CHAN_TICKET_BL_LOG)
    emb = Embed(title="Driver Blacklist", color=discord.Color.dark_red())
    emb.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=False)
    emb.add_field(name="Reason", value=reason, inline=False)
    if ch: await ch.send(embed=emb, content=user.mention, allowed_mentions=discord.AllowedMentions(users=True))
    await interaction.response.send_message("Driver blacklist logged.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="driver_unblacklist", description="Revoke a driver blacklist (SHR only).")
async def driver_unblacklist(interaction: Interaction, user: discord.Member):
    if not has_any_role(interaction.user, [ROLE_SHR_STAFF]):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    ch = interaction.guild.get_channel(CHAN_TICKET_BL_LOG)
    if ch:
        try:
            async for m in ch.history(limit=100):
                if m.author.id == client.user.id and m.embeds:
                    e = m.embeds[0]
                    if "Driver Blacklist" in (e.title or "") and str(user.id) in "".join((f.value or "") for f in e.fields):
                        await m.reply(f"Driver blacklist revoked by {interaction.user.mention}.")
                        break
        except Exception:
            pass
    await interaction.response.send_message("Driver blacklist revocation noted.", ephemeral=True)

# --------------------------------------------------------------------------------------
# LINK / UNLINK
# --------------------------------------------------------------------------------------
async def find_user_forum_thread(guild: discord.Guild, user_id: int) -> Optional[discord.Thread]:
    forum = guild.get_channel(CHAN_FORUM)
    if not isinstance(forum, discord.ForumChannel): return None
    for th in forum.threads:
        if getattr(th, "owner_id", None) == user_id: return th
    try:
        async for th, _ in forum.public_archived_threads(limit=100):
            if getattr(th, "owner_id", None) == user_id: return th
    except Exception:
        pass
    return None

@client.tree.command(guild=GUILD_OBJ, name="link", description="Link your forum thread automatically.")
async def link(interaction: Interaction):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_CORE]):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    th = await find_user_forum_thread(interaction.guild, interaction.user.id)
    if not th:
        await interaction.followup.send("Could not find a forum thread you created in the forum.", ephemeral=True); return
    links = load_json(LINKS_FILE, [])
    links = [l for l in links if l.get("user") != interaction.user.id]
    links.append({"user": interaction.user.id, "forum": CHAN_FORUM, "thread_id": th.id, "thread_name": th.name})
    save_json(LINKS_FILE, links)
    await interaction.followup.send(f"Linked to **{th.name}** (`{th.id}`)", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="unlink", description="Unlink your forum thread.")
async def unlink(interaction: Interaction):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_CORE]):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    links = load_json(LINKS_FILE, [])
    new = [l for l in links if l.get("user") != interaction.user.id]
    if len(new) == len(links):
        await interaction.response.send_message("You have no linked thread.", ephemeral=True); return
    save_json(LINKS_FILE, new)
    await interaction.response.send_message("Unlinked.", ephemeral=True)

# --------------------------------------------------------------------------------------
# DELIVERY LOG / INCIDENT / DELIVERY REQUEST
# --------------------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="log_delivery", description="Log a delivery (requires /link)")
async def log_delivery(interaction: Interaction,
                       pickup: str, items: str, dropoff: str, tipped: str, duration: str,
                       customer: str, method: str, proof: Optional[str] = None):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_CORE]):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    links = load_json(LINKS_FILE, [])
    user_link = next((l for l in links if l["user"] == interaction.user.id), None)
    if not user_link:
        await interaction.response.send_message("No auto-detect here. A lead must add your link to links.json.", ephemeral=True); return

    emb = Embed(title="Delivery Log", color=discord.Color.green())
    emb.set_image(url=DEPLOYMENT_GIF)
    emb.add_field(name="Pickup", value=pickup, inline=True)
    emb.add_field(name="Items", value=items, inline=True)
    emb.add_field(name="Dropoff", value=dropoff, inline=True)
    emb.add_field(name="Tip", value=tipped, inline=True)
    emb.add_field(name="Duration", value=duration, inline=True)
    emb.add_field(name="Customer", value=customer, inline=True)
    emb.add_field(name="Requested Via", value=method, inline=True)
    if proof: emb.add_field(name="Proof", value=proof, inline=False)

    # channel
    chan = client.get_channel(CHAN_DELIVERY)
    if chan: await chan.send(embed=emb)
    # user's forum thread
    th = interaction.guild.get_thread(user_link["thread_id"])
    if isinstance(th, discord.Thread): await th.send(embed=emb)

    deliveries = load_json(DELIVERIES_FILE, [])
    deliveries.append({
        "user": interaction.user.id, "pickup": pickup, "items": items, "dropoff": dropoff,
        "tipped": tipped, "duration": duration, "customer": customer, "method": method,
        "proof": proof or "", "thread_id": user_link["thread_id"], "ts": datetime.now(timezone.utc).isoformat()
    })
    save_json(DELIVERIES_FILE, deliveries)
    await interaction.response.send_message("Delivery logged.", ephemeral=True)

# INCIDENT (no pings)
@client.tree.command(guild=GUILD_OBJ, name="log_incident", description="Log an incident (no pings).")
async def log_incident(interaction: Interaction, location: str, incident_type: str, reason: str):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_CORE]):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    emb = Embed(title="Incident Log", color=discord.Color.red())
    emb.add_field(name="Location", value=location, inline=False)
    emb.add_field(name="Type", value=incident_type, inline=False)
    emb.add_field(name="Reason", value=reason, inline=False)
    chan = client.get_channel(CHAN_INCIDENT)
    if chan: await chan.send(embed=emb)
    await interaction.response.send_message("Incident logged.", ephemeral=True)

# Delivery Request (status updates on same embed)
class DeliveryReqView(ui.View):
    def __init__(self, ping_role_id: int, message_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.ping_role_id = ping_role_id
        self.claimed_by: Optional[int] = None
        self.message_id = message_id

    async def _edit_status(self, inter: Interaction, ended: bool = False):
        try:
            msg = await inter.channel.fetch_message(self.message_id) if self.message_id else None
            if not msg or not msg.embeds: return
            emb = msg.embeds[0]
            claimed = f"<@{self.claimed_by}>" if self.claimed_by else "Unclaimed"
            ongoing = "No" if ended else ("Yes" if self.claimed_by else "No")
            ended_s = "Yes" if ended else "No"
            set_or_update_field(emb, "Status", f"Claimed By: {claimed}\nOngoing: {ongoing}\nEnded: {ended_s}", inline=False)
            await msg.edit(embed=emb, view=self)
        except Exception:
            pass

    @ui.button(label="Claim Request", style=discord.ButtonStyle.success, custom_id="dr_claim")
    async def claim(self, inter: Interaction, btn: ui.Button):
        if not has_any_role(inter.user, [self.ping_role_id]):
            await inter.response.send_message("Only the pinged role can claim.", ephemeral=True); return
        if self.claimed_by:
            await inter.response.send_message("Already claimed.", ephemeral=True); return
        self.claimed_by = inter.user.id
        btn.disabled = True
        await self._edit_status(inter, ended=False)
        await inter.response.send_message(f"Claimed by {inter.user.mention}.", ephemeral=False)

    @ui.button(label="End Delivery", style=discord.ButtonStyle.danger, custom_id="dr_end")
    async def end(self, inter: Interaction, btn: ui.Button):
        if not has_any_role(inter.user, [self.ping_role_id]) or self.claimed_by != inter.user.id:
            await inter.response.send_message("Only the claimer can end.", ephemeral=True); return
        btn.disabled = True
        await self._edit_status(inter, ended=True)
        await inter.response.send_message("Delivery ended. Thank you!", ephemeral=False)

@client.tree.command(guild=GUILD_OBJ, name="delivery_request", description="Post a delivery request (lead role only).")
async def delivery_request(interaction: Interaction, in_game_name: str, delivery_location: str, restaurant: str, items_food: str):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_CORE]):  # you asked to use this shared role
        await interaction.response.send_message("No permission.", ephemeral=True); return
    emb = Embed(title="Delivery Request", color=discord.Color.blurple())
    emb.add_field(name="In-Game Name", value=in_game_name, inline=True)
    emb.add_field(name="Location", value=delivery_location, inline=True)
    emb.add_field(name="Restaurant", value=restaurant, inline=True)
    emb.add_field(name="Items/Food", value=items_food, inline=False)
    emb.add_field(name="Status", value="Claimed By: Unclaimed\nOngoing: No\nEnded: No", inline=False)
    emb.set_footer(text="Use the buttons below to claim or end.")

    ch = interaction.guild.get_channel(CHAN_DELIVERY_REQUESTS)
    view = DeliveryReqView(ROLE_DELIVERY_REQ_PING)
    msg = await ch.send(content=f"<@&{ROLE_DELIVERY_REQ_PING}>", embed=emb, view=view,
                        allowed_mentions=discord.AllowedMentions(roles=True))
    view.message_id = msg.id
    try:
        await msg.create_thread(name=f"Delivery Discussion • {in_game_name}")
    except Exception:
        pass
    await interaction.response.send_message("Delivery request posted.", ephemeral=True)

# --------------------------------------------------------------------------------------
# PROMOTIONS / INFRACTIONS (single message)
# --------------------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="promote", description="Promote an employee")
async def promote(interaction: Interaction, employee: discord.Member, old_rank: str, new_rank: str, reason: str, notes: str):
    if not has_any_role(interaction.user, [ROLE_PROMOTE]):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    embed = Embed(
        title="DoorDash Promotion",
        description=DIVIDER + "\n[Driver Chat](https://discord.com/channels/1420461249757577329/1420803252093583471) • "
                    "[Ticket Support](https://discord.com/channels/1420461249757577329/1420723037015113749)",
        color=discord.Color.red(),
    )
    embed.set_thumbnail(url=DD_EMOJI_THUMB)
    embed.add_field(name="Employee", value=employee.mention, inline=False)
    embed.add_field(name="Old Rank", value=old_rank or "—", inline=True)
    embed.add_field(name="New Rank", value=new_rank or "—", inline=True)
    embed.add_field(name="Reason", value=reason or "—", inline=False)
    embed.add_field(name="Notes", value=notes or "—", inline=False)
    embed.set_image(url=PROMO_INFRA_GIF)
    embed.timestamp = datetime.now(timezone.utc)
    chan = client.get_channel(CHAN_PROMOTE)
    await chan.send(content=employee.mention, embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
    await interaction.response.send_message("Promotion logged.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="infraction", description="Issue an infraction")
async def infraction(interaction: Interaction, employee: discord.Member, reason: str, infraction_type: str, proof: str, notes: str, appealable: str):
    if not has_any_role(interaction.user, [ROLE_INFRACT]):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    embed = Embed(
        title="DoorDash Infraction",
        description=DIVIDER + "\nIf you believe this infraction is **false**, ping the **primary issuer** in "
                    "[Driver Chat](https://discord.com/channels/1420461249757577329/1420803252093583471) "
                    "or open a ticket in "
                    "[Ticket Support](https://discord.com/channels/1420461249757577329/1420723037015113749).",
        color=discord.Color.dark_red(),
    )
    embed.set_thumbnail(url=DD_EMOJI_THUMB)
    embed.add_field(name="Employee", value=employee.mention, inline=False)
    embed.add_field(name="Type", value=infraction_type or "—", inline=True)
    embed.add_field(name="Appealable", value=appealable or "—", inline=True)
    embed.add_field(name="Reason", value=reason or "—", inline=False)
    embed.add_field(name="Proof", value=proof or "—", inline=False)
    embed.add_field(name="Notes", value=notes or "—", inline=False)
    embed.set_image(url=PROMO_INFRA_GIF)
    embed.timestamp = datetime.now(timezone.utc)
    chan = client.get_channel(CHAN_INFRACT)
    await chan.send(content=employee.mention, embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
    await interaction.response.send_message("Infraction logged.", ephemeral=True)

# --------------------------------------------------------------------------------------
# DISABLED COMMANDS (kept)
# --------------------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="permission_request", description="Command disabled.")
async def permission_request(interaction: Interaction, permission: str, reason: str, signed: str):
    await interaction.response.send_message("Command disabled.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="resignation_request", description="Command disabled.")
async def resignation_request(interaction: Interaction, division: str, note: str, ping: str):
    await interaction.response.send_message("Command disabled.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="suggest", description="Command disabled.")
async def suggest(interaction: Interaction, suggestion: str, details: str):
    await interaction.response.send_message("Command disabled.", ephemeral=True)

# --------------------------------------------------------------------------------------
# CUSTOMER SERVICE WELCOME (manual)
# --------------------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="customer_service_welcome", description="Welcome a new Customer Service member (authorized role only).")
async def customer_service_welcome(interaction: Interaction, user: discord.Member):
    if not has_any_role(interaction.user, [ROLE_CS_WELCOME_CAN_USE]):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    emb = Embed(title="Welcome to Customer Services!", color=discord.Color.teal())
    emb.description = (
        "You have been handpicked by the Customer Service Commander.\n\n"
        "1) Pass the initial test in DMs.\n"
        "2) Take a ticket-handling test.\n"
        "3) Trial for one week.\n\n"
        "We hope you enjoy your stay!"
    )
    ch = interaction.guild.get_channel(CHAN_CUSTOMER_SERVICE_WAVE)
    if ch: await ch.send(content=user.mention, embed=emb)
    await interaction.response.send_message("Welcome message sent.", ephemeral=True)

# --------------------------------------------------------------------------------------
# SYNC (cooldown)
# --------------------------------------------------------------------------------------
_last_sync_by_user: dict[int, datetime] = {}

@client.tree.command(name="sync", description="Force sync slash commands (staff only, 5 min cooldown)")
async def sync_cmd(interaction: Interaction):
    if not has_any_role(interaction.user, [STAFF_ROLE_ID]):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    now = datetime.utcnow()
    last = _last_sync_by_user.get(interaction.user.id)
    if last and (now - last) < timedelta(minutes=5):
        remain = 300 - int((now - last).total_seconds())
        await interaction.response.send_message(f"On cooldown. Try again in ~{remain}s.", ephemeral=True); return
    _last_sync_by_user[interaction.user.id] = now
    try:
        cmds = await client.tree.sync(guild=GUILD_OBJ) if GUILD_OBJ else await client.tree.sync()
        await interaction.response.send_message(f"Synced {len(cmds)} commands.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Sync failed: `{e}`", ephemeral=True)

# --------------------------------------------------------------------------------------
# READY & STARTUP (restore panel + ticket views)
# --------------------------------------------------------------------------------------
@client.event
async def on_ready():
    print(f"Bot ready as {client.user}")
    try:
        await client.tree.sync(guild=GUILD_OBJ) if GUILD_OBJ else await client.tree.sync()
    except Exception as e:
        print("Sync failed:", e)
    # Ensure ticket panel exists & is interactive
    for g in client.guilds:
        if GUILD_ID and g.id != GUILD_ID:
            continue
        await ensure_ticket_panel(g)
        # Restore open ticket views & fix Status field
        tickets = load_json(TICKETS_FILE, [])
        for t in tickets:
            if t.get("status") != "open": continue
            ch = g.get_channel(t["channel_id"])
            if not isinstance(ch, discord.TextChannel): continue
            try:
                msg = await ch.fetch_message(t.get("message_id")) if t.get("message_id") else None
                if msg and msg.embeds:
                    emb = msg.embeds[0]
                    set_or_update_field(emb, "Status", status_text(t), inline=False)
                    await msg.edit(embed=emb, view=TicketActionView(t))
            except Exception:
                pass

# --------------------------------------------------------------------------------------
# WEB SERVER FOR RENDER
# --------------------------------------------------------------------------------------
async def _health(request): return web.Response(text="ok")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[web] listening on :{port}")

# --------------------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------------------
async def main():
    asyncio.create_task(start_web_server())
    await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
