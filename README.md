# DEIMOS Discord Bot

A Python `discord.py` all-in-one server management bot. Commands only; no dashboard and no marketplace.

## Features
- Persistent ticket panel with private ticket channels, staff claim/close buttons, and text transcripts
- Custom embeds and announcement embeds
- Moderation: ban, kick, timeout, purge, lock, unlock
- Role add/remove
- Welcome messages and basic server logs
- Basic invite-link AutoMod
- Server information and help command
- SQLite settings database

## 1. Create the Discord application
1. Open https://discord.com/developers/applications and create an application named **DEIMOS**.
2. Open **Bot** and create/reset the bot token. Keep it private.
3. Under **Privileged Gateway Intents**, enable **Server Members Intent** and **Message Content Intent**.
4. Open **OAuth2 → URL Generator**. Select `bot` and `applications.commands`.
5. Select permissions: View Channels, Send Messages, Embed Links, Read Message History, Manage Channels, Manage Messages, Manage Roles, Kick Members, Ban Members, Moderate Members, and Attach Files.
6. Use the generated URL to invite DEIMOS to your server.

## 2. Deploy on Railway
1. Create a GitHub repository and upload the files in this project.
2. In Railway, create a new project → Deploy from GitHub Repo.
3. Open the service → **Variables** and add `DISCORD_TOKEN` with your bot token.
4. Add a persistent Railway Volume and mount it at `/app/data` (recommended). Then set `DATABASE_PATH=/app/data/deimos.sqlite3`.
5. Deploy. Check the deployment logs for `Deimos online`.

Keep the token secret. If it is ever exposed, reset it in the Discord Developer Portal.

## 3. Configure it in Discord
Run these slash commands as an administrator:
1. `/setup_logs` and choose your staff-log channel.
2. `/setup_welcome` and choose your welcome channel.
3. `/setup_tickets` and choose a ticket category, panel channel, and optional support role.
4. `/ticket_panel` and choose the channel where you want the panel posted.

Other commands:
- `/embed` — send a custom embed to a channel
- `/announce` — send an announcement embed
- `/ban`, `/kick`, `/timeout`, `/purge`
- `/lock`, `/unlock`
- `/role_add`, `/role_remove`
- `/automod` — toggle invite-link blocking
- `/serverinfo`, `/help`

## Notes
- Discord slash commands can take a little time to appear after first startup.
- The bot's highest role must be above roles it manages.
- The bot needs **Manage Channels** to create and close tickets.
- Transcripts are sent to the configured log channel. Set `/setup_logs` to enable them.
- SQLite data is stored at `DATABASE_PATH`. Use a Railway Volume so settings survive redeploys.
