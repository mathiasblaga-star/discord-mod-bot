# Discord Moderation Bot

A fully automated Discord moderation bot built with `discord.py`. Designed to run 24/7 with no human intervention — admins only review flagged cases.

## Features

- **Spam detection** — rate-based flooding, duplicate-message spam, mass mentions / `@everyone`
- **Slur & hate-speech filter** — custom configurable list, leetspeak / fuzzy matching, escalating sanctions (mute → kick → permanent ban)
- **Server-nuke protection** — detects mass channel deletes/creates, mass role creates, and mass bans via the audit log; auto-strips roles, locks the actor down, and triggers server-wide lockdown
- **Slash commands** — `/history`, `/unban`, `/unmute`, `/lockdown on|off`
- **Persistent state** — SQLite (via `aiosqlite`) for infractions, active mutes, lockdown state; mutes survive restarts
- **Auto-recovery** — reconnects automatically; supervisor loop restarts on crash; periodic cleanup tasks
- **Keep-alive endpoint** — Flask web server for free-tier hosts that idle inactive workers

## Project layout

```
discord-mod-bot/
├── bot.py                      # entry point + supervisor loop
├── config.py                   # all tunables (thresholds, IDs, slur list)
├── database.py                 # aiosqlite layer
├── keep_alive.py               # Flask uptime ping
├── cogs/
│   ├── spam.py                 # rate / duplicate / mass-mention spam
│   ├── slurs.py                # slur detection + escalation
│   ├── nuke_protection.py      # audit-log nuke detection
│   └── admin.py                # /history /unban /unmute /lockdown + unmute task
├── utils/
│   ├── fuzzy_match.py          # leetspeak normalisation + fuzzy compare
│   ├── embeds.py               # embed builders
│   └── views.py                # Review / Undo Ban buttons
├── requirements.txt
├── Procfile                    # for Railway / Heroku-style platforms
├── runtime.txt                 # Python version pin
├── .env.example
└── .gitignore
```

## 1. Create the bot in the Discord Developer Portal

1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. Open the **Bot** tab → **Add Bot**.
3. Under **Privileged Gateway Intents**, enable:
   - **Presence Intent** *(optional)*
   - **Server Members Intent** *(required)*
   - **Message Content Intent** *(required)*
4. Copy the **Bot Token** — this is your `DISCORD_TOKEN`.

### Permissions

The simplest setup is **Administrator**, which grants everything the bot needs (timeouts, kicks, bans, role management, channel overrides for lockdown, audit log viewing).

If you prefer granular permissions, the bot needs:

- View Channels
- Send Messages
- Manage Messages
- Read Message History
- Moderate Members (timeouts)
- Kick Members
- Ban Members
- Manage Roles  *(must be higher in the role hierarchy than any role it manages)*
- Manage Channels  *(for lockdown)*
- View Audit Log

### Invite URL

Use the OAuth2 → URL Generator with scopes `bot` + `applications.commands`. Pick your permission set and open the generated URL to invite the bot.

## 2. Set up `.env` (non-sensitive IDs only)

Copy `.env.example` to `.env` and fill in the values:

```
GUILD_ID=123456789012345678               # right-click your server → Copy Server ID (Developer Mode)
MOD_LOG_CHANNEL_ID=...                    # for Medium-severity logs
MOD_ALERTS_CHANNEL_ID=...                 # for Severe-severity alerts (admin pings)
ADMIN_ROLE_ID=...                         # role to ping on Severe alerts
MUTED_ROLE_ID=                            # optional fallback if Discord timeouts fail
```

> **Do not put `DISCORD_TOKEN` in `.env`.** The bot token is encrypted at rest — see step 3.

To copy IDs: enable **Settings → Advanced → Developer Mode** in Discord, then right-click any server / channel / role → **Copy ID**.

## 3. Encrypt the bot token

The bot token is encrypted with a master password using Fernet (AES-128 + HMAC) over a PBKDF2-SHA256-derived key (480 000 iterations, 16-byte random salt). The plaintext token is never written to disk.

```bash
pip install -r requirements.txt
python setup_token.py
```

You'll be prompted for:

1. **Your Discord bot token** (input is hidden)
2. **A master password** (twice — you'll need this every time the bot starts, unless you opt into the OS keyring)
3. Optionally, **save the master password to the OS keyring** (macOS Keychain, Windows Credential Locker, or Linux Secret Service). If you opt in, the bot starts non-interactively. Otherwise it prompts for the password on every launch.

This writes `token.enc` in the project root. `.gitignore` already excludes it. If you had `DISCORD_TOKEN=` in `.env`, delete that line now — it is no longer used.

To rotate the token or password, just re-run `python setup_token.py`. To forget a keyring entry: `keyring del discord-mod-bot owner`.

## 4. Configure thresholds and slur list

Open `config.py` — every tunable lives there:

- spam thresholds (`SPAM_MESSAGE_COUNT`, `SPAM_TIME_WINDOW`, `DUPLICATE_MESSAGE_LIMIT`, `MASS_MENTION_LIMIT`)
- mute durations (`SPAM_MUTE_DURATION`, `SLUR_MUTE_DURATION`)
- nuke-detection limits (`NUKE_*_LIMIT`, `NUKE_TIME_WINDOW`)
- fuzzy matching threshold (`FUZZY_THRESHOLD`)
- **`SLUR_LIST`** — populate this with the slurs you want filtered. Regular profanity (`fuck`, `shit`, `damn`, etc.) should NOT be added — only targeted slurs and hate speech. The fuzzy matcher handles leetspeak (`l33t` → `leet`, `n!gger`-style obfuscation, etc.) so you only need the canonical form.

## 5. Run locally

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python setup_token.py     # one-time — encrypts token to token.enc
python bot.py
```

```powershell
# Windows
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python setup_token.py
python bot.py
```

If you opted into the OS keyring during `setup_token.py`, `python bot.py` starts non-interactively. Otherwise the bot prompts for the master password on every launch. Once it logs in, slash commands sync to the configured guild on `on_ready` and appear within seconds.

## 6. Deploy 24/7 on Railway (free)

1. Push this folder to a GitHub repo (do not commit `.env` — `.gitignore` excludes it).
2. Go to <https://railway.app> → **New Project → Deploy from GitHub repo**.
3. Pick your repo. Railway auto-detects Python.
4. In the **Variables** tab, paste the contents of your `.env` (one variable per row).
5. Railway runs the `Procfile` (`worker: python bot.py`).
6. The Flask `keep_alive` server binds to `$PORT` so platforms like Replit can ping it from an uptime monitor (e.g. UptimeRobot) to keep the worker hot. On Railway this isn't strictly needed, but it doesn't hurt.

### Replit alternative

1. Import the repo into Replit.
2. Add the `.env` variables in the Secrets tab.
3. Run. Use UptimeRobot to ping the Replit web URL every 5 minutes.

## How the moderation logic works

### Severity tiers

| Tier   | Where it goes                | Admin signal           |
| ------ | ---------------------------- | ---------------------- |
| Low    | DB only                      | silent                 |
| Medium | `MOD_LOG_CHANNEL_ID` embed   | "Mark Reviewed" button |
| Severe | `MOD_ALERTS_CHANNEL_ID`      | pings `ADMIN_ROLE_ID`, includes "Undo Ban" button on bans |

### Spam

| Trigger                                         | Severity | Action                          |
| ----------------------------------------------- | -------- | ------------------------------- |
| 5+ messages within 5 seconds                    | Low      | delete + warn                   |
| Same message repeated 3+ times                  | Medium   | mute 10 min                     |
| `@everyone` / `@here` or 5+ unique user pings   | Medium   | kick                            |

### Slurs (escalating)

| Offence count | Action                |
| ------------- | --------------------- |
| 1st           | mute 1 hour (Medium)  |
| 2nd           | kick (Severe)         |
| 3rd           | permanent ban (Severe) |

History is read from `infractions.offence_type = 'slur'` so it survives restarts.

### Nuke protection

Listens to `on_audit_log_entry_create` (requires the **Moderation** intent — already configured). If a single user hits any of these in a 10-second window:

- 3+ channel deletes
- 5+ channel creates
- 5+ role creates
- 4+ bans

…the bot strips all their roles (those below its top role), times them out for 28 days (Discord's max), logs a Severe infraction with the full event timeline, pings the admin role in `MOD_ALERTS_CHANNEL_ID`, and triggers server-wide lockdown.

Lift lockdown with `/lockdown off`.

## Slash commands

| Command                        | Who         | Effect                                                |
| ------------------------------ | ----------- | ----------------------------------------------------- |
| `/history user:@user`          | Admins only | Last 20 infractions for the user                      |
| `/unban user_id:<id>`          | Admins only | Lifts ban by user ID                                  |
| `/unmute member:@user`         | Admins only | Removes timeout + muted role + DB entry               |
| `/lockdown state:on\|off`      | Admins only | Toggles `send_messages` for `@everyone` on every text channel |

"Admins only" = `Administrator` permission OR the role configured as `ADMIN_ROLE_ID`.

## Persistence guarantees

- **Infractions** — every action (Low, Medium, Severe) is written to `infractions`. `/history` reads from here.
- **Active mutes** — `add_mute()` records `expires_at`. The `unmute_task` loop runs every 30s and lifts expired mutes whether the bot was online when they expired or not.
- **Lockdown state** — `lockdown_state` table stores per-guild lockdown status so you can detect it after a restart (use `get_lockdown(guild_id)` if you want to extend the bot to re-apply it on startup).

## Resilience

- `discord.py` reconnects on transient disconnects automatically (`bot.start(..., reconnect=True)`).
- The `bot.py` supervisor loop catches uncaught exceptions and restarts the bot after 10 seconds.
- All Discord API calls are wrapped in `try/except discord.HTTPException` so a single failed permission action never takes the bot down.

## Limits and notes

- The bot can only act on members **below** its highest role. Place its role at the top of the hierarchy.
- Discord timeouts max out at 28 days — that's the cap used for nuke neutralisation.
- `on_audit_log_entry_create` fires for actions taken by anyone with elevated perms, including the bot itself; the cog filters its own actions out.
- The slur list is empty by default — the filter is inert until you populate `SLUR_LIST` in `config.py`.
