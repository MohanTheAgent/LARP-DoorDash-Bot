# -*- coding: utf-8 -*-
# bot.py
# Python 3.11+ | discord.py 2.3+ | pip install -U discord.py python-dotenv

import json
import os
from datetime import datetime, timezone
from typing import Dict, Set, Any, List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ========= Load env =========
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_STR = os.getenv("GUILD_ID")
if not DISCORD_TOKEN or not GUILD_ID_STR:
    raise RuntimeError("Missing DISCORD_TOKEN or GUILD_ID in .env")

GUILD_ID = int(GUILD_ID_STR)
GUILD_OBJ = discord.Object(id=GUILD_ID)

# ========= IDs (UPDATED PER YOUR REQUEST) =========
# New role that can use: /link, /log_delivery, /log_incident, /permission_request, /resignation_request
ROLE_GENERAL_ACCESS      = 1420838579780714586

# New role that can use: /host_deployment
ROLE_DEPLOYMENT_HOST     = 1420836448692736122

# New role that can use: /infraction and /promote
ROLE_HR_MANAGEMENT       = 1420836510185554074

# Existing roles still referenced
ROLE_CUSTOMER_SERVICE    = 1420721072197861446  # used for welcome + optional incident ping

# Channels (unchanged)
CHANNEL_DEPLOYMENTS      = 1420778159879753800
CHANNEL_DELIVERIES       = 1420773516512460840
CHANNEL_INCIDENTS        = 1420773317949784124
FORUM_DELIVERIES         = 1420780863632965763

CHANNEL_RESIGNATIONS     = 1420810835508330557
CHANNEL_PERMISSIONS      = 1420811205114859732
CHANNEL_PROMOTIONS       = 1420819381788741653
CHANNEL_INFRACTIONS      = 1420820006530191511
CHANNEL_CUSTOMER_WELCOME = 1420784105448280136

# Pings
PING_ROLE_INCIDENT       = 1420721072197861446          # optional ping on /log_incident
PING_ROLE_DEPLOY_NOTIFY  = 1420838579780714586          # ping on /host_deployment top content
PERMISSION_PING_ROLE     = 1420721073170677783          # small-text ping in permission request

# Embed image for all major embeds (as requested)
EMBED_IMAGE = "https://cdn.discordapp.com/attachments/1420749680538816553/1420834953335275710/togif.gif"

# ========= Persistence =========
LINKS_FILE   = "links.json"   # stores { "<user_id>": <thread_id> }

def _ensure_file(path: str, default_text: Optional[str] = None):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            if default_text is not None:
                f.write(default_text)

def load_links() -> Dict[str, int]:
    _ensure_file(LINKS_FILE, "{}")
    try:
        with open(LINKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_links(data: Dict[str, int]) -> None:
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

links_map: Dict[str, int] = load_links()

# ========= Helpers =========
def user_has_any_role_ids(member: discord.Member, role_ids: Set[int]) -> bool:
    return any(r.id in role_ids for r in getattr(member, "roles", []))

def role_check(required_roles: Set[int]):
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message("This command is not available in this server.", ephemeral=True)
            return False
        member = interaction.user
        if not isinstance(member, discord.Member):
            member = interaction.guild.get_member(interaction.user.id)  # type: ignore
        if not isinstance(member, discord.Member) or not user_has_any_role_ids(member, required_roles):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

# ========= Bot =========
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
client = commands.Bot(command_prefix="!", intents=intents)

# ========= Voting View (buttons use plain text labels to avoid '??') =========
vote_state: Dict[int, Dict[str, Set[int]]] = {}
def ensure_state(message_id: int):
    if message_id not in vote_state:
        vote_state[message_id] = {"up": set(), "down": set()}

class DeploymentVoteView(discord.ui.View):
    def __init__(self, required_role_for_buttons: int, *, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.allowed_role_id = required_role_for_buttons

    async def _guard(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            member = interaction.guild.get_member(interaction.user.id)  # type: ignore
        if not isinstance(member, discord.Member) or (self.allowed_role_id not in [r.id for r in member.roles]):
            await interaction.response.send_message("You are not allowed to use these buttons.", ephemeral=True)
            return False
        return True

    async def _update_embed_counts(self, interaction: discord.Interaction):
        msg = interaction.message
        s = vote_state.get(msg.id, {"up": set(), "down": set()})
        up_count = len(s["up"])
        down_count = len(s["down"])
        base = msg.embeds[0]
        updated = base.copy()
        updated.set_footer(text=f"Upvotes: {up_count} | Downvotes: {down_count}")
        await msg.edit(embeds=[updated], view=self)

    @discord.ui.button(label="Upvote", style=discord.ButtonStyle.success)
    async def upvote(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        s = vote_state.setdefault(interaction.message.id, {"up": set(), "down": set()})
        uid = interaction.user.id
        s["down"].discard(uid)
        if uid in s["up"]:
            s["up"].discard(uid)
            await interaction.response.send_message("Removed upvote.", ephemeral=True)
        else:
            s["up"].add(uid)
            await interaction.response.send_message("Upvoted.", ephemeral=True)
        await self._update_embed_counts(interaction)

    @discord.ui.button(label="Downvote", style=discord.ButtonStyle.danger)
    async def downvote(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        s = vote_state.setdefault(interaction.message.id, {"up": set(), "down": set()})
        uid = interaction.user.id
        s["up"].discard(uid)
        if uid in s["down"]:
            s["down"].discard(uid)
            await interaction.response.send_message("Removed downvote.", ephemeral=True)
        else:
            s["down"].add(uid)
            await interaction.response.send_message("Downvoted.", ephemeral=True)
        await self._update_embed_counts(interaction)

    @discord.ui.button(label="List Voters", style=discord.ButtonStyle.secondary)
    async def list_voters(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        s = vote_state.setdefault(interaction.message.id, {"up": set(), "down": set()})
        def fmt(users: Set[int]) -> str:
            return "N/A" if not users else "\n".join(f"- <@{u}>" for u in users)
        embed = discord.Embed(title="Deployment Votes", color=discord.Color.blurple())
        embed.add_field(name="Upvotes", value=fmt(s["up"]), inline=True)
        embed.add_field(name="Downvotes", value=fmt(s["down"]), inline=True)
        embed.set_image(url=EMBED_IMAGE)
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ========= Ready =========
@client.event
async def on_ready():
    await client.tree.sync(guild=GUILD_OBJ)
    print(f"Bot ready as {client.user}")

# ========= Auto Welcome when gaining Customer Service role =========
@client.event
async def on_member_update(before: discord.Member, after: discord.Member):
    before_roles = {r.id for r in before.roles}
    after_roles = {r.id for r in after.roles}
    if ROLE_CUSTOMER_SERVICE not in before_roles and ROLE_CUSTOMER_SERVICE in after_roles:
        chan = after.guild.get_channel(CHANNEL_CUSTOMER_WELCOME)
        if chan:
            embed = discord.Embed(
                title="Welcome to Customer Services!",
                description=(
                    "You have been handpicked by the Customer Service Commander.\n\n"
                    "Step 1: Take a short test in the Commander's DMs.\n"
                    "Step 2: Complete a ticket test (simulate handling a customer).\n"
                    "Step 3: Begin a 1-week trial period.\n\n"
                    "We are excited to see you grow and succeed!"
                ),
                color=discord.Color.blurple()
            )
            embed.set_footer(text="DoorDash LA:RP - Customer Service Division")
            embed.set_image(url=EMBED_IMAGE)
            await chan.send(content=f"{after.mention}", embed=embed)

# ========= Utility: find user's forum thread in the forum channel =========
async def _find_user_forum_threads(guild: discord.Guild, user_id: int) -> List[discord.Thread]:
    forum = guild.get_channel(FORUM_DELIVERIES) or await guild.fetch_channel(FORUM_DELIVERIES)
    if not isinstance(forum, discord.ForumChannel):
        return []
    results: List[discord.Thread] = []

    # Active threads across guild
    try:
        active = await guild.active_threads()
        for t in active:
            if t.parent_id == FORUM_DELIVERIES and getattr(t, "owner_id", None) == user_id:
                results.append(t)
    except Exception:
        pass

    # Threads already in forum cache
    try:
        for t in getattr(forum, "threads", []):
            if getattr(t, "owner_id", None) == user_id:
                results.append(t)
    except Exception:
        pass

    # Archived threads
    try:
        async for t in forum.archived_threads(limit=200):  # type: ignore[attr-defined]
            if getattr(t, "owner_id", None) == user_id:
                results.append(t)
    except Exception:
        try:
            archived = await forum.fetch_archived_threads(limit=200)  # type: ignore[attr-defined]
            for t in archived.threads:
                if getattr(t, "owner_id", None) == user_id:
                    results.append(t)
        except Exception:
            pass

    # Dedup
    dedup: Dict[int, discord.Thread] = {t.id: t for t in results}
    return list(dedup.values())

# ========= Slash Commands =========

# /log_incident (Role: GENERAL_ACCESS)
@client.tree.command(name="log_incident", description="Log an incident.", guild=GUILD_OBJ)
@role_check({ROLE_GENERAL_ACCESS})
@app_commands.describe(
    location="Location of the incident",
    incident_type="Type of incident (Crash, Spillage, etc.)",
    reason="Reason for the incident",
    ping="Ping the designated incident role"
)
async def log_incident(
    interaction: discord.Interaction,
    location: str,
    incident_type: str,
    reason: str,
    ping: bool = False
):
    embed = discord.Embed(title="Incident Logged", color=discord.Color.red())
    embed.add_field(name="Location", value=location, inline=False)
    embed.add_field(name="Type", value=incident_type, inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.set_image(url=EMBED_IMAGE)

    chan = interaction.client.get_channel(CHANNEL_INCIDENTS) or await interaction.client.fetch_channel(CHANNEL_INCIDENTS)
    await interaction.response.send_message("Incident submitted.", ephemeral=True)
    await chan.send(
        content=(f"<@&{PING_ROLE_INCIDENT}>" if ping else None),
        embed=embed,
        allowed_mentions=discord.AllowedMentions(roles=True) if ping else discord.AllowedMentions.none()
    )

# /host_deployment (Role: DEPLOYMENT_HOST)
@client.tree.command(name="host_deployment", description="Host a deployment with voting.", guild=GUILD_OBJ)
@role_check({ROLE_DEPLOYMENT_HOST})
@app_commands.describe(
    reason="Why is this deployment being hosted?",
    location="Where is the deployment?",
    votes="Optional target/notes for votes"
)
async def host_deployment(
    interaction: discord.Interaction,
    reason: str,
    location: str,
    votes: Optional[int] = None
):
    chan = interaction.client.get_channel(CHANNEL_DEPLOYMENTS) or await interaction.client.fetch_channel(CHANNEL_DEPLOYMENTS)
    embed = discord.Embed(title="Deployment Hosted", description=reason, color=discord.Color.green())
    embed.add_field(name="Location", value=location, inline=True)
    if votes is not None:
        embed.add_field(name="Votes", value=str(votes), inline=True)
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text="Upvotes: 0 | Downvotes: 0")
    embed.set_image(url=EMBED_IMAGE)

    # Buttons usable by the GENERAL_ACCESS role
    view = DeploymentVoteView(required_role_for_buttons=ROLE_GENERAL_ACCESS)

    await interaction.response.send_message("Deployment created.", ephemeral=True)
    await chan.send(
        content=f"<@&{PING_ROLE_DEPLOY_NOTIFY}>",
        embed=embed,
        view=view,
        allowed_mentions=discord.AllowedMentions(roles=True)
    )

# /link (Role: GENERAL_ACCESS)
@client.tree.command(name="link", description="Auto-link your personal deliveries forum post.", guild=GUILD_OBJ)
@role_check({ROLE_GENERAL_ACCESS})
async def link(interaction: discord.Interaction):
    user_id = interaction.user.id
    threads = await _find_user_forum_threads(interaction.guild, user_id)  # type: ignore
    if not threads:
        await interaction.response.send_message(
            f"I could not find a forum post created by you in <#{FORUM_DELIVERIES}>. Create one first, then run /link again.",
            ephemeral=True
        )
        return
    threads.sort(key=lambda t: (t.created_at or datetime.fromtimestamp(0, tz=timezone.utc), t.id), reverse=True)
    chosen = threads[0]
    links_map[str(user_id)] = chosen.id
    save_links(links_map)
    await interaction.response.send_message(f"Linked your deliveries thread: <#{chosen.id}>.", ephemeral=True)

# /log_delivery (Role: GENERAL_ACCESS) requires /link first
@client.tree.command(name="log_delivery", description="Log a delivery.", guild=GUILD_OBJ)
@role_check({ROLE_GENERAL_ACCESS})
@app_commands.describe(
    pickup="Place of pickup",
    items="Items delivered",
    dropoff="Place of dropoff",
    amount_tipped="Amount tipped",
    duration="Time taken",
    customer="Customer (in-game user)",
    method="How was it requested (In-Game, DC, etc.)"
)
async def log_delivery(
    interaction: discord.Interaction,
    pickup: str,
    items: str,
    dropoff: str,
    amount_tipped: str,
    duration: str,
    customer: str,
    method: str
):
    thread_id = links_map.get(str(interaction.user.id))
    if not thread_id:
        await interaction.response.send_message("You must link your forum first with /link.", ephemeral=True)
        return

    embed = discord.Embed(title="Delivery Logged", color=discord.Color.blurple())
    embed.add_field(name="Pickup", value=pickup, inline=True)
    embed.add_field(name="Dropoff", value=dropoff, inline=True)
    embed.add_field(name="Items", value=items, inline=False)
    embed.add_field(name="Amount Tipped", value=amount_tipped, inline=True)
    embed.add_field(name="Duration", value=duration, inline=True)
    embed.add_field(name="Customer", value=customer, inline=True)
    embed.add_field(name="Requested Via", value=method, inline=True)
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.set_image(url=EMBED_IMAGE)

    deliveries_chan = interaction.client.get_channel(CHANNEL_DELIVERIES) or await interaction.client.fetch_channel(CHANNEL_DELIVERIES)
    await deliveries_chan.send(embed=embed)

    try:
        thread = interaction.client.get_channel(thread_id) or await interaction.client.fetch_channel(thread_id)
        if isinstance(thread, discord.Thread) and thread.parent_id == FORUM_DELIVERIES:
            await thread.send(embed=embed)
    except Exception:
        pass

    await interaction.response.send_message("Delivery logged.", ephemeral=True)

# /resignation_request (Role: GENERAL_ACCESS)
@client.tree.command(name="resignation_request", description="Submit a resignation request.", guild=GUILD_OBJ)
@role_check({ROLE_GENERAL_ACCESS})
@app_commands.describe(
    division="Division you are resigning from",
    final_note="Your final note",
    ping="Who to ping (mention text)"
)
async def resignation_request(interaction: discord.Interaction, division: str, final_note: str, ping: str):
    embed = discord.Embed(title="Resignation Request", color=discord.Color.red())
    embed.add_field(name="Division", value=division, inline=False)
    embed.add_field(name="Final Note", value=final_note, inline=False)
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.set_image(url=EMBED_IMAGE)

    chan = interaction.client.get_channel(CHANNEL_RESIGNATIONS) or await interaction.client.fetch_channel(CHANNEL_RESIGNATIONS)
    await chan.send(content=ping, embed=embed, allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=True))
    await interaction.response.send_message("Resignation submitted.", ephemeral=True)

# /permission_request (Role: GENERAL_ACCESS)
@client.tree.command(name="permission_request", description="Submit a permission request.", guild=GUILD_OBJ)
@role_check({ROLE_GENERAL_ACCESS})
@app_commands.describe(
    permission="Permission you are requesting",
    reason="Reason for the request"
)
async def permission_request(interaction: discord.Interaction, permission: str, reason: str):
    embed = discord.Embed(
        title="<:DoorDash:1420463324713320469> Permission Request <:DoorDash:1420463324713320469>",
        color=discord.Color.orange()
    )
    embed.add_field(name="Permission", value=permission, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Signed", value=str(interaction.user), inline=False)
    embed.set_image(url=EMBED_IMAGE)

    mention_line = f"-# <@&{PERMISSION_PING_ROLE}>"

    chan = interaction.client.get_channel(CHANNEL_PERMISSIONS) or await interaction.client.fetch_channel(CHANNEL_PERMISSIONS)
    await chan.send(content=mention_line, embed=embed, allowed_mentions=discord.AllowedMentions(roles=True))
    await interaction.response.send_message("Permission request submitted.", ephemeral=True)

# /promote (Role: HR_MANAGEMENT)
@client.tree.command(name="promote", description="Promote an employee.", guild=GUILD_OBJ)
@role_check({ROLE_HR_MANAGEMENT})
@app_commands.describe(
    employee="User being promoted",
    old_rank="Old rank",
    new_rank="New rank",
    reason="Reason for promotion",
    notes="Additional notes"
)
async def promote(
    interaction: discord.Interaction,
    employee: discord.Member,
    old_rank: str,
    new_rank: str,
    reason: str,
    notes: str
):
    embed = discord.Embed(title="Promotion", color=discord.Color.gold())
    embed.add_field(name="Employee", value=employee.mention, inline=False)
    embed.add_field(name="Old Rank", value=old_rank, inline=True)
    embed.add_field(name="New Rank", value=new_rank, inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Notes", value=notes, inline=False)
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.set_image(url=EMBED_IMAGE)

    chan = interaction.client.get_channel(CHANNEL_PROMOTIONS) or await interaction.client.fetch_channel(CHANNEL_PROMOTIONS)
    await chan.send(content=f"{employee.mention}", embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
    await interaction.response.send_message("Promotion submitted.", ephemeral=True)

# /infraction (Role: HR_MANAGEMENT)
INFRACTION_CHOICES = [
    app_commands.Choice(name="Notice", value="Notice"),
    app_commands.Choice(name="Warning", value="Warning"),
    app_commands.Choice(name="Strike", value="Strike"),
    app_commands.Choice(name="Demotion", value="Demotion"),
    app_commands.Choice(name="Suspension", value="Suspension"),
    app_commands.Choice(name="Termination", value="Termination"),
    app_commands.Choice(name="Blacklist", value="Blacklist"),
    app_commands.Choice(name="Termination + Blacklist", value="Termination + Blacklist"),
]

@client.tree.command(name="infraction", description="Infract an employee.", guild=GUILD_OBJ)
@role_check({ROLE_HR_MANAGEMENT})
@app_commands.describe(
    employee="Employee being infracted",
    reason="Reason for infraction",
    infraction_type="Type of infraction",
    proof="Proof (link or text)",
    notes="Extra notes",
    appealable="Yes or No"
)
@app_commands.choices(infraction_type=INFRACTION_CHOICES)
async def infraction(
    interaction: discord.Interaction,
    employee: discord.Member,
    reason: str,
    infraction_type: app_commands.Choice[str],
    proof: str,
    notes: str,
    appealable: str
):
    embed = discord.Embed(title="Infraction Issued", color=discord.Color.red())
    embed.add_field(name="Employee", value=employee.mention, inline=False)
    embed.add_field(name="Type", value=infraction_type.value, inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Proof", value=proof, inline=False)
    embed.add_field(name="Notes", value=notes, inline=False)
    embed.add_field(name="Appealable", value=appealable, inline=True)
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.set_image(url=EMBED_IMAGE)

    chan = interaction.client.get_channel(CHANNEL_INFRACTIONS) or await interaction.client.fetch_channel(CHANNEL_INFRACTIONS)
    await chan.send(content=f"{employee.mention}", embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
    await interaction.response.send_message("Infraction submitted.", ephemeral=True)

# ========= Run =========
client.run(DISCORD_TOKEN)
