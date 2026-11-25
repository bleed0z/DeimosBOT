import os
import io
import sqlite3
import logging
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DATABASE_PATH", "deimos.sqlite3")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("deimos")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("""
CREATE TABLE IF NOT EXISTS settings (
    guild_id INTEGER PRIMARY KEY,
    ticket_category_id INTEGER,
    ticket_panel_channel_id INTEGER,
    support_role_id INTEGER,
    log_channel_id INTEGER,
    welcome_channel_id INTEGER,
    automod_enabled INTEGER DEFAULT 0,
    block_invites INTEGER DEFAULT 0
)
""")
db.commit()


def ensure_guild(guild_id: int):
    db.execute("INSERT OR IGNORE INTO settings (guild_id) VALUES (?)", (guild_id,))
    db.commit()


def get_settings(guild_id: int):
    ensure_guild(guild_id)
    return db.execute("SELECT * FROM settings WHERE guild_id = ?", (guild_id,)).fetchone()


def set_setting(guild_id: int, key: str, value):
    allowed = {
        "ticket_category_id", "ticket_panel_channel_id", "support_role_id",
        "log_channel_id", "welcome_channel_id", "automod_enabled", "block_invites"
    }
    if key not in allowed:
        raise ValueError("Invalid setting")
    ensure_guild(guild_id)
    db.execute(f"UPDATE settings SET {key} = ? WHERE guild_id = ?", (value, guild_id))
    db.commit()


def make_embed(title: str, description: str = "", color: discord.Color = discord.Color.blurple()):
    e = discord.Embed(title=title[:256], description=description[:4000], color=color)
    e.set_footer(text="DEIMOS • Server Management")
    return e


async def send_log(guild: discord.Guild, title: str, description: str):
    settings = get_settings(guild.id)
    channel_id = settings["log_channel_id"]
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel:
        try:
            await channel.send(embed=make_embed(title, description, discord.Color.dark_grey()))
        except discord.Forbidden:
            log.warning("Missing permission to send logs in guild %s", guild.id)


def admin_only():
    return app_commands.checks.has_permissions(administrator=True)


class TicketControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, custom_id="deimos:ticket_claim", emoji="🙋")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("This button only works in a server ticket.", ephemeral=True)
        if not interaction.user.guild_permissions.manage_channels:
            return await interaction.response.send_message("You need Manage Channels to claim tickets.", ephemeral=True)
        await interaction.channel.edit(topic=f"Claimed by {interaction.user} ({interaction.user.id})")
        await interaction.response.send_message(embed=make_embed("Ticket claimed", f"{interaction.user.mention} is handling this ticket.", discord.Color.green()))

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="deimos:ticket_close", emoji="🔒")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("This button only works in a server ticket.", ephemeral=True)
        if not interaction.user.guild_permissions.manage_channels:
            topic = interaction.channel.topic or ""
            if str(interaction.user.id) not in topic and interaction.user.id not in [m.id for m in interaction.channel.members]:
                return await interaction.response.send_message("You can't close this ticket.", ephemeral=True)
        await interaction.response.send_message("Closing ticket and preparing transcript…", ephemeral=True)
        await create_transcript(interaction.channel)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send("I couldn't delete this channel. Check my Manage Channels permission.", ephemeral=True)


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open a ticket", style=discord.ButtonStyle.success, custom_id="deimos:ticket_open", emoji="🎫")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Tickets can only be opened in a server.", ephemeral=True)

        settings = get_settings(guild.id)
        category = guild.get_channel(settings["ticket_category_id"]) if settings["ticket_category_id"] else None
        support_role = guild.get_role(settings["support_role_id"]) if settings["support_role_id"] else None

        existing = discord.utils.get(guild.text_channels, topic=f"Ticket owner: {interaction.user.id}")
        if existing:
            return await interaction.response.send_message(f"You already have a ticket: {existing.mention}", ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        safe_name = "".join(c.lower() if c.isalnum() else "-" for c in interaction.user.name).strip("-")[:18] or "member"
        try:
            channel = await guild.create_text_channel(
                name=f"ticket-{safe_name}",
                category=category if isinstance(category, discord.CategoryChannel) else None,
                overwrites=overwrites,
                topic=f"Ticket owner: {interaction.user.id}",
                reason=f"Ticket opened by {interaction.user}"
            )
            await channel.send(
                content=f"{interaction.user.mention}" + (f" {support_role.mention}" if support_role else ""),
                embed=make_embed("Support ticket", "Thanks for opening a ticket. Explain what you need help with and staff will be with you soon."),
                view=TicketControls()
            )
            await interaction.response.send_message(f"Your ticket is ready: {channel.mention}", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I need Manage Channels and permission to view/send messages in the ticket category.", ephemeral=True)


async def create_transcript(channel: discord.TextChannel):
    settings = get_settings(channel.guild.id)
    log_channel = channel.guild.get_channel(settings["log_channel_id"]) if settings["log_channel_id"] else None
    if not log_channel:
        return
    lines = [f"Transcript for #{channel.name}", f"Channel ID: {channel.id}", ""]
    try:
        async for msg in channel.history(limit=500, oldest_first=True):
            stamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            content = msg.clean_content.replace("\n", " ")
            if msg.attachments:
                content += " " + " ".join(a.url for a in msg.attachments)
            lines.append(f"[{stamp}] {msg.author} ({msg.author.id}): {content}")
        data = "\n".join(lines).encode("utf-8")
        file = discord.File(io.BytesIO(data), filename=f"{channel.name}-transcript.txt")
        await log_channel.send(embed=make_embed("Ticket transcript", f"Transcript for **#{channel.name}**"), file=file)
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Could not create transcript")


@bot.event
async def on_ready():
    bot.add_view(TicketPanel())
    bot.add_view(TicketControls())
    try:
        synced = await bot.tree.sync()
        log.info("Synced %s slash commands", len(synced))
    except Exception:
        log.exception("Slash command sync failed")
    log.info("Deimos online as %s", bot.user)


@bot.event
async def on_member_join(member: discord.Member):
    settings = get_settings(member.guild.id)
    channel = member.guild.get_channel(settings["welcome_channel_id"]) if settings["welcome_channel_id"] else None
    if channel:
        await channel.send(embed=make_embed("Welcome!", f"Welcome {member.mention} to **{member.guild.name}**!\nYou're member **#{member.guild.member_count}**.", discord.Color.green()))
    await send_log(member.guild, "Member joined", f"{member.mention} (`{member.id}`) joined the server.")


@bot.event
async def on_member_remove(member: discord.Member):
    await send_log(member.guild, "Member left", f"**{member}** (`{member.id}`) left the server.")


@bot.event
async def on_message_delete(message: discord.Message):
    if message.guild and not message.author.bot:
        content = message.content[:1500] if message.content else "*No text content*"
        await send_log(message.guild, "Message deleted", f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}\n**Content:** {content}")


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.guild and not before.author.bot and before.content != after.content:
        await send_log(before.guild, "Message edited", f"**Author:** {before.author.mention}\n**Channel:** {before.channel.mention}\n**Before:** {before.content[:700]}\n**After:** {after.content[:700]}")


@bot.event
async def on_message(message: discord.Message):
    if message.guild and not message.author.bot:
        settings = get_settings(message.guild.id)
        if settings["automod_enabled"]:
            blocked = False
            reason = ""
            if settings["block_invites"] and ("discord.gg/" in message.content.lower() or "discord.com/invite/" in message.content.lower()):
                blocked, reason = True, "Discord invite links aren't allowed here."
            if blocked:
                try:
                    await message.delete()
                    await message.channel.send(f"{message.author.mention} {reason}", delete_after=5)
                    await send_log(message.guild, "AutoMod action", f"Deleted a message from {message.author.mention} in {message.channel.mention}.")
                except discord.Forbidden:
                    pass
    await bot.process_commands(message)


@bot.tree.command(name="setup_tickets", description="Configure ticket category, panel channel and support role.")
@app_commands.describe(category="Category where tickets should be created", panel_channel="Channel where the ticket panel will be posted", support_role="Optional staff role")
@admin_only()
async def setup_tickets(interaction: discord.Interaction, category: discord.CategoryChannel, panel_channel: discord.TextChannel, support_role: Optional[discord.Role] = None):
    set_setting(interaction.guild_id, "ticket_category_id", category.id)
    set_setting(interaction.guild_id, "ticket_panel_channel_id", panel_channel.id)
    set_setting(interaction.guild_id, "support_role_id", support_role.id if support_role else None)
    await interaction.response.send_message(embed=make_embed("Ticket system configured", f"Category: {category.mention}\nPanel channel: {panel_channel.mention}\nSupport role: {support_role.mention if support_role else 'Not set'}", discord.Color.green()), ephemeral=True)


@bot.tree.command(name="ticket_panel", description="Post the ticket panel in a channel.")
@app_commands.describe(channel="Where to post the panel")
@admin_only()
async def ticket_panel(interaction: discord.Interaction, channel: discord.TextChannel):
    await channel.send(embed=make_embed("Need help?", "Press the button below to open a private support ticket.", discord.Color.blurple()), view=TicketPanel())
    await interaction.response.send_message(f"Ticket panel posted in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="setup_logs", description="Set the server log channel.")
@admin_only()
async def setup_logs(interaction: discord.Interaction, channel: discord.TextChannel):
    set_setting(interaction.guild_id, "log_channel_id", channel.id)
    await interaction.response.send_message(f"Logs will be sent to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="setup_welcome", description="Set the welcome channel.")
@admin_only()
async def setup_welcome(interaction: discord.Interaction, channel: discord.TextChannel):
    set_setting(interaction.guild_id, "welcome_channel_id", channel.id)
    await interaction.response.send_message(f"Welcome messages will be sent to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="embed", description="Send a custom embed.")
@app_commands.describe(channel="Where to send it", title="Embed title", description="Embed description", color="Hex color, e.g. #5865F2")
@admin_only()
async def custom_embed(interaction: discord.Interaction, channel: discord.TextChannel, title: str, description: str, color: str = "#5865F2"):
    try:
        value = int(color.strip().replace("#", ""), 16)
        if value < 0 or value > 0xFFFFFF:
            raise ValueError
        embed = make_embed(title, description, discord.Color(value))
        await channel.send(embed=embed)
        await interaction.response.send_message(f"Embed sent to {channel.mention}.", ephemeral=True)
    except ValueError:
        await interaction.response.send_message("Invalid color. Use a hex color like `#5865F2`.", ephemeral=True)


@bot.tree.command(name="announce", description="Send an announcement embed.")
@admin_only()
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    await channel.send(embed=make_embed("📢 Announcement", message, discord.Color.gold()))
    await interaction.response.send_message("Announcement sent.", ephemeral=True)


@bot.tree.command(name="ban", description="Ban a member.")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await member.ban(reason=f"{reason} | By {interaction.user}")
    await interaction.response.send_message(embed=make_embed("Member banned", f"{member} was banned.\n**Reason:** {reason}", discord.Color.red()))
    await send_log(interaction.guild, "Member banned", f"**Member:** {member} (`{member.id}`)\n**By:** {interaction.user.mention}\n**Reason:** {reason}")


@bot.tree.command(name="kick", description="Kick a member.")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await member.kick(reason=f"{reason} | By {interaction.user}")
    await interaction.response.send_message(embed=make_embed("Member kicked", f"{member} was kicked.\n**Reason:** {reason}", discord.Color.orange()))
    await send_log(interaction.guild, "Member kicked", f"**Member:** {member} (`{member.id}`)\n**By:** {interaction.user.mention}\n**Reason:** {reason}")


@bot.tree.command(name="timeout", description="Timeout a member.")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(minutes="Timeout duration in minutes (1–40320)")
async def timeout_member(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided"):
    await member.timeout(timedelta(minutes=minutes), reason=f"{reason} | By {interaction.user}")
    await interaction.response.send_message(embed=make_embed("Member timed out", f"{member.mention} was timed out for **{minutes} minutes**.\n**Reason:** {reason}", discord.Color.orange()))
    await send_log(interaction.guild, "Member timed out", f"**Member:** {member.mention}\n**Duration:** {minutes} minutes\n**By:** {interaction.user.mention}\n**Reason:** {reason}")


@bot.tree.command(name="purge", description="Delete recent messages.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(amount="Number of messages to delete (1–100)")
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Deleted **{len(deleted)}** messages.", ephemeral=True)


@bot.tree.command(name="lock", description="Lock the current channel.")
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
    await interaction.response.send_message(embed=make_embed("Channel locked", f"{interaction.channel.mention} has been locked.", discord.Color.red()))


@bot.tree.command(name="unlock", description="Unlock the current channel.")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=None)
    await interaction.response.send_message(embed=make_embed("Channel unlocked", f"{interaction.channel.mention} has been unlocked.", discord.Color.green()))


@bot.tree.command(name="role_add", description="Give a member a role.")
@app_commands.checks.has_permissions(manage_roles=True)
async def role_add(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    await member.add_roles(role, reason=f"By {interaction.user}")
    await interaction.response.send_message(f"Added {role.mention} to {member.mention}.", ephemeral=True)


@bot.tree.command(name="role_remove", description="Remove a role from a member.")
@app_commands.checks.has_permissions(manage_roles=True)
async def role_remove(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    await member.remove_roles(role, reason=f"By {interaction.user}")
    await interaction.response.send_message(f"Removed {role.mention} from {member.mention}.", ephemeral=True)


@bot.tree.command(name="automod", description="Configure basic invite-link AutoMod.")
@app_commands.describe(enabled="Turn invite blocking on or off")
@admin_only()
async def automod(interaction: discord.Interaction, enabled: bool):
    set_setting(interaction.guild_id, "automod_enabled", int(enabled))
    set_setting(interaction.guild_id, "block_invites", int(enabled))
    await interaction.response.send_message(f"Invite-link AutoMod is now **{'enabled' if enabled else 'disabled'}**.", ephemeral=True)


@bot.tree.command(name="serverinfo", description="Show server information.")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    e = make_embed(f"{guild.name} — Server Info", f"**Members:** {guild.member_count}\n**Owner:** {guild.owner.mention if guild.owner else 'Unknown'}\n**Created:** <t:{int(guild.created_at.timestamp())}:D>\n**Channels:** {len(guild.channels)}\n**Roles:** {len(guild.roles)}")
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="help", description="Show Deimos commands.")
async def help_command(interaction: discord.Interaction):
    e = make_embed("DEIMOS Commands", (
        "**Setup:** `/setup_tickets`, `/ticket_panel`, `/setup_logs`, `/setup_welcome`\n"
        "**Community:** `/embed`, `/announce`, `/serverinfo`\n"
        "**Moderation:** `/ban`, `/kick`, `/timeout`, `/purge`, `/lock`, `/unlock`\n"
        "**Roles:** `/role_add`, `/role_remove`\n"
        "**AutoMod:** `/automod`\n"
        "**Tickets:** Use the ticket panel buttons to open, claim, and close tickets."
    ))
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You don't have permission to use that command."
    elif isinstance(error, app_commands.BotMissingPermissions):
        message = "I don't have the Discord permissions needed for that action."
    elif isinstance(error, app_commands.CheckFailure):
        message = "You can't use that command here or with your current permissions."
    else:
        log.exception("Slash command error", exc_info=error)
        message = "Something went wrong. Check the bot logs and permissions."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable. Add it in Railway Variables.")

bot.run(TOKEN)
