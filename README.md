# Wordle Roast Bot

A Discord bot that posts a daily AI-generated roast of your group's Wordle scores. Listens for a "Here are yesterday's results" message, pulls both the text and any attached grid images, and sends them to Claude for a 3–6 sentence roast with a fun-animal-fact sign-off.

Built for a small, persistent friend group that shares Wordle scores in one channel. Designed to run 24/7 on cheap hardware — a Raspberry Pi, a home server, a small VPS, or anything else that can keep a Python process alive.

This repo is the source for the **Wordle Roast Bot** Discord application. If you want to run your own instance against your own group, fork this and follow the setup below — you'll register your own Discord application and use your own token, not this one. You'll want to be running, and actively using, the official [Wordle bot](https://discord.com/discovery/applications/1211781489931452447) already. 

## Features

- **Daily roast** of whoever played Wordle, triggered by the group's existing results message
- **Grid image analysis** — Claude sees the colored squares, not just the score, so it can call out collapses and clutch saves
- **Per-player persistent notes** (`Wordle Edit: Alice roast her harder when she loses streaks`)
- **Score history** — rolling 60 days of scores so the bot can catch trends, streaks, and personal bests
- **Roast archive** — look up any past roast by puzzle date (`Wordle Roast: April 10, 2026`)
- **One-shot temp notes** for targeted shots on a specific day (`Wordle Temp Roast: Bob - give him hell about missing yesterday`)
- **Name aliases** so stats survive nickname changes
- **Animal request + no-repeat animal sign-offs** — rolling 60-day animal history prevents reuse
- **Nightly reminder** at 11:00 PM Eastern — generic nudge to play before midnight. The timezone is hardcoded to UTC-4 (EDT), so during US standard time (roughly November–March) it will fire at 10:00 PM local instead of 11:00. Swap the `EDT = timezone(...)` line at the top of [bot.py](bot.py) for `ZoneInfo("America/New_York")` if you want it to auto-switch, or change it to your own zone.
- **Admin-only system prompt dump** (obscure, env-configurable command string)

## Requirements

- Python 3.10+
- A Discord bot application with a token (see below)
- An Anthropic API key (https://console.anthropic.com)
- A Discord server you can add the bot to, with at least one channel where Wordle scores get posted

## Setup

### 1. Clone and install

```bash
git clone https://github.com/zachvier/wordle-bot.git
cd wordle-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Create a Discord bot

1. Go to https://discord.com/developers/applications and create a new application.
2. Under **Bot**, click **Reset Token** and copy the token — you'll put it in `.env` in a moment. Never commit it.
3. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**. Without this the bot cannot read message text and will appear online but silent.
4. Under **OAuth2 → URL Generator**, select scopes `bot` and `applications.commands`, and bot permissions `Read Messages/View Channels` and `Send Messages`. Visit the generated URL to add the bot to your server.

### 3. Find your channel and admin IDs

In Discord, enable Developer Mode (**Settings → Advanced → Developer Mode**). Then:

- Right-click the channel where Wordle results are posted → **Copy Channel ID**
- Your admin username is the Discord **display name** of whoever should be able to run privileged commands (like wiping all player notes). The match is a case-insensitive substring, so `alice` will match `Alice {fast}`.

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | What it is |
|---|---|
| `DISCORD_TOKEN` | Your bot token from step 2 |
| `ANTHROPIC_API_KEY` | Your key from console.anthropic.com |
| `WORDLE_CHANNEL_IDS` | Comma-separated channel IDs the bot should watch |
| `REMINDER_CHANNEL_ID` | Channel ID for the nightly 11 PM reminder (usually same as above) |
| `ADMIN_USERNAME` | Substring of the Discord display name allowed to run privileged commands |
| `ADMIN_SECRET_CMD` | Exact string that dumps the current system prompt — keep it obscure |

### 5. Run it

```bash
python bot.py
```

You should see `Wordle Roast Bot is online as <BotName>` in the logs. Post `Wordle Help` in a monitored channel to see the command list.

## Running it as a service (Linux / systemd)

A sample unit file is included at `wordle-roast-bot.service`. To install:

```bash
# Edit the unit file and replace YOUR_USERNAME with your actual Linux username
# and adjust the WorkingDirectory / ExecStart paths to match your install location.
sudo cp wordle-roast-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wordle-roast-bot
sudo systemctl start wordle-roast-bot
sudo systemctl status wordle-roast-bot
```

Follow logs with:

```bash
journalctl -u wordle-roast-bot -f
```

## How the bot triggers

- **Real roast:** any message in a watched channel starting with `Here are yesterday's results`. Fires at most once per day (guard is in-memory; resets on restart).
- **Test roast:** send `ROAST TEST` in a watched channel. Bypasses the once-per-day guard and does NOT write to history files. Use this for development.
- **Nightly reminder:** fires automatically at 11:00 PM ET to `REMINDER_CHANNEL_ID`.

## Commands

All commands are typed as plain messages in a watched channel (not slash commands). Use plain names, **not** Discord @mentions — @mentions get converted to raw IDs which breaks name matching.

| Command | What it does |
|---|---|
| `Wordle Edit: PlayerName note here` | Add/update a persistent note about that player |
| `Wordle Temp Roast: PlayerName - instruction` | One-shot note consumed by the next real roast |
| `Wordle Temp Roast: clear PlayerName` | Remove a pending temp roast note |
| `Wordle Notes: PlayerName` | Show the current notes for a player |
| `Wordle Forget: PlayerName` | Wipe all notes for a player |
| `Wordle Forget: all` | Wipe all player notes (admin only) |
| `Wordle Stats: PlayerName` | Show score history, average, best/worst for a player |
| `Wordle Roast: April 10, 2026` | Look up an archived roast by **puzzle date** |
| `Wordle Alias: CurrentName OldName` | Link an old name to a current one so stats combine |
| `Wordle Aliases` | List all aliases |
| `Wordle Map: 123456789012345678 DisplayName` | Manually map a Discord user ID to a display name |
| `Wordle Animal Request: octopus` | Request the animal for the next roast's sign-off |
| `Wordle Help` | Show the command list |

## Data files (created at runtime)

All runtime data lives next to `bot.py` and is gitignored. Delete any of these to reset that piece of state.

| File | Purpose |
|---|---|
| `player_notes.json` | Persistent per-player roast notes |
| `roast_history.json` | Last 3 roasts (for callbacks / avoiding repeats) |
| `score_history.json` | Up to 60 days of scores per player |
| `animal_history.json` | Rolling 60-day list of animals used in sign-offs |
| `roast_archive.json` | 60-day archive of full roast text |
| `aliases.json` | Old name → current name mappings |
| `user_map.json` | Discord user ID → display name overrides |
| `animal_request.json` | Single pending animal request for the next roast |
| `temp_roasts.json` | One-shot per-player notes consumed by the next roast |

**Date convention:** every `date` field in the JSON files is the **puzzle date** — the day the Wordle was actually played, which is one day earlier than when the roast runs. The bot derives this as `today - timedelta(days=1)` because the results message says "Here are yesterday's results." Keep this convention for any new dated logic.

## Costs

Using Claude Sonnet 4.6 at the configured token limits (400 for roasts, 150 for reminders), real-world daily spend for a small group averages around **$0.02/day** (roast + reminder + incidental command traffic). A $5 Anthropic credit comfortably covers roughly 8 months of daily use for one group.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Bot shows online but doesn't respond | **Message Content Intent** not enabled in the Discord Developer Portal |
| `DISCORD_TOKEN is not set` on startup | `.env` missing or not in the same directory as `bot.py` |
| Roast never fires | Your results message must start with `Here are yesterday's results` and be posted in a channel whose ID is in `WORDLE_CHANNEL_IDS` |
| Roast fires twice after restart | Expected — the "already roasted today" guard lives in memory and resets on restart |
| `Wordle Edit` doesn't find the player | Don't use @mentions — use the plain display name |
| systemd: `status=217/USER` | The `User=` line in the unit file doesn't match an existing Linux user |
| systemd: `status=203/EXEC` | `~` isn't expanded in systemd — use absolute paths like `/home/youruser/wordle-bot/venv/bin/python` |

## Security notes

- `.env` is gitignored. Never commit it.
- All runtime JSON files are gitignored — they contain your group's display names, notes, and scores.
- The `ADMIN_SECRET_CMD` is not a real authentication layer — it's just an obscure string. Anyone in the Discord whose display name contains `ADMIN_USERNAME` can trigger it. Don't rely on it for anything sensitive.
- The bot only reads messages in the channels listed in `WORDLE_CHANNEL_IDS` and only needs read/send permissions in those channels. Grant it nothing more.

## License

MIT. Do whatever you want with it.
