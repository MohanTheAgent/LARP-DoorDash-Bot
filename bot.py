"""
DoorDash LARP Bot - FULL (ASCII-safe)
- Tickets: /ticket_embed, /ticket_open, /ticket_close, /ticket_close_request, /ticket_blacklist, /ticket_unblacklist,
           /ticket_add, /ticket_remove
- Linking: /link, /unlink (require link for /log_delivery)
- Delivery: /log_delivery (requires link), Delivery Request panel: /delivery_request (claim/end with status; auto-thread)
- Incidents: /log_incident (no ping)
- Promotions/Infractions: /promote, /infraction (appealable & type are strings)
- Permission/Resignation: present but DISABLED (description mentions disabled)
- Suggestion: present but DISABLED
- Deployment: /host_deployment (only role 1420777206770171985, no ping)
- SHR ticket visibility: ONLY opener + SHR role
- Transcripts: posted as text; fall back to file if too long
- JSON persistence (auto-created)
- Web server for Render (binds $PORT)
"""

import os
import io
import json
import asyncio
from typing import Optional, Literal, Dict, Any, List

import discord
from discord import app_commands, Interaction, Embed, ui
from discord.ext import commands
from aiohttp import web
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

# -----------------------------------------------------------------------------
# Load environment
# -----------------------------------------------------------------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# -----------------------------------------------------------------------------
# IDs
# -----------------------------------------------------------------------------
# Channels
CHAN_INCIDENT = 1420773317949784124
CHAN_DELIVERY = 1420773516512460840
CHAN_FORUM = 1420780863632965763
CHAN_RESIGNATION = 1420810835508330557
CHAN_PERMISSION = 1420811205114859732
CHAN_DEPLOYMENT = 1420778159879753800
CHAN_PROMOTE = 1420819381788741653
CHAN_INFRACT = 1420820006530191511
CHAN_CUSTOMER_SERVICE = 1420784105448280136
TICKET_CATEGORY_GS = 1420967576153882655
TICKET_CATEGORY_MC = 1421019719023984690
TICKET_CATEGORY_SHR = 1421019777807290461
TICKET_PANEL_CHANNEL_ID = 1420723037015113749
CHAN_TRANSCRIPTS = 1420970087376093214
CHAN_AUDIT_LOG = 1421124715707367434
CHAN_BLACKLIST_LOG = 1421125894168379422
CHAN_DELIVERY_REQUESTS = 1421199639255973908

# Roles
ROLE_EMPLOYEE_CORE = 1420838579780714586
ROLE_LINK = ROLE_EMPLOYEE_CORE
ROLE_DELIVERY = ROLE_EMPLOYEE_CORE
ROLE_RESIGNATION = ROLE_EMPLOYEE_CORE
ROLE_PERMISSION = ROLE_EMPLOYEE_CORE
ROLE_INCIDENT_ANY = ROLE_EMPLOYEE_CORE

ROLE_TICKET_GS  = 1420721072197861446
ROLE_TICKET_MC  = 1420836510185554074
ROLE_TICKET_SHR = 1420721073170677783

ROLE_PROMOTE = 1420836510185554074
ROLE_INFRACT = 1420836510185554074

ROLE_DEPLOYMENT_CAN_USE = 1420777206770171985
ROLE_DEPLOYMENT_PING = ROLE_EMPLOYEE_CORE

ROLE_DELIVERY_REQUEST_PING = 1420838579780714586
ROLE_DELIVERY_REQUEST_ALLOWED = 1420757573270769725

# /sync staff role
STAFF_ROLE_ID = ROLE_TICKET_SHR

# Assets
DEPLOYMENT_GIF = "https://cdn.discordapp.com/attachments/1420749680538816553/1420834953335275710/togif.gif"

# -----------------------------------------------------------------------------
# Files
# -----------------------------------------------------------------------------
LINKS_FILE = "links.json"
DELIVERIES_FILE = "deliveries.json"
TICKETS_FILE = "tickets.json"
BLACKLIST_FILE = "blacklist.json"
COUNTERS_FILE = "ticket_counters.json"
AUDIT_FILE = "audit.jsonl"

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

def audit_file(event: str, payload: Dict[str, Any]) -> None:
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event, **payload}) + "\n")
    except Exception:
        pass

async def audit_embed(guild: discord.Guild, title: str, fields: List[tuple]):
    chan = guild.get_channel(CHAN_AUDIT_LOG)
    if not isinstance(chan, (discord.TextChannel, discord.Thread)):
        return
    emb = Embed(title=title, color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    for name, value, inline in fields:
        emb.add_field(name=name, value=value, inline=inline)
    await chan.send(embed=emb)

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

# -----------------------------------------------------------------------------
# Bot setup
# -----------------------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
client = commands.Bot(command_prefix="!", intents=intents)
GUILD_OBJ = discord.Object(id=GUILD_ID) if GUILD_ID else None

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def has_any_role(member: discord.Member, role_ids) -> bool:
    ids = set(role_ids if isinstance(role_ids, (list, tuple, set)) else [role_ids])
    return any(r.id in ids for r in member.roles)

def ticket_type_meta(ttype: str) -> Dict[str, Any]:
    t = ttype.lower()
    if t == "gs":
        return {
            "label": "General Support",
            "ping_role": ROLE_TICKET_GS,
            "bio": "General Support for any questions you may have regarding anything DoorDash related. Our Support Team will be with you as soon as they can.",
            "visibility_role": None,
            "category_id": TICKET_CATEGORY_GS
        }
    if t == "mc":
        return {
            "label": "Misconduct",
            "ping_role": ROLE_TICKET_MC,
            "bio": "Misconduct ticket for reporting misconduct or issues regarding your food delivery.",
            "visibility_role": None,
            "category_id": TICKET_CATEGORY_MC
        }
    if t == "shr":
        return {
            "label": "Senior High Ranking",
            "ping_role": ROLE_TICKET_SHR,
            "bio": "Used to report customer support members, high ranking questions, or reporting NSFW.",
            "visibility_role": ROLE_TICKET_SHR,
            "category_id": TICKET_CATEGORY_SHR
        }
    raise ValueError("Unknown ticket type")

def ticket_role_for_claim(ttype: str) -> int:
    return ticket_type_meta(ttype)["ping_role"]

def next_ticket_number(ttype: str) -> int:
    counters = load_json(COUNTERS_FILE, {"gs": 1, "mc": 1, "shr": 1})
    n = int(counters.get(ttype, 1))
    counters[ttype] = n + 1
    save_json(COUNTERS_FILE, counters)
    return n

def get_ticket_by_channel_id(ch_id: int) -> Optional[dict]:
    for t in load_json(TICKETS_FILE, []):
        if int(t.get("channel_id", 0)) == int(ch_id):
            return t
    return None

def update_ticket(updated: dict):
    tickets = load_json(TICKETS_FILE, [])
    for i, t in enumerate(tickets):
        if str(t.get("id")) == str(updated.get("id")):
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

def find_user_link(user_id: int) -> Optional[dict]:
    links = load_json(LINKS_FILE, [])
    return next((l for l in links if int(l.get("user")) == int(user_id)), None)

# -----------------------------------------------------------------------------
# Transcript (text first; fallback to file)
# -----------------------------------------------------------------------------
async def build_transcript_text(channel: discord.TextChannel, ticket: dict) -> str:
    lines = []
    header = [
        "Ticket: {t} #{n} ({name})".format(t=ticket.get("type","?").upper(), n=ticket.get("number","?"), name=channel.name),
        "Channel ID: {cid}".format(cid=channel.id),
        "Opened by: {op}".format(op=ticket.get("opener_id")),
        "Handler: {hd}".format(hd=ticket.get("handler_id")),
        "Status: {st}".format(st=ticket.get("status")),
        "Generated: {ts}".format(ts=datetime.now(timezone.utc).isoformat()),
        "-" * 60,
    ]
    lines.extend(header)

    async for m in channel.history(limit=2000, oldest_first=True):
        t = m.created_at.replace(tzinfo=timezone.utc).isoformat() if m.created_at else "?"
        author = "{name} ({uid})".format(name=str(m.author), uid=m.author.id)
        content = (m.content or "").replace("\r", "")
        if content.strip():
            lines.append("[{t}] {a}: {c}".format(t=t, a=author, c=content))
        for a in m.attachments:
            lines.append("[{t}] {a} [attachment]: {u}".format(t=t, a=author, u=a.url))
        for e in m.embeds:
            lines.append("[{t}] {a} [embed]: {ti} {de}".format(t=t, a=author, ti=e.title or "", de=e.description or ""))
    return "\n".join(lines) + "\n"

async def send_transcript_and_delete(channel: discord.TextChannel, ticket: dict, reason: Optional[str], by: discord.abc.User):
    try:
        text = await build_transcript_text(channel, ticket)
        trans = channel.guild.get_channel(CHAN_TRANSCRIPTS)
        if trans and isinstance(trans, (discord.TextChannel, discord.Thread)):
            if len(text) <= 1800:
                msg = (
                    "**Ticket Closed**\n"
                    "**Channel:** {nm} (`{cid}`)\n"
                    "**Type:** {tp}\n"
                    "**Number:** {num}\n"
                    "**Closed By:** {by} (`{byid}`)\n"
                ).format(nm=channel.name, cid=channel.id, tp=ticket.get("type","?").upper(), num=ticket.get("number","?"), by=str(by), byid=by.id)
                if reason:
                    msg += "**Reason:** {r}\n".format(r=reason)
                msg += "-" * 20 + "\n" + text
                await trans.send(msg)
            else:
                embed = Embed(title="Ticket Closed (Transcript attached)", color=discord.Color.dark_grey())
                embed.add_field(name="Channel", value="{nm} (`{cid}`)".format(nm=channel.name, cid=channel.id), inline=False)
                embed.add_field(name="Type", value=ticket.get("type","?").upper(), inline=True)
                embed.add_field(name="Number", value=str(ticket.get("number","?")), inline=True)
                embed.add_field(name="Closed By", value="{by} (`{id}`)".format(by=str(by), id=by.id), inline=False)
                if reason:
                    embed.add_field(name="Reason", value=reason, inline=False)
                file = discord.File(fp=io.BytesIO(text.encode("utf-8")), filename="transcript-{cid}.txt".format(cid=channel.id))
                await trans.send(embed=embed, file=file)
    except Exception:
        pass

    ticket["status"] = "closed"
    update_ticket(ticket)
    audit_file("ticket_closed", {"ticket": ticket, "reason": reason})
    await audit_embed(channel.guild, "Ticket Closed", [
        ("Channel", "{nm} (`{cid}`)".format(nm=channel.name, cid=channel.id), False),
        ("Type/Number", "{tp} #{n}".format(tp=ticket.get("type","?").upper(), n=ticket.get("number","?")), True),
        ("Closed By", "{by} (`{id}`)".format(by=str(by), id=by.id), True),
        ("Reason", reason or "-", False),
    ])
    try:
        await channel.delete(reason=reason or "Ticket closed")
    except Exception:
        pass

# -----------------------------------------------------------------------------
# Ticket views
# -----------------------------------------------------------------------------
class ConfirmCloseView(ui.View):
    def __init__(self, opener_id: int, handler_id: Optional[int], ticket_id: str):
        super().__init__(timeout=180)
        self.opener_id = opener_id
        self.handler_id = handler_id
        self.ticket_id = ticket_id

    @ui.button(label="Yes, close", style=discord.ButtonStyle.danger, custom_id="close_yes")
    async def yes(self, interaction: Interaction, button: ui.Button):
        t = get_ticket_by_channel_id(interaction.channel.id)
        if not t:
            return await interaction.response.send_message("Ticket not found.", ephemeral=True)
        needed_role = ticket_role_for_claim(t["type"])
        allowed = (
            interaction.user.id in {t["opener_id"], t.get("handler_id")} or
            has_any_role(interaction.user, [needed_role])
        )
        if not allowed:
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
        allowed = (
            interaction.user.id in {self.ticket["opener_id"], self.ticket.get("handler_id")} or
            has_any_role(interaction.user, [needed_role])
        )
        if not allowed:
            return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
        await interaction.response.defer()
        await send_transcript_and_delete(interaction.channel, self.ticket, reason=str(self.reason), by=interaction.user)

class TicketActionView(ui.View):
    def __init__(self, ticket: dict):
        super().__init__(timeout=None)
        self.ticket = ticket
        if self.ticket.get("handler_id"):
            for child in list(self.children):
                if isinstance(child, ui.Button) and child.custom_id == "ticket_claim":
                    self.remove_item(child)

    @ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="ticket_claim")
    async def claim(self, interaction: Interaction, button: ui.Button):
        role_needed = ticket_role_for_claim(self.ticket["type"])
        if not has_any_role(interaction.user, [role_needed]):
            return await interaction.response.send_message("Only the pinged role can claim this ticket.", ephemeral=True)
        if self.ticket.get("handler_id"):
            return await interaction.response.send_message("This ticket is already claimed.", ephemeral=True)

        self.ticket["handler_id"] = interaction.user.id
        update_ticket(self.ticket)

        button.disabled = True
        await interaction.message.edit(view=self)

        opener = interaction.guild.get_member(self.ticket["opener_id"])
        if opener:
            await interaction.channel.send("{op} your ticket will be handled by {h}.".format(op=opener.mention, h=interaction.user.mention))

        await interaction.response.send_message("Ticket claimed.", ephemeral=True)

    @ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_btn(self, interaction: Interaction, button: ui.Button):
        t = get_ticket_by_channel_id(interaction.channel.id)
        if not t or t.get("status") == "closed":
            return await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True)
        role_needed = ticket_role_for_claim(t["type"])
        allowed = (
            interaction.user.id in {t["opener_id"], t.get("handler_id")} or
            has_any_role(interaction.user, [role_needed])
        )
        if not allowed:
            return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
        view = ConfirmCloseView(t["opener_id"], t.get("handler_id"), t["id"])
        await interaction.response.send_message("Are you sure you want to close the ticket?", view=view, ephemeral=False)

    @ui.button(label="Close w/ Reason", style=discord.ButtonStyle.secondary, custom_id="ticket_close_reason")
    async def close_reason(self, interaction: Interaction, button: ui.Button):
        t = get_ticket_by_channel_id(interaction.channel.id)
        if not t or t.get("status") == "closed":
            return await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True)
        role_needed = ticket_role_for_claim(t["type"])
        allowed = (
            interaction.user.id in {t["opener_id"], t.get("handler_id")} or
            has_any_role(interaction.user, [role_needed])
        )
        if not allowed:
            return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
        await interaction.response.send_modal(ReasonModal(t))

# -----------------------------------------------------------------------------
# Ticket creation and commands
# -----------------------------------------------------------------------------
def make_ticket_embed(ttype: str, opener: discord.Member) -> Embed:
    meta = ticket_type_meta(ttype)
    e = Embed(title="{lbl} Ticket".format(lbl=meta["label"]), description=meta["bio"], color=discord.Color.red())
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
    name = "{t}-ticket-{n}".format(t=ttype, n=num)

    everyone = guild.default_role
    overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        opener: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
    }

    if meta["visibility_role"]:
        staff_role = guild.get_role(meta["visibility_role"])
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
    else:
        ping_role = guild.get_role(meta["ping_role"])
        if ping_role:
            overwrites[ping_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

    channel = await guild.create_text_channel(
        name=name, category=category, overwrites=overwrites,
        topic="{lbl} ticket opened by {user} ({uid})".format(lbl=meta["label"], user=str(opener), uid=opener.id)
    )

    header_ping = "-# <@&{rid}>".format(rid=meta["ping_role"])
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
    audit_file("ticket_open", {"ticket": ticket})
    await audit_embed(guild, "Ticket Opened", [
        ("Channel", "{nm} (`{cid}`)".format(nm=channel.name, cid=channel.id), False),
        ("Type/Number", "{tp} #{n}".format(tp=ttype.upper(), n=num), True),
        ("Opened By", "{u} (`{id}`)".format(u=str(opener), id=opener.id), True),
    ])

    await channel.send(content=header_ping, embed=embed, view=make_ticket_view(ticket),
                       allowed_mentions=discord.AllowedMentions(roles=True, users=True))
    return channel

@client.tree.command(guild=GUILD_OBJ, name="ticket_embed", description="Post the ticket dropdown panel (staff only).")
async def ticket_embed(interaction: Interaction):
    if not has_any_role(interaction.user, [ROLE_TICKET_SHR]):
        return await interaction.response.send_message("No permission.", ephemeral=True)

    embed = Embed(
        title="DoorDash Support Tickets",
        description=(
            "Choose a ticket type from the menu below.\n\n"
            "• General Support - Questions about anything DoorDash related. Our Support Team will assist you shortly.\n"
            "• Misconduct - Report misconduct or issues regarding your delivery.\n"
            "• Senior High Ranking - Report customer support members, high ranking questions, or NSFW.\n\n"
            "After opening a ticket, use the buttons to claim or close."
        ),
        color=discord.Color.red()
    )

    class TicketSelect(ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(label="General Support", description="Open a General Support ticket", value="gs"),
                discord.SelectOption(label="Misconduct",      description="Report misconduct about a delivery", value="mc"),
                discord.SelectOption(label="Senior High Ranking", description="Report staff/NSFW or ask HR questions", value="shr"),
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

    panel_channel = interaction.guild.get_channel(TICKET_PANEL_CHANNEL_ID)
    if not panel_channel or not isinstance(panel_channel, discord.TextChannel):
        return await interaction.response.send_message("Configured ticket panel channel not found.", ephemeral=True)

    await panel_channel.send(embed=embed, view=TicketDropdownView())
    await interaction.response.send_message("Ticket panel posted.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="ticket_open", description="Open a ticket.")
@app_commands.describe(subject="Optional subject for your ticket")
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
    t = get_ticket_by_channel_id(interaction.channel.id)
    if not t or t.get("status") == "closed":
        return await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True)
    needed_role = ticket_role_for_claim(t["type"])
    allowed = (
        interaction.user.id in {t["opener_id"], t.get("handler_id")} or
        has_any_role(interaction.user, [needed_role])
    )
    if not allowed:
        return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
    view = ConfirmCloseView(t["opener_id"], t.get("handler_id"), t["id"])
    await interaction.response.send_message("Are you sure you want to close the ticket?", view=view, ephemeral=False)

@client.tree.command(guild=GUILD_OBJ, name="ticket_close_request", description="Handler requests close; opener must approve.")
async def ticket_close_request(interaction: Interaction):
    t = get_ticket_by_channel_id(interaction.channel.id)
    if not t or t.get("status") == "closed":
        return await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True)
    needed_role = ticket_role_for_claim(t["type"])
    allowed = (interaction.user.id == t.get("handler_id")) or has_any_role(interaction.user, [needed_role])
    if not allowed:
        return await interaction.response.send_message("Only the handler or staff can request close.", ephemeral=True)
    opener = interaction.guild.get_member(t["opener_id"])
    if not opener:
        return await interaction.response.send_message("Opener not found.", ephemeral=True)
    view = ui.View(timeout=300)

    class _Approve(ui.Button):
        def __init__(self): super().__init__(label="Approve Close", style=discord.ButtonStyle.success)
        async def callback(self, inter: Interaction):
            if inter.user.id != t["opener_id"]:
                return await inter.response.send_message("Only the ticket opener can approve this.", ephemeral=True)
            await inter.response.defer()
            await send_transcript_and_delete(inter.channel, t, reason="Approved by opener", by=inter.user)

    class _Decline(ui.Button):
        def __init__(self): super().__init__(label="Decline", style=discord.ButtonStyle.secondary)
        async def callback(self, inter: Interaction):
            if inter.user.id != t["opener_id"]:
                return await inter.response.send_message("Only the ticket opener can respond.", ephemeral=True)
            await inter.response.send_message("Close request declined.", ephemeral=True)
            view.stop()

    view.add_item(_Approve())
    view.add_item(_Decline())
    await interaction.response.send_message(
        content="{op}, the handler is requesting to close this ticket. Do you approve?".format(op=opener.mention),
        view=view
    )

@client.tree.command(guild=GUILD_OBJ, name="ticket_blacklist", description="Blacklist users from ticket types. (Staff only)")
@app_commands.choices(types=[
    app_commands.Choice(name="General Support", value="gs"),
    app_commands.Choice(name="Misconduct", value="mc"),
    app_commands.Choice(name="Senior High Ranking", value="shr"),
    app_commands.Choice(name="All Types", value="all"),
])
async def ticket_blacklist(interaction: Interaction, user: discord.Member, types: app_commands.Choice[str]):
    if not has_any_role(interaction.user, [ROLE_TICKET_SHR]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if user.id == interaction.user.id:
        return await interaction.response.send_message("You cannot blacklist yourself.", ephemeral=True)
    tlist = ["gs", "mc", "shr"] if types.value == "all" else [types.value]
    blacklist_add(user.id, tlist)
    bl_chan = interaction.guild.get_channel(CHAN_BLACKLIST_LOG)
    if isinstance(bl_chan, discord.TextChannel):
        emb = Embed(title="Ticket Blacklist Added", color=discord.Color.dark_red(), timestamp=datetime.now(timezone.utc))
        emb.add_field(name="User", value="{u} (`{id}`)".format(u=str(user), id=user.id), inline=False)
        emb.add_field(name="Types", value=", ".join(tlist), inline=False)
        emb.set_footer(text="By {m} ({id})".format(m=str(interaction.user), id=interaction.user.id))
        await bl_chan.send(embed=emb)
    audit_file("blacklist_add", {"by": interaction.user.id, "user": user.id, "types": tlist})
    await interaction.response.send_message("Blacklisted {m} from: {t}.".format(m=user.mention, t=", ".join(tlist)), ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="ticket_unblacklist", description="Remove ticket blacklist (Staff only)")
@app_commands.choices(types=[
    app_commands.Choice(name="General Support", value="gs"),
    app_commands.Choice(name="Misconduct", value="mc"),
    app_commands.Choice(name="Senior High Ranking", value="shr"),
    app_commands.Choice(name="All Types", value="all"),
])
async def ticket_unblacklist(interaction: Interaction, user: discord.Member, types: app_commands.Choice[str]):
    if not has_any_role(interaction.user, [ROLE_TICKET_SHR]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    tlist = ["gs", "mc", "shr"] if types.value == "all" else [types.value]
    blacklist_remove(user.id, tlist)
    bl_chan = interaction.guild.get_channel(CHAN_BLACKLIST_LOG)
    if isinstance(bl_chan, discord.TextChannel):
        emb = Embed(title="Ticket Blacklist Revoked", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        emb.add_field(name="User", value="{u} (`{id}`)".format(u=str(user), id=user.id), inline=False)
        emb.add_field(name="Types", value=", ".join(tlist), inline=False)
        emb.set_footer(text="By {m} ({id})".format(m=str(interaction.user), id=interaction.user.id))
        await bl_chan.send(embed=emb)
    audit_file("blacklist_remove", {"by": interaction.user.id, "user": user.id, "types": tlist})
    await interaction.response.send_message("Removed blacklist for {m} on: {t}.".format(m=user.mention, t=", ".join(tlist)), ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="ticket_add", description="Add a user to this ticket (handler only).")
async def ticket_add(interaction: Interaction, user: discord.Member):
    t = get_ticket_by_channel_id(interaction.channel.id)
    if not t or t.get("status") == "closed":
        return await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True)
    if interaction.user.id != t.get("handler_id"):
        return await interaction.response.send_message("Only the handler can add users.", ephemeral=True)
    try:
        await interaction.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
        await interaction.response.send_message("Added {m} to the ticket.".format(m=user.mention), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message("Failed to add user: `{}`".format(e), ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="ticket_remove", description="Remove a user from this ticket (handler only).")
async def ticket_remove(interaction: Interaction, user: discord.Member):
    t = get_ticket_by_channel_id(interaction.channel.id)
    if not t or t.get("status") == "closed":
        return await interaction.response.send_message("This is not an open ticket channel.", ephemeral=True)
    if interaction.user.id != t.get("handler_id"):
        return await interaction.response.send_message("Only the handler can remove users.", ephemeral=True)
    if user.id in {t["opener_id"], t.get("handler_id")}:
        return await interaction.response.send_message("You cannot remove the opener or handler.", ephemeral=True)
    try:
        await interaction.channel.set_permissions(user, overwrite=None)
        await interaction.response.send_message("Removed {m} from the ticket.".format(m=user.mention), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message("Failed to remove user: `{}`".format(e), ephemeral=True)

# -----------------------------------------------------------------------------
# Link / Unlink
# -----------------------------------------------------------------------------
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
    if not has_any_role(interaction.user, [ROLE_LINK]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    thread = await _find_user_forum_thread(interaction.guild, interaction.user.id)
    if not thread:
        return await interaction.followup.send("Could not find a forum thread you created in the configured forum.", ephemeral=True)
    links = load_json(LINKS_FILE, [])
    links = [l for l in links if int(l.get("user")) != interaction.user.id]
    links.append({"user": interaction.user.id, "forum": CHAN_FORUM, "thread_id": thread.id, "thread_name": thread.name})
    save_json(LINKS_FILE, links)
    audit_file("link_set", {"user": interaction.user.id, "thread_id": thread.id})
    await audit_embed(interaction.guild, "Forum Linked", [
        ("User", "{u} (`{id}`)".format(u=str(interaction.user), id=interaction.user.id), True),
        ("Thread", "{nm} (`{id}`)".format(nm=thread.name, id=thread.id), True),
    ])
    await interaction.followup.send("Linked to your forum thread: {nm} (`{id}`)".format(nm=thread.name, id=thread.id), ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="unlink", description="Unlink your forum (employee role required).")
async def unlink(interaction: Interaction):
    if not has_any_role(interaction.user, [ROLE_EMPLOYEE_CORE]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    links = load_json(LINKS_FILE, [])
    new_links = [l for l in links if int(l.get("user")) != interaction.user.id]
    if len(new_links) == len(links):
        return await interaction.response.send_message("You do not have a linked forum.", ephemeral=True)
    save_json(LINKS_FILE, new_links)
    audit_file("link_unset", {"user": interaction.user.id})
    await audit_embed(interaction.guild, "Forum Unlinked", [
        ("User", "{u} (`{id}`)".format(u=str(interaction.user), id=interaction.user.id), False),
    ])
    await interaction.response.send_message("Unlinked your forum thread.", ephemeral=True)

# -----------------------------------------------------------------------------
# Delivery Log (requires link)
# -----------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="log_delivery", description="Log a delivery (requires /link first)")
async def log_delivery(interaction: Interaction,
                       pickup: str,
                       items: str,
                       dropoff: str,
                       tipped: str,
                       duration: str,
                       customer: str,
                       method: str,
                       proof: str):
    if not has_any_role(interaction.user, [ROLE_DELIVERY]):
        return await interaction.response.send_message("No permission.", ephemeral=True)

    user_link = find_user_link(interaction.user.id)
    if not user_link:
        return await interaction.response.send_message("You must use /link first to attach your forum thread.", ephemeral=True)

    embed = Embed(title="Delivery Log", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_image(url=DEPLOYMENT_GIF)
    embed.add_field(name="Pickup", value=pickup, inline=True)
    embed.add_field(name="Dropoff", value=dropoff, inline=True)
    embed.add_field(name="Items", value=items, inline=False)
    embed.add_field(name="Tip", value=tipped, inline=True)
    embed.add_field(name="Duration", value=duration, inline=True)
    embed.add_field(name="Customer", value=customer, inline=True)
    embed.add_field(name="Requested Via", value=method, inline=True)
    embed.add_field(name="Proof", value=proof, inline=False)
    embed.set_footer(text="Logged by {u} • {id}".format(u=str(interaction.user), id=interaction.user.id))

    chan = client.get_channel(CHAN_DELIVERY)
    await chan.send(embed=embed)

    forum = interaction.guild.get_channel(CHAN_FORUM)
    thread = None
    if isinstance(forum, discord.ForumChannel):
        try:
            thread = await interaction.guild.fetch_channel(user_link["thread_id"])
        except Exception:
            thread = await _find_user_forum_thread(interaction.guild, interaction.user.id)

    if isinstance(thread, discord.Thread):
        await thread.send(embed=embed)
    else:
        try:
            await interaction.user.send("Heads up: I could not find your forum thread to mirror the delivery log. Please /link again if needed.")
        except Exception:
            pass

    deliveries = load_json(DELIVERIES_FILE, [])
    deliveries.append({
        "user": interaction.user.id, "pickup": pickup, "items": items,
        "dropoff": dropoff, "tipped": tipped, "duration": duration,
        "customer": customer, "method": method, "proof": proof,
        "thread_id": (thread.id if isinstance(thread, discord.Thread) else None)
    })
    save_json(DELIVERIES_FILE, deliveries)
    audit_file("delivery_log", {"by": interaction.user.id, "data": deliveries[-1]})
    await audit_embed(interaction.guild, "Delivery Logged", [
        ("User", "{u} (`{id}`)".format(u=str(interaction.user), id=interaction.user.id), True),
        ("Pickup -> Dropoff", "{a} -> {b}".format(a=pickup, b=dropoff), False),
    ])
    await interaction.response.send_message("Delivery logged.", ephemeral=True)

# -----------------------------------------------------------------------------
# Delivery Request (claim & end with status, auto-thread)
# -----------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="delivery_request", description="Create a delivery request.")
async def delivery_request(interaction: Interaction,
                           in_game_name: str,
                           delivery_location: str,
                           restaurant: str,
                           items_food: str):
    if not has_any_role(interaction.user, [ROLE_DELIVERY_REQUEST_ALLOWED]):
        return await interaction.response.send_message("No permission.", ephemeral=True)

    embed = Embed(title="Delivery Request", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="In-Game Name", value=in_game_name, inline=True)
    embed.add_field(name="Location", value=delivery_location, inline=True)
    embed.add_field(name="Restaurant", value=restaurant, inline=True)
    embed.add_field(name="Items/Food", value=items_food, inline=False)
    embed.add_field(name="Status", value="Unclaimed", inline=False)

    class DeliveryReqView(ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.claimed_by: Optional[int] = None

        @ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="drv_claim")
        async def claim(self, inter: Interaction, b: ui.Button):
            if not has_any_role(inter.user, [ROLE_DELIVERY_REQUEST_PING]):
                return await inter.response.send_message("Only delivery team can claim.", ephemeral=True)
            if self.claimed_by:
                return await inter.response.send_message("Already claimed.", ephemeral=True)
            self.claimed_by = inter.user.id
            for f in embed.fields:
                if f.name == "Status":
                    embed.remove_field(embed.fields.index(f))
                    break
            embed.add_field(name="Status", value="Ongoing - Claimed by <@{id}>".format(id=self.claimed_by), inline=False)
            b.disabled = True
            await inter.message.edit(embed=embed, view=self)
            await inter.response.send_message("Claimed.", ephemeral=True)

        @ui.button(label="End Delivery", style=discord.ButtonStyle.danger, custom_id="drv_end")
        async def end(self, inter: Interaction, b: ui.Button):
            if not has_any_role(inter.user, [ROLE_DELIVERY_REQUEST_PING]):
                return await inter.response.send_message("Only delivery team can end.", ephemeral=True)
            for f in embed.fields:
                if f.name == "Status":
                    embed.remove_field(embed.fields.index(f))
                    break
            who = "<@{id}>".format(id=self.claimed_by) if self.claimed_by else inter.user.mention
            embed.add_field(name="Status", value="Ended - Last handled by {w}".format(w=who), inline=False)
            for c in self.children:
                if isinstance(c, ui.Button):
                    c.disabled = True
            await inter.message.edit(embed=embed, view=self)
            await inter.response.send_message("Marked as Ended.", ephemeral=True)

    chan = interaction.guild.get_channel(CHAN_DELIVERY_REQUESTS)
    if not isinstance(chan, discord.TextChannel):
        return await interaction.response.send_message("Configured delivery-request channel not found.", ephemeral=True)

    msg = await chan.send(content="<@&{rid}>".format(rid=ROLE_DELIVERY_REQUEST_PING),
                          embed=embed, view=DeliveryReqView(),
                          allowed_mentions=discord.AllowedMentions(roles=True))
    try:
        await msg.create_thread(name="Discuss: {ign} @ {loc}".format(ign=in_game_name, loc=delivery_location))
    except Exception:
        pass

    await interaction.response.send_message("Delivery request posted.", ephemeral=True)

# -----------------------------------------------------------------------------
# Incidents (no ping)
# -----------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="log_incident", description="Log an incident (no ping).")
async def log_incident(interaction: Interaction, location: str, incident_type: str, reason: str):
    if not has_any_role(interaction.user, [ROLE_INCIDENT_ANY]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    embed = Embed(title="Incident Log", color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Location", value=location, inline=False)
    embed.add_field(name="Type", value=incident_type, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    chan = client.get_channel(CHAN_INCIDENT)
    await chan.send(embed=embed)
    audit_file("incident_log", {"by": interaction.user.id, "loc": location})
    await audit_embed(interaction.guild, "Incident Logged", [
        ("User", "{u} (`{id}`)".format(u=str(interaction.user), id=interaction.user.id), True),
        ("Type@Location", "{t} @ {l}".format(t=incident_type, l=location), False),
    ])
    await interaction.response.send_message("Incident logged.", ephemeral=True)

# -----------------------------------------------------------------------------
# Promotions / Infractions
# -----------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="promote", description="Promote an employee")
async def promote(interaction: Interaction, employee: discord.Member, old_rank: str, new_rank: str, reason: str, notes: str):
    if not has_any_role(interaction.user, [ROLE_PROMOTE]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    embed = Embed(title="Promotion", color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Employee", value=employee.mention, inline=False)
    embed.add_field(name="Old Rank", value=old_rank, inline=True)
    embed.add_field(name="New Rank", value=new_rank, inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Notes", value=notes, inline=False)
    chan = client.get_channel(CHAN_PROMOTE)
    await chan.send(content=employee.mention, embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
    audit_file("promote", {"by": interaction.user.id, "emp": employee.id, "new": new_rank})
    await audit_embed(interaction.guild, "Promotion Logged", [
        ("Employee", "{u} (`{id}`)".format(u=str(employee), id=employee.id), True),
        ("New Rank", new_rank, True),
    ])
    await interaction.response.send_message("Promotion logged.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="infraction", description="Infract an employee")
async def infraction(interaction: Interaction, employee: discord.Member, reason: str, infraction_type: str, proof: str, notes: str, appealable: str):
    if not has_any_role(interaction.user, [ROLE_INFRACT]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    embed = Embed(title="Infraction", color=discord.Color.dark_red(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Employee", value=employee.mention, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Type", value=infraction_type, inline=True)
    embed.add_field(name="Proof", value=proof, inline=True)
    embed.add_field(name="Notes", value=notes, inline=False)
    embed.add_field(name="Appealable", value=appealable, inline=True)
    chan = client.get_channel(CHAN_INFRACT)
    await chan.send(content=employee.mention, embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
    audit_file("infraction", {"by": interaction.user.id, "emp": employee.id, "type": infraction_type})
    await audit_embed(interaction.guild, "Infraction Logged", [
        ("Employee", "{u} (`{id}`)".format(u=str(employee), id=employee.id), True),
        ("Type", infraction_type, True),
    ])
    await interaction.response.send_message("Infraction logged.", ephemeral=True)

# -----------------------------------------------------------------------------
# Driver Blacklist logging only
# -----------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="driver_blacklist", description="Log a driver blacklist (staff only).")
async def driver_blacklist(interaction: Interaction, user: discord.Member, reason: str):
    if not has_any_role(interaction.user, [ROLE_TICKET_SHR]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    ch = interaction.guild.get_channel(CHAN_BLACKLIST_LOG)
    emb = Embed(title="Driver Blacklisted", color=discord.Color.dark_red(), timestamp=datetime.now(timezone.utc))
    emb.add_field(name="User", value="{u} (`{id}`)".format(u=str(user), id=user.id), inline=False)
    emb.add_field(name="Reason", value=reason, inline=False)
    emb.set_footer(text="By {m} ({id})".format(m=str(interaction.user), id=interaction.user.id))
    if isinstance(ch, discord.TextChannel):
        await ch.send(embed=emb)
    await interaction.response.send_message("Driver blacklist logged.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="driver_unblacklist", description="Revoke a driver blacklist (staff only).")
async def driver_unblacklist(interaction: Interaction, user: discord.Member, note: str):
    if not has_any_role(interaction.user, [ROLE_TICKET_SHR]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    ch = interaction.guild.get_channel(CHAN_BLACKLIST_LOG)
    emb = Embed(title="Driver Blacklist Revoked", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    emb.add_field(name="User", value="{u} (`{id}`)".format(u=str(user), id=user.id), inline=False)
    emb.add_field(name="Note", value=note, inline=False)
    emb.set_footer(text="By {m} ({id})".format(m=str(interaction.user), id=interaction.user.id))
    if isinstance(ch, discord.TextChannel):
        await ch.send(embed=emb)
    await interaction.response.send_message("Driver blacklist revoked (logged).", ephemeral=True)

# -----------------------------------------------------------------------------
# Permission / Resignation / Suggest (disabled)
# -----------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="permission_request", description="Command disabled.")
async def permission_request(interaction: Interaction, permission: str, reason: str, signed: str):
    return await interaction.response.send_message("This command is currently disabled.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="resignation_request", description="Command disabled.")
async def resignation_request(interaction: Interaction, division: str, note: str, ping: str):
    return await interaction.response.send_message("This command is currently disabled.", ephemeral=True)

@client.tree.command(guild=GUILD_OBJ, name="suggest", description="Command disabled.")
async def suggest(interaction: Interaction, suggestion: str, details: str):
    return await interaction.response.send_message("This command is currently disabled.", ephemeral=True)

# -----------------------------------------------------------------------------
# Host Deployment (host-role only; no ping)
# -----------------------------------------------------------------------------
@client.tree.command(guild=GUILD_OBJ, name="host_deployment", description="Host a deployment (host-role only).")
async def host_deployment(interaction: Interaction, reason: str, location: str, votes: str):
    if not has_any_role(interaction.user, [ROLE_DEPLOYMENT_CAN_USE]):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    embed = Embed(title="Deployment Hosted", color=discord.Color.purple(), timestamp=datetime.now(timezone.utc))
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
            if not has_any_role(inter.user, [ROLE_DEPLOYMENT_PING]):
                return await inter.response.send_message("Only employees can vote.", ephemeral=True)
            self.downvoters.discard(inter.user.id)
            self.upvoters.add(inter.user.id)
            await inter.response.send_message("Upvoted.", ephemeral=True)

        @ui.button(label="Downvote", style=discord.ButtonStyle.danger, custom_id="dep_down")
        async def down(self, inter: Interaction, b: ui.Button):
            if not has_any_role(inter.user, [ROLE_DEPLOYMENT_PING]):
                return await inter.response.send_message("Only employees can vote.", ephemeral=True)
            self.upvoters.discard(inter.user.id)
            self.downvoters.add(inter.user.id)
            await inter.response.send_message("Downvoted.", ephemeral=True)

        @ui.button(label="List Voters", style=discord.ButtonStyle.secondary, custom_id="dep_list")
        async def lst(self, inter: Interaction, b: ui.Button):
            if not has_any_role(inter.user, [ROLE_DEPLOYMENT_PING]):
                return await inter.response.send_message("Only employees can view.", ephemeral=True)
            up_list = ", ".join("<@{i}>".format(i=i) for i in self.upvoters) or "None"
            down_list = ", ".join("<@{i}>".format(i=i) for i in self.downvoters) or "None"
            emb = Embed(title="Deployment Votes", color=discord.Color.blurple())
            emb.add_field(name="Upvotes", value=up_list, inline=False)
            emb.add_field(name="Downvotes", value=down_list, inline=False)
            await inter.response.send_message(embed=emb, ephemeral=True)

    chan = client.get_channel(CHAN_DEPLOYMENT)
    await chan.send(embed=embed, view=DeployVoteView())
    audit_file("host_deploy", {"by": interaction.user.id, "reason": reason})
    await audit_embed(interaction.guild, "Deployment Hosted", [
        ("Host", "{u} (`{id}`)".format(u=str(interaction.user), id=interaction.user.id), True),
        ("Location", location, True),
    ])
    await interaction.response.send_message("Deployment hosted.", ephemeral=True)

# -----------------------------------------------------------------------------
# /sync (staff-only, 5 minute cooldown)
# -----------------------------------------------------------------------------
_last_sync_by_user: dict[int, datetime] = {}

@client.tree.command(name="sync", description="Force sync slash commands (staff only, 5 min cooldown)")
async def sync_cmd(interaction: Interaction):
    if not has_any_role(interaction.user, [STAFF_ROLE_ID]):
        return await interaction.response.send_message("You do not have permission to use this.", ephemeral=True)
    now = datetime.utcnow()
    last = _last_sync_by_user.get(interaction.user.id)
    if last and (now - last) < timedelta(minutes=5):
        remain = 300 - int((now - last).total_seconds())
        return await interaction.response.send_message("On cooldown. Try again in about {s}s.".format(s=remain), ephemeral=True)
    _last_sync_by_user[interaction.user.id] = now
    try:
        if GUILD_OBJ:
            cmds = await client.tree.sync(guild=GUILD_OBJ)
        else:
            cmds = await client.tree.sync()
        await interaction.response.send_message("Synced {n} commands.".format(n=len(cmds)), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message("Sync failed: `{}`".format(e), ephemeral=True)

# -----------------------------------------------------------------------------
# Events
# -----------------------------------------------------------------------------
@client.event
async def on_ready():
    print("Bot ready as {u}".format(u=str(client.user)))
    try:
        synced = await client.tree.sync(guild=GUILD_OBJ) if GUILD_OBJ else await client.tree.sync()
        print("Synced {n} slash commands.".format(n=len(synced)))
    except Exception as e:
        print("Sync failed:", e)

# -----------------------------------------------------------------------------
# Tiny aiohttp web server for Render (binds $PORT)
# -----------------------------------------------------------------------------
WEB_RUNNER = None
WEB_SITE = None

async def _health(request):
    return web.Response(text="ok")

async def start_web_server():
    global WEB_RUNNER, WEB_SITE
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)

    port_str = os.getenv("PORT") or "10000"
    port = int(port_str)

    WEB_RUNNER = web.AppRunner(app)
    await WEB_RUNNER.setup()
    WEB_SITE = web.TCPSite(WEB_RUNNER, "0.0.0.0", port)
    await WEB_SITE.start()
    print("[web] listening on :{p} (PORT env: {env})".format(p=port, env=os.getenv("PORT")))

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
async def main():
    asyncio.create_task(start_web_server())
    await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())


