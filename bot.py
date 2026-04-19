import discord
from discord.ext import tasks
import anthropic
import os
import json
import re
import base64
import logging
from datetime import date, datetime, time, timezone, timedelta
from dotenv import load_dotenv
import httpx

# Load environment variables from the .env file (your secret keys live there)
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Channel IDs to monitor. Set WORDLE_CHANNEL_IDS in your .env as a
# comma-separated list of channel IDs (the last segment of each channel URL).
# Example: WORDLE_CHANNEL_IDS=111122223333444455,666677778888999900
# Using a set so the membership check ("is this channel one of ours?") is fast.
_channel_ids_raw = os.getenv("WORDLE_CHANNEL_IDS", "")
TARGET_CHANNEL_IDS = {int(cid.strip()) for cid in _channel_ids_raw.split(",") if cid.strip().isdigit()}

# Path to the file that stores player notes on disk.
# This lives next to bot.py in the project folder.
NOTES_FILE = os.path.join(os.path.dirname(__file__), "player_notes.json")

# EDT is UTC-4. We define it here so the nightly reminder fires at 11:00 PM Eastern.
# Note: this does NOT auto-switch to EST in winter. If you want that, you'd need the
# `pytz` or `zoneinfo` library — but for a Wordle bot, being off by an hour in Nov-Mar
# is probably fine. If it matters, swap this for:
#   from zoneinfo import ZoneInfo
#   EDT = ZoneInfo("America/New_York")
EDT = timezone(timedelta(hours=-4))

# The channel where the nightly reminder should be sent.
# Set REMINDER_CHANNEL_ID in your .env to the channel ID you want reminders in.
REMINDER_CHANNEL_ID = int(os.getenv("REMINDER_CHANNEL_ID", "0"))

# Admin username — used to restrict certain commands (e.g. "Wordle Forget: all").
# Set ADMIN_USERNAME in your .env to the Discord display name of your admin.
# The check is case-insensitive and looks for this string anywhere in the display name.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").lower()

# Secret command to output the current system prompt in Discord.
# Set ADMIN_SECRET_CMD in your .env — keep it obscure so others don't stumble on it.
# Defaults to "SysPromptWH" if not set.
ADMIN_SECRET_CMD = os.getenv("ADMIN_SECRET_CMD", "SysPromptWH")

# Path to the file that stores recent roast history on disk.
# Keeps the last few roasts so Claude can make callbacks and reference streaks.
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "roast_history.json")
MAX_ROAST_HISTORY = 3  # how many past roasts to remember

# Path to the file that stores per-player score history on disk.
# Compact numeric data — just who scored what each day — for long-term trend analysis.
SCORE_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "score_history.json")
MAX_SCORE_HISTORY = 60  # how many days of scores to keep

# Path to the file that stores which animals have been used in sign-off facts.
# A rolling 60-day list of animal names so Claude doesn't repeat them.
ANIMAL_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "animal_history.json")
MAX_ANIMAL_HISTORY = 60  # how many days of animals to keep

# Path to the file that archives the full roast text indexed by date.
# Powers the "Wordle Roast: April 10, 2026" lookup command.
ROAST_ARCHIVE_FILE = os.path.join(os.path.dirname(__file__), "roast_archive.json")
MAX_ROAST_ARCHIVE = 60  # two months of daily roasts

# Path to the file that maps Discord user IDs to display names.
# Needed because Discord mentions arrive as raw <@123456789> strings — we
# rewrite those to @displayname before handing anything to Claude.
USER_MAP_FILE = os.path.join(os.path.dirname(__file__), "user_map.json")

# Path to the file that maps old player names to current names.
# Used by Wordle Stats to combine scores across name changes.
# Format: {"old_name": "current_name"} e.g. {"PlayerA{fast}": "PlayerA"}
ALIASES_FILE = os.path.join(os.path.dirname(__file__), "aliases.json")

# Path to the file that stores a single pending animal request for the next roast.
# Format: {"date": "2026-04-12", "animal": "octopus", "requester": "PlayerName"}
# Empty dict / missing file = no pending request. Cleared after the next real
# roast consumes it, or overridden via the Yes/No confirmation flow.
ANIMAL_REQUEST_FILE = os.path.join(os.path.dirname(__file__), "animal_request.json")

# Path to the file that stores one-shot temporary roast notes keyed by player.
# Format: {"PlayerName": "Give her a hard time about missing a day"}
# Consumed and wiped after the next real roast posts. Test roasts do not wipe.
TEMP_ROASTS_FILE = os.path.join(os.path.dirname(__file__), "temp_roasts.json")

# Set up logging so you can see what the bot is doing in the systemd journal
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# PLAYER NOTES — LOAD / SAVE
# player_notes is a dict like: {"@PlayerA": "roast harder, 110%", "@PlayerB": "humble"}
# It's loaded from disk at startup and saved every time it changes.
# ─────────────────────────────────────────────

def load_notes() -> dict:
    """Read player_notes.json from disk. Returns empty dict if file doesn't exist yet."""
    if os.path.exists(NOTES_FILE):
        with open(NOTES_FILE, "r") as f:
            return json.load(f)
    return {}

def save_notes(notes: dict):
    """Write the current notes dict to disk so they survive restarts."""
    with open(NOTES_FILE, "w") as f:
        json.dump(notes, f, indent=2)

# Load notes into memory when the bot starts
player_notes: dict = load_notes()


# ─────────────────────────────────────────────
# ROAST HISTORY — LOAD / SAVE
# roast_history is a list of dicts like:
#   [{"date": "2026-04-04", "scores": "4/6: @PlayerA ...", "roast": "PlayerA and PlayerB..."}]
# Newest entries are appended to the end. Trimmed to MAX_ROAST_HISTORY.
# ─────────────────────────────────────────────

def load_history() -> list:
    """Read roast_history.json from disk. Returns empty list if file doesn't exist yet."""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return []

def save_history(history: list):
    """Write the roast history list to disk, trimmed to the max."""
    # Only keep the most recent entries
    trimmed = history[-MAX_ROAST_HISTORY:]
    with open(HISTORY_FILE, "w") as f:
        json.dump(trimmed, f, indent=2)

# Load history into memory when the bot starts
roast_history: list = load_history()


# ─────────────────────────────────────────────
# SCORE HISTORY — LOAD / SAVE / PARSE
# score_history is a list of dicts like:
#   [{"date": "2026-04-04", "scores": {"PlayerA": 5, "PlayerB": 6, "PlayerC": 4}}]
# Stores up to MAX_SCORE_HISTORY days of compact numeric scores.
# ─────────────────────────────────────────────

def load_score_history() -> list:
    """Read score_history.json from disk. Returns empty list if file doesn't exist yet."""
    if os.path.exists(SCORE_HISTORY_FILE):
        with open(SCORE_HISTORY_FILE, "r") as f:
            return json.load(f)
    return []

def save_score_history(history: list):
    """Write the score history list to disk, trimmed to the max."""
    trimmed = history[-MAX_SCORE_HISTORY:]
    with open(SCORE_HISTORY_FILE, "w") as f:
        json.dump(trimmed, f, indent=2)

def parse_scores_from_text(text: str) -> dict:
    """Extract player scores from the Wordle results message text.

    Looks for lines like '4/6: @PlayerA @PlayerB' and returns a dict
    mapping player names (without the @) to their guess count as an int.
    For example: {"PlayerA": 4, "PlayerB": 4, "PlayerC": 5}

    Uses a regex to find the score pattern, then pulls @-mentioned names
    from the rest of that line. This runs in Python so the scores stored
    in history are guaranteed accurate — no LLM guessing involved.
    """
    scores = {}
    # Match lines like "4/6: @Name1 @Name2" — the crown emoji and other
    # prefixes are ignored by searching anywhere in each line.
    for line in text.split("\n"):
        # Find a score pattern like "3/6" or "6/6"
        score_match = re.search(r'(\d)/6', line)
        if not score_match:
            continue
        guess_count = int(score_match.group(1))
        # Find all @mentions on this line — handles names with special chars
        # like player{tag} by matching @ followed by non-whitespace characters.
        names = re.findall(r'@(\S+)', line)
        for name in names:
            scores[name] = guess_count
    return scores

def build_score_table(history: list) -> str:
    """Format score history as a compact readable table for the prompt.

    Produces something like:
      Date        | PlayerA | PlayerB | PlayerC
      2026-04-04  | 5       | 6       | 4
      2026-04-03  | 3       | -       | 4

    A dash means the player didn't play that day.
    """
    if not history:
        return ""

    # Collect all player names that have ever appeared, in a stable order.
    # We use dict.fromkeys to preserve insertion order while deduplicating.
    all_players = list(dict.fromkeys(
        name
        for entry in history
        for name in entry["scores"]
    ))

    # Build header row
    header = "Date        | " + " | ".join(f"{p:>12}" for p in all_players)
    # Build each data row
    rows = []
    for entry in history:
        cells = []
        for player in all_players:
            val = entry["scores"].get(player)
            cells.append(f"{val:>12}" if val is not None else f"{'-':>12}")
        rows.append(f"{entry['date']}  | " + " | ".join(cells))

    return header + "\n" + "\n".join(rows)

def build_player_stats(player: str, history: list, names: list[str] | None = None) -> str | None:
    # Build a stats summary for a single player from score_history.
    # 'player' is the display name shown in the output.
    # 'names' is a list of all names to search for (handles aliases).
    # If names is None, searches for just 'player'.
    if names is None:
        names = [player]
    player_entries = [
        (entry["date"], entry["scores"][n])
        for entry in history
        for n in names
        if n in entry["scores"]
    ]

    if not player_entries:
        return None

    scores = [s for _, s in player_entries]
    avg = sum(scores) / len(scores)
    best = min(scores)
    worst = max(scores)
    days_played = len(scores)
    total_days = len(history)

    # Show up to 20 most recent scores, oldest first, so you can read the trend left-to-right
    recent = scores[-20:]
    score_str = "  ".join(str(s) for s in recent)

    lines = [
        f"**{player}** — {days_played} of {total_days} days played",
        f"Scores (oldest → newest): {score_str}",
        f"Avg: {avg:.1f} | Best: {best}/6 | Worst: {worst}/6",
    ]
    return "\n".join(lines)


# Load score history into memory when the bot starts
score_history: list = load_score_history()


# ─────────────────────────────────────────────
# ANIMAL HISTORY — LOAD / SAVE / EXTRACT
# animal_history is a list of dicts like:
#   [{"date": "2026-04-04", "animal": "sea turtle"}]
# Stores up to MAX_ANIMAL_HISTORY days so Claude avoids repeats.
# ─────────────────────────────────────────────

def load_animal_history() -> list:
    """Read animal_history.json from disk. Returns empty list if file doesn't exist yet."""
    if os.path.exists(ANIMAL_HISTORY_FILE):
        with open(ANIMAL_HISTORY_FILE, "r") as f:
            return json.load(f)
    return []

def save_animal_history(history: list):
    """Write the animal history list to disk, trimmed to the max."""
    trimmed = history[-MAX_ANIMAL_HISTORY:]
    with open(ANIMAL_HISTORY_FILE, "w") as f:
        json.dump(trimmed, f, indent=2)

def extract_animal_from_roast(roast_text: str) -> str | None:
    """Extract the bolded animal name from the roast sign-off.

    Looks for **animal name** in the text — the bot prompt requires the animal
    to be bolded with double asterisks. Returns the animal name in lowercase,
    or None if no match is found.
    """
    match = re.search(r'\*\*(.+?)\*\*', roast_text)
    if match:
        return match.group(1).lower()
    return None

# Load animal history into memory when the bot starts
animal_history: list = load_animal_history()


# ─────────────────────────────────────────────
# ROAST ARCHIVE — LOAD / SAVE
# roast_archive is a dict like: {"2026-04-10": "PlayerA and PlayerB..."}
# Keyed by ISO date string. Powers the "Wordle Roast: April 10, 2026" command.
# ─────────────────────────────────────────────

def load_roast_archive() -> dict:
    """Read roast_archive.json from disk. Returns empty dict if file doesn't exist."""
    if os.path.exists(ROAST_ARCHIVE_FILE):
        with open(ROAST_ARCHIVE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_roast_archive(archive: dict):
    """Write roast archive to disk, trimmed to the most recent MAX_ROAST_ARCHIVE entries."""
    if len(archive) > MAX_ROAST_ARCHIVE:
        for old_date in sorted(archive.keys())[:len(archive) - MAX_ROAST_ARCHIVE]:
            del archive[old_date]
    with open(ROAST_ARCHIVE_FILE, "w") as f:
        json.dump(archive, f, indent=2)

# Load roast archive into memory when the bot starts
roast_archive: dict = load_roast_archive()


# ─────────────────────────────────────────────
# USER MAP — LOAD / SAVE / RESOLVE
# user_map is a dict like: {"453912146707742730": "PlayerA", ...}
# Keys are Discord user IDs (as strings, since JSON has no int keys).
# Values are the display names we want Claude to see.
# ─────────────────────────────────────────────

def load_user_map() -> dict:
    """Read user_map.json from disk. Returns empty dict if file doesn't exist yet."""
    if os.path.exists(USER_MAP_FILE):
        with open(USER_MAP_FILE, "r") as f:
            return json.load(f)
    return {}

def save_user_map(user_map: dict):
    """Write the current user map to disk so it survives restarts."""
    with open(USER_MAP_FILE, "w") as f:
        json.dump(user_map, f, indent=2)

def resolve_mentions(text: str, user_map: dict) -> str:
    """Replace every <@123456789> in text with @displayname from the map.

    Discord sends real mentions as raw ID strings like <@453912146707742730>
    in message.content. Claude has no way to map those IDs back to names, so
    we do it here with a regex substitution. Unknown IDs are replaced with
    @unknown-<short-id> so the roast still flows but the missing mapping is
    visible in the output and logs.
    """
    def _sub(match: re.Match) -> str:
        # The ! handles nickname mentions (<@!123>) which Discord used to send
        # for users with server nicknames. Modern Discord uses <@123> for both,
        # but we strip ! just in case.
        user_id = match.group(1)
        name = user_map.get(user_id)
        if name:
            return f"@{name}"
        # Fallback: keep something readable but obviously unresolved
        return f"@unknown-{user_id[-4:]}"
    return re.sub(r'<@!?(\d+)>', _sub, text)

# Load user map into memory when the bot starts
user_map: dict = load_user_map()


# ─────────────────────────────────────────────
# PLAYER ALIASES — LOAD / SAVE / RESOLVE
# aliases is a dict like: {"OldName": "NewName", "FormerName": "CurrentName"}
# Keys are old names, values are current/canonical names.
# Used by Wordle Stats to combine scores across name changes.
# ─────────────────────────────────────────────

def load_aliases() -> dict:
    if os.path.exists(ALIASES_FILE):
        with open(ALIASES_FILE, "r") as f:
            return json.load(f)
    return {}

def save_aliases(aliases: dict):
    with open(ALIASES_FILE, "w") as f:
        json.dump(aliases, f, indent=2)

def resolve_player_aliases(name: str, aliases: dict) -> tuple[str, list[str]]:
    # Given a player name, return (canonical_name, all_names_to_search).
    # If 'name' is a key in aliases, its value is the canonical name.
    # Otherwise, 'name' itself is treated as canonical.
    # all_names includes the canonical name plus every alias that points to it.
    canonical = aliases.get(name, name)
    all_names = [canonical]
    for alias, canon in aliases.items():
        if canon == canonical and alias != canonical:
            all_names.append(alias)
    return canonical, all_names

# Load aliases into memory when the bot starts
player_aliases: dict = load_aliases()


# ─────────────────────────────────────────────
# ANIMAL REQUEST — LOAD / SAVE / CLEAR
# Stores a single pending animal request that overrides the "no repeat" rule
# for the next real roast. Cleared after the roast consumes it.
#
# pending_override is in-memory only: maps a user's Discord ID to the animal
# they're proposing as an override. Populated when a second request arrives
# while another is already pending; consumed by `Wordle Animal Request: Yes/No`.
# Not persisted — if the bot restarts, the proposer just re-issues the command.
# ─────────────────────────────────────────────

def load_animal_request() -> dict:
    """Read animal_request.json from disk. Returns empty dict if no request is pending."""
    if os.path.exists(ANIMAL_REQUEST_FILE):
        with open(ANIMAL_REQUEST_FILE, "r") as f:
            return json.load(f)
    return {}

def save_animal_request(req: dict):
    """Write the current pending request to disk."""
    with open(ANIMAL_REQUEST_FILE, "w") as f:
        json.dump(req, f, indent=2)

def clear_animal_request():
    """Remove the animal_request.json file so no request is pending."""
    if os.path.exists(ANIMAL_REQUEST_FILE):
        os.remove(ANIMAL_REQUEST_FILE)

animal_request: dict = load_animal_request()
pending_override: dict = {}


# ─────────────────────────────────────────────
# TEMP ROASTS — LOAD / SAVE / CLEAR
# One-shot per-player instructions that get injected into the next real roast
# and wiped after it posts. Anyone can set them.
# ─────────────────────────────────────────────

def load_temp_roasts() -> dict:
    if os.path.exists(TEMP_ROASTS_FILE):
        with open(TEMP_ROASTS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_temp_roasts(notes: dict):
    with open(TEMP_ROASTS_FILE, "w") as f:
        json.dump(notes, f, indent=2)

def clear_temp_roasts():
    if os.path.exists(TEMP_ROASTS_FILE):
        os.remove(TEMP_ROASTS_FILE)

temp_roasts: dict = load_temp_roasts()


# ─────────────────────────────────────────────
# SYSTEM PROMPT BUILDER
# Assembles the base prompt + any saved player notes into one string.
# Called fresh on every roast so it always reflects the latest notes.
# ─────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """You are the official trash-talk analyst for the Wordle Warlords — a tight-knit Discord group competing daily at Wordle.

WHEN TO ACTIVATE:
Only produce a roast when the message contains the phrase "Here are yesterday's results". Ignore anything else.

READING THE RESULTS:
Results are posted by a bot each morning and look like:
  Your group is on a [X] day streak!
  Here are yesterday's results:
  3/6: @PlayerA
  4/6: @PlayerB @PlayerC
  5/6: @PlayerD
The format may change over time. Focus on extracting who played and how many guesses they needed. Lower number = better.

CRITICAL — PARSE BEFORE YOU ROAST:
Internally — without writing anything out — extract the exact score for every player
from the text. Build a mental list like:
  @PlayerA = 3/6
  @PlayerB = 4/6
  @PlayerC = 4/6
Do NOT include this parsing step in your output. Your entire output must be only the
roast — no preamble, no bullet lists, no "let me parse" section, no separators.
Double-check: if the text says "4/6: @PlayerB @PlayerC", that means BOTH PlayerB AND
PlayerC scored 4/6 — not that one is 4 and the other is something else. Every name
on the same line shares the same score.
Never guess or infer a player's score from the grid image — always use the text as
the source of truth for the number. The grid image is only for color pattern analysis
(green/yellow/gray), not for determining the score itself.
If your roast attributes a score to a player that contradicts the text, you have made
an error. Fix it before responding.
This applies to every sentence — including the closing hype line. Any relational claim
(e.g. "your partner scored X", "your rival beat you") must be verified against the
actual score text. Note: if two players are listed as each other's partners, they
cannot both have "their partner" be a third person — check before writing it.

READING THE GRID IMAGES:
You will also receive the Wordle grid image(s) attached to the results message.
Each player's grid shows their guesses as colored squares:
- Green = correct letter, correct position
- Yellow = correct letter, wrong position
- Gray/Dark = letter not in the word

MATCHING GRIDS TO PLAYERS:
The grids appear left-to-right in the image in the exact same order as players are
listed top-to-bottom in the text results. For example if the text reads:
  3/6: @Alice
  4/6: @Bob
  5/6: @Charlie
Then the leftmost grid = Alice, middle = Bob, rightmost = Charlie.
Use this ordering to confidently attribute each grid to the correct player.

Use the grids to go deeper than the score alone. For example:
- A 6/6 with green squares early on = had the information and still blew it
- A 5/6 with all gray until the last two rows = genuinely hard path, less embarrassing
- Back-to-back yellows with no follow-through = knows the letters, can't place them
Factor this context into the roast when it's damning or surprisingly redemptive.

PLAYERS:
Player names come from the @mentions in the results text — that is the source of truth.
Do not assign players recurring titles, roles, or personalities not established in their
player notes. All characterization must come from the notes or direct observation of
today's data — not invented lore or assumptions.
New players may appear — roast them equally, just without historical context.

USING RECENT HISTORY:
You may receive recent roast history below. Use it when something connects naturally:
- A player choking multiple days in a row = escalate the disappointment
- A player on a hot streak = acknowledge it (grudgingly)
- A callback to a previous roast's joke = chef's kiss
- Someone who was roasted yesterday and did the same thing today = gold
Don't force it. If nothing connects, just roast today's results fresh. Never repeat
a previous roast's jokes word-for-word — always find a new angle. Do not reference
the history section directly (e.g. don't say "as I said yesterday"). Just weave it in.

USING SCORE HISTORY:
You may also receive a table of each player's scores over the past weeks/months.
Use it to spot patterns worth roasting:
- Long losing streaks ("PlayerA hasn't cracked a 4 in three weeks")
- Declining averages ("PlayerB's scores are trending the wrong direction")
- Personal bests or worsts ("first 3/6 in 40 days — mark the calendar")
- Head-to-head trends ("PlayerC has beaten PlayerA four days straight")
- Attendance gaps ("PlayerD shows up once a week like a guest lecturer")
Pick ONE or TWO observations max — don't turn the roast into a stats report.
Weave the stat naturally into a joke. Never say "according to the data" or reference
the table directly. The stat should feel like something you just *know* from watching.

CRITICAL — ACCURACY OVER NARRATIVE:
The score table is the ground truth. Every trend, streak, or comparison you cite
must be literally verifiable by reading the table. Do NOT say "back-to-back" unless
two consecutive rows actually show the same score. Do NOT say "X days in a row"
unless you can count X consecutive matching rows. If you're tempted to claim a
pattern and the table doesn't precisely support it, drop the claim — a sharper
roast about what actually happened beats a made-up streak every time. The dates
in the table are the puzzle dates; today's incoming results are the newest puzzle
(one day after the last row in the table).

ROAST RULES:
1. Crown the winner with genuine (if sarcastic) respect.
2. Specifically call out anyone who scored 5/6 or 6/6, especially if a faster player already solved it in 3.
3. If someone performed worse than someone they'd normally beat, highlight it.
4. Keep it punchy — 3 to 6 sentences total, no walls of text.
5. End with one sentence hyping tomorrow's game.
6. Never use emojis unless they're truly earned.
7. Tone: disappointed sports commentator meets group chat bestie. Disbelief, mild betrayal, dramatic flair — but never mean-spirited.
8. Do not invent recurring titles or characterizations for players (e.g. "self-proclaimed
   king", "reigning champion", "the veteran"). If it is not explicitly in their player
   notes or directly observable from today's grid, do not say it.
9. Use fresh language every time. Never reuse a phrase that appears in the recent roast
   history. Crown and royalty metaphors ("split the crown", "claiming the throne",
   "defending the title") are currently overused — retire them indefinitely.

FORMATTING:
- Put a blank line before the closing hype sentence so it stands apart from the roast body.
- After the hype sentence, add another blank line and then a fun animal fact as a
  sign-off. The animal fact should be its own separate line and should tie back to
  something that happened in the results (e.g. a slow player, a lucky guess, a
  dramatic collapse). Keep it to one sentence.
- In the animal fact, bold the animal's name with double asterisks (e.g. **sloth**)
  and start the line with an emoji of that animal or the closest match available.
  Example: 🐢 Fun animal fact: The **sea turtle** can hold its breath for seven hours — PlayerA took notes.
- Never repeat an animal from the ANIMALS ALREADY USED list below. That list covers
  the last 60 days — if an animal appears there, pick a completely different one today.

Output only the roast. No parsing steps, no bullet lists, no preamble, no separators. Just the roast text."""


def build_system_prompt(requested_animal: str | None = None) -> str:
    """Append any saved player notes and recent roast history to the base prompt.

    If requested_animal is provided, the prompt is told to use that exact animal
    in today's sign-off fact — overriding the normal "no repeat" rule.
    """
    prompt = BASE_SYSTEM_PROMPT

    if player_notes:
        prompt += "\n\nPLAYER NOTES (set by the group — factor these into every roast):\n"
        for player, note in player_notes.items():
            prompt += f"- {player}: {note}\n"

    if roast_history:
        prompt += "\n\nRECENT ROAST HISTORY (use for callbacks, streaks, or running jokes when natural):\n"
        for entry in roast_history:
            prompt += f"\n[{entry['date']}]\n"
            prompt += f"Scores: {entry['scores']}\n"
            prompt += f"Roast: {entry['roast']}\n"

    score_table = build_score_table(score_history)
    if score_table:
        prompt += f"\n\nPLAYER SCORE HISTORY (last {len(score_history)} days):\n"
        prompt += score_table + "\n"

    if requested_animal:
        # The group requested a specific animal for today's sign-off.
        # This overrides the "animals already used" rule — use it even if it's in that list.
        prompt += (
            f"\n\nREQUIRED ANIMAL FOR TODAY'S SIGN-OFF: Use **{requested_animal}** as the animal "
            f"in today's sign-off fact. This was specifically requested by the group and OVERRIDES "
            f"the 'animals already used' rule — use this animal even if it has appeared recently. "
            f"Still bold the name with double asterisks and start the line with a matching emoji.\n"
        )
    if temp_roasts:
        prompt += "\n\nTEMPORARY ROAST NOTES (one-time, use only if relevant to today's results):\n"
        for player, instruction in temp_roasts.items():
            prompt += f"- {player}: {instruction}\n"
        prompt += (
            "Rules for TEMPORARY ROAST NOTES: work these in naturally when they fit today's "
            "results. If today's results contradict the note (e.g. the note says they missed "
            "a day but they actually played, or it says they choked but they scored a 3), "
            "acknowledge reality instead of blindly following the note. If a note simply "
            "doesn't fit today's data at all, you may ignore it. Do not reference this "
            "section directly — weave it in.\n"
        )

    if animal_history and not requested_animal:
        recent_animals = [entry["animal"] for entry in animal_history]
        prompt += f"\n\nANIMALS ALREADY USED (do NOT repeat any of these — pick a completely different animal):\n"
        prompt += ", ".join(recent_animals) + "\n"

    return prompt


# ─────────────────────────────────────────────
# DISCORD CLIENT SETUP
# ─────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

discord_client = discord.Client(intents=intents)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Track the last date a roast was sent so we only roast once per day
last_roast_date: date | None = None
# Track the last date a nightly reminder was sent — same pattern as above
last_reminder_date: date | None = None


# ─────────────────────────────────────────────
# NIGHTLY REMINDER TASK
# Uses discord.ext.tasks — a built-in scheduler that lets you run a
# function on a repeating loop. The @tasks.loop decorator with a `time=`
# argument fires once per day at that exact time. We pass a timezone-aware
# `time` object so it fires at 11:00 PM EDT regardless of the server's
# local clock setting.
# ─────────────────────────────────────────────

REMINDER_TIME = time(hour=23, minute=0, tzinfo=EDT)  # 11:00 PM EDT


@tasks.loop(time=REMINDER_TIME)
async def nightly_reminder():
    """Send a short Claude-generated reminder to play Wordle before midnight."""
    global last_reminder_date

    today = date.today()

    # Once-per-day guard — same pattern as the roast's last_roast_date.
    # Prevents double-fires if the bot restarts close to 11 PM.
    if last_reminder_date == today:
        log.info("Nightly reminder already sent today — skipping.")
        return

    channel = discord_client.get_channel(REMINDER_CHANNEL_ID)
    if channel is None:
        log.error(f"Could not find reminder channel {REMINDER_CHANNEL_ID}!")
        return

    # We never reference individual scores in the reminder — today's data isn't
    # aggregated until tomorrow morning when the roast fires. Calling out specific
    # players based on stale or missing data caused false accusations. Generic only.
    reminder_prompt = """You are the Wordle Warlords reminder bot — same vibe as the morning roast, but this is the 11 PM nudge.

Write 1-2 short sentences reminding the group to get their Wordle in before midnight.
Tone: snarky and casual — like a friend poking the group chat. NOT a full roast, save that for the morning.
Do NOT call out or name any specific players. Address the group as a whole.
No emojis. No hashtags. Just a quick nudge.

Output only the reminder message. Nothing else."""

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            system=reminder_prompt,
            messages=[
                {"role": "user", "content": "Send tonight's Wordle reminder."}
            ]
        )

        reminder_text = response.content[0].text
        log.info(f"Nightly reminder generated:\n{reminder_text}")

        await channel.send(reminder_text)
        last_reminder_date = today
        log.info("Nightly reminder sent successfully.")

    except anthropic.APIError as e:
        log.error(f"Claude API error during nightly reminder: {e}")
    except discord.DiscordException as e:
        log.error(f"Discord send error during nightly reminder: {e}")


@nightly_reminder.before_loop
async def before_nightly_reminder():
    """Wait until the bot is fully connected before the loop starts ticking.
    Without this, the task might try to send a message before the bot has
    logged in, which would fail."""
    await discord_client.wait_until_ready()


@discord_client.event
async def on_ready():
    log.info(f"Wordle Roast Bot is online as {discord_client.user}")
    log.info(f"Monitoring {len(TARGET_CHANNEL_IDS)} channel(s): {TARGET_CHANNEL_IDS}")
    log.info(f"Loaded {len(player_notes)} player note(s) from disk.")
    log.info(f"Loaded {len(roast_history)} roast(s) in history.")
    log.info(f"Loaded {len(score_history)} day(s) of score history.")
    log.info(f"Loaded {len(animal_history)} animal(s) in animal history.")
    log.info(f"Loaded {len(roast_archive)} roast(s) in archive.")
    log.info(f"Loaded {len(player_aliases)} player alias(es).")
    if animal_request:
        log.info(f"Pending animal request: {animal_request.get('animal')} (from {animal_request.get('requester')})")
    else:
        log.info("No pending animal request.")
    log.info(f"Loaded {len(temp_roasts)} temp roast note(s).")

    # Start the nightly reminder loop. The `if not .is_running()` check
    # prevents duplicate loops — on_ready can fire more than once if
    # Discord drops and reconnects the websocket.
    if not nightly_reminder.is_running():
        nightly_reminder.start()
        log.info(f"Nightly reminder scheduled for {REMINDER_TIME} EDT.")


@discord_client.event
async def on_message(message: discord.Message):
    global last_roast_date, last_reminder_date, player_notes, roast_history, score_history, animal_history, roast_archive, user_map, player_aliases, animal_request, temp_roasts

    # Ignore messages from channels we're not monitoring
    if message.channel.id not in TARGET_CHANNEL_IDS:
        return

    # Ignore the bot's own messages (prevents infinite loops)
    if message.author == discord_client.user:
        return

    # ── Auto-populate the user map from any real Discord mentions ──
    # discord.py's message.mentions is a list of fully-resolved User/Member
    # objects for every <@id> in the message. We stash any new ones so next
    # time that player appears, resolve_mentions() can swap their ID for a
    # name without any manual setup. Existing entries are NOT overwritten —
    # that way, a `Wordle Map:` override always wins over Discord's default.
    map_dirty = False
    for user in message.mentions:
        uid = str(user.id)
        if uid not in user_map:
            # Prefer display_name (server nickname) over plain username,
            # since it's closer to what the group actually calls each other.
            user_map[uid] = user.display_name
            map_dirty = True
            log.info(f"Auto-learned user: {uid} -> {user.display_name}")
    if map_dirty:
        save_user_map(user_map)

    # Rewrite <@id> mentions to @displayname BEFORE any downstream logic
    # touches the content. Every trigger check, parser, and Claude call
    # from here on sees clean text with readable names.
    content = resolve_mentions(message.content.strip(), user_map)

    # ─────────────────────────────────────────
    # WORDLE EDIT — append a note about a player
    # Usage: Wordle Edit: PlayerName note here
    # Notes accumulate — each new edit is tacked on with a " | " separator.
    # To start fresh, use Wordle Forget first, then Wordle Edit.
    # ─────────────────────────────────────────
    if content.startswith("Wordle Edit:"):
        remainder = content[len("Wordle Edit:"):].strip()

        # Extract the player name — it must be the first word after the command
        parts = remainder.split(" ", 1)
        if len(parts) < 2:
            await message.channel.send(
                "Usage: `Wordle Edit: PlayerName your note here`"
            )
            return

        player = parts[0]   # e.g. "PlayerName"
        note = parts[1]     # everything after the name

        # Append to existing note instead of overwriting.
        # Uses " | " as a separator so multiple notes read cleanly in the prompt.
        if player in player_notes:
            player_notes[player] += f" | {note}"
        else:
            player_notes[player] = note
        save_notes(player_notes)

        log.info(f"Note updated for {player}: {player_notes[player]}")
        await message.channel.send(
            f"Got it, added note for {player}: *{note}*"
        )
        return

    # ─────────────────────────────────────────
    # WORDLE TEMP ROAST — one-shot instruction for the next real roast
    # Usage: Wordle Temp Roast: PlayerName - instruction text
    #    or: Wordle Temp Roast: clear PlayerName
    # Overwrites any existing temp note for that player. Wiped after the next
    # real roast consumes it. Open to everyone.
    # ─────────────────────────────────────────
    if content.startswith("Wordle Temp Roast:"):
        remainder = content[len("Wordle Temp Roast:"):].strip()

        if remainder.lower().startswith("clear "):
            target = remainder[len("clear "):].strip()
            canonical = player_aliases.get(target, target)
            removed = False
            for key in (target, canonical):
                if key in temp_roasts:
                    del temp_roasts[key]
                    removed = True
            if removed:
                save_temp_roasts(temp_roasts)
                await message.channel.send(f"Temp roast note for **{canonical}** cleared.")
            else:
                await message.channel.send(f"No temp roast note found for **{target}**.")
            return

        if " - " not in remainder:
            await message.channel.send(
                "Usage: `Wordle Temp Roast: PlayerName - instruction here`\n"
                "Or: `Wordle Temp Roast: clear PlayerName`"
            )
            return

        player, instruction = remainder.split(" - ", 1)
        player = player.strip()
        instruction = instruction.strip()
        if not player or not instruction:
            await message.channel.send(
                "Usage: `Wordle Temp Roast: PlayerName - instruction here`"
            )
            return

        canonical = player_aliases.get(player, player)
        temp_roasts[canonical] = instruction
        save_temp_roasts(temp_roasts)
        log.info(f"Temp roast note set for {canonical}: {instruction}")
        await message.channel.send(
            f"Got it — will work that into the next roast for **{canonical}** (if it fits)."
        )
        return

    # ─────────────────────────────────────────
    # WORDLE FORGET — remove a note
    # Usage: Wordle Forget: PlayerName
    #    or: Wordle Forget: all
    # ─────────────────────────────────────────
    if content.startswith("Wordle Forget:"):
        target = content[len("Wordle Forget:"):].strip()

        if target.lower() == "all":
            if ADMIN_USERNAME not in message.author.display_name.lower():
                await message.channel.send("Nice try. Only the admin can wipe all notes.")
                return
            player_notes.clear()
            save_notes(player_notes)
            log.info("All player notes cleared.")
            await message.channel.send("All player notes have been wiped.")

        elif not target.startswith("<"):  # ignore raw Discord mention objects
            if target in player_notes:
                del player_notes[target]
                save_notes(player_notes)
                log.info(f"Note removed for {target}.")
                await message.channel.send(f"Note for {target} has been forgotten.")
            else:
                await message.channel.send(f"No note found for {target}.")

        else:
            await message.channel.send(
                "Usage: `Wordle Forget: PlayerName` or `Wordle Forget: all`"
            )
        return

    # ─────────────────────────────────────────
    # WORDLE MAP — manually set a Discord ID → display name mapping
    # Usage: Wordle Map: 453912146707742730 PlayerName
    # Needed when Discord's display name differs from the group's nickname.
    # Overwrites any existing entry for that ID.
    # ─────────────────────────────────────────
    if content.startswith("Wordle Map:"):
        remainder = content[len("Wordle Map:"):].strip()
        parts = remainder.split(" ", 1)
        if len(parts) < 2 or not parts[0].isdigit():
            await message.channel.send(
                "Usage: `Wordle Map: 123456789012345678 DisplayName`"
            )
            return
        user_id, display_name = parts[0], parts[1].strip()
        user_map[user_id] = display_name
        save_user_map(user_map)
        log.info(f"User map updated: {user_id} -> {display_name}")
        await message.channel.send(
            f"Mapped `{user_id}` to **{display_name}**."
        )
        return

    # ─────────────────────────────────────────
    # WORDLE HELP — list available commands
    # Usage: Wordle Help
    # ─────────────────────────────────────────
    if content.lower().startswith("wordle help"):
        help_text = (
            "**Wordle Roast Bot — Commands**\n"
            "`Wordle Edit: PlayerName your note here` — Add a note about a player (notes stack up)\n"
            "`Wordle Temp Roast: PlayerName - instruction` — One-shot note for the next roast, wiped after use\n"
            "`Wordle Temp Roast: clear PlayerName` — Remove a pending temp roast note\n"
            "`Wordle Notes: PlayerName` — Show the current notes for a player\n"
            "`Wordle Forget: PlayerName` — Wipe all notes for a player\n"
            "`Wordle Forget: all` — Wipe all player notes\n"
            "`Wordle Stats: PlayerName` — Show score history for a player\n"
            "`Wordle Roast: April 10, 2026` — Look up the roast for a past date\n"
            "`Wordle Alias: CurrentName OldName` — Link an old name to a current one\n"
            "`Wordle Aliases` — List all name aliases\n"
            "`Wordle Map: 123456789 DisplayName` — Manually map a Discord ID to a name\n"
            "`Wordle Animal Request: octopus` — Request the animal for tomorrow's sign-off (one at a time; confirm overrides with Yes/No)\n"
            "`Wordle Help` — Show this message\n\n"
            "Use plain names (e.g. `PlayerName`), not @mentions."
        )
        await message.channel.send(help_text)
        return

    # ─────────────────────────────────────────
    # WORDLE NOTES — show notes for a player
    # Usage: Wordle Notes: PlayerName
    # ─────────────────────────────────────────
    if content.startswith("Wordle Notes:"):
        target = content[len("Wordle Notes:"):].strip()

        if not target:
            await message.channel.send(
                "Usage: `Wordle Notes: PlayerName`"
            )
            return

        if target in player_notes:
            await message.channel.send(
                f"Notes for {target}: *{player_notes[target]}*"
            )
        else:
            await message.channel.send(f"No notes found for {target}.")
        return

    # ───────────────────────────────────────
    # WORDLE ALIAS — link an old name to a current name
    # Usage: Wordle Alias: CurrentName OldName
    # ───────────────────────────────────────
    if content.startswith("Wordle Alias:"):
        remainder = content[len("Wordle Alias:"):].strip()
        parts = remainder.split(None, 1)
        if len(parts) < 2:
            await message.channel.send(
                "Usage: `Wordle Alias: CurrentName OldName`\n"
                "Example: `Wordle Alias: PlayerA PlayerA_old`"
            )
            return
        current_name, old_name = parts[0], parts[1]
        player_aliases[old_name] = current_name
        save_aliases(player_aliases)
        log.info(f"Alias set: {old_name} -> {current_name}")

        # Inject alias info into player notes so the LLM knows about the name change.
        # When the roast prompt lists notes for current_name, it will also know
        # old_name is the same person — useful if old notes reference the old name.
        alias_note = f"[also known as: {old_name}]"
        if current_name in player_notes:
            if old_name not in player_notes[current_name]:
                player_notes[current_name] += f" | {alias_note}"
        else:
            player_notes[current_name] = alias_note

        # If the old name had its own notes, carry them over and remove the orphan entry.
        if old_name in player_notes:
            old_note = player_notes[old_name]
            player_notes[current_name] += f" | [notes from {old_name}: {old_note}]"
            del player_notes[old_name]
            log.info(f"Carried notes from {old_name} to {current_name}: {old_note}")

        save_notes(player_notes)
        log.info(f"Player notes updated for {current_name}: {player_notes.get(current_name)}")

        await message.channel.send(
            f"Linked **{old_name}** as an alias for **{current_name}**."
        )
        return

    # ───────────────────────────────────────
    # WORDLE ALIASES — list all name aliases
    # Usage: Wordle Aliases
    # ───────────────────────────────────────
    if content.lower().startswith("wordle aliases"):
        if not player_aliases:
            await message.channel.send("No aliases set.")
        else:
            alias_lines = [f"**{v}** ← {k}" for k, v in player_aliases.items()]
            await message.channel.send(
                "**Player Aliases:**\n" + "\n".join(alias_lines)
            )
        return

    # ───────────────────────────────────────
    # WORDLE STATS — show score history for a player
    # Usage: Wordle Stats: PlayerName
    # ───────────────────────────────────────
    if content.startswith("Wordle Stats:"):
        target = content[len("Wordle Stats:"):].strip()

        if not target:
            await message.channel.send("Usage: `Wordle Stats: PlayerName`")
            return

        # Build a set of all known player names across score history
        all_players = set(
            name
            for entry in score_history
            for name in entry["scores"]
        )

        # Also include alias keys/values so users can look up by old or new name
        known_names = all_players | set(player_aliases.keys()) | set(player_aliases.values())

        # Try exact match first, then fall back to case-insensitive
        matched_name = None
        if target in known_names:
            matched_name = target
        else:
            target_lower = target.lower()
            for name in known_names:
                if name.lower() == target_lower:
                    matched_name = name
                    break

        if matched_name is None:
            await message.channel.send(f"No score history found for **{target}**.")
            return

        # Resolve aliases so stats combine across name changes
        canonical, all_names = resolve_player_aliases(matched_name, player_aliases)

        stats = build_player_stats(canonical, score_history, names=all_names)
        if stats:
            await message.channel.send(stats)
        else:
            await message.channel.send(f"No score history found for **{target}**.")
        return

    # ───────────────────────────────────────
    # WORDLE ROAST — look up a past roast by date
    # Usage: Wordle Roast: April 10, 2026
    # Accepts: "April 10, 2026" / "Apr 10, 2026" / "4/10/2026" / "2026-04-10"
    # ───────────────────────────────────────
    if content.startswith("Wordle Roast:"):
        date_str = content[len("Wordle Roast:"):].strip()
        parsed_date = None
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                parsed_date = datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue
        if parsed_date is None:
            await message.channel.send(
                f"Couldn't parse that date. Try: `Wordle Roast: April 10, 2026`"
            )
            return
        key = parsed_date.isoformat()
        if key in roast_archive:
            label = parsed_date.strftime("%B %d, %Y")
            await message.channel.send(f"**Roast — {label}:**\n\n{roast_archive[key]}")
        else:
            await message.channel.send(
                f"No roast on record for {parsed_date.strftime('%B %d, %Y')}. "
                f"Archive goes back up to 60 days."
            )
        return

    # ─────────────────────────────────────────
    # WORDLE ANIMAL REQUEST — request a specific animal for the next roast's sign-off
    # Usage: Wordle Animal Request: octopus
    # One pending request at a time. If a request already exists, anyone can
    # propose an override; the bot asks them to confirm with Yes/No.
    # Usage: Wordle Animal Request: Yes   (confirm override)
    # Usage: Wordle Animal Request: No    (cancel override)
    # ─────────────────────────────────────────
    if content.startswith("Wordle Animal Request:"):
        arg = content[len("Wordle Animal Request:"):].strip()
        if not arg:
            await message.channel.send(
                "Usage: `Wordle Animal Request: octopus`\n"
                "Requests the animal for tomorrow's roast sign-off. One pending request at a time."
            )
            return

        user_id = str(message.author.id)
        requester_name = message.author.display_name

        # Yes / No branch — only valid if this user has a pending override proposal
        if arg.lower() in ("yes", "no"):
            proposal = pending_override.get(user_id)
            if not proposal:
                await message.channel.send(
                    "You don't have a pending override to confirm. "
                    "Start with `Wordle Animal Request: <animal>`."
                )
                return
            if arg.lower() == "yes":
                new_animal = proposal["animal"]
                animal_request = {
                    "date": date.today().isoformat(),
                    "animal": new_animal,
                    "requester": requester_name,
                }
                save_animal_request(animal_request)
                del pending_override[user_id]
                log.info(f"Animal request overridden by {requester_name}: {new_animal}")
                await message.channel.send(
                    f"Override confirmed. Tomorrow's sign-off will feature **{new_animal}**."
                )
            else:
                del pending_override[user_id]
                await message.channel.send("Override cancelled. Existing request stands.")
            return

        # Otherwise treat arg as a new animal name. Normalize to lowercase
        # so it matches the format stored in animal_history.
        new_animal = arg.lower()

        if animal_request:
            # A request is already pending — stash this as a proposal and ask for confirmation.
            pending_override[user_id] = {"animal": new_animal}
            current = animal_request.get("animal", "unknown")
            current_requester = animal_request.get("requester", "someone")
            await message.channel.send(
                f"A request for **{current}** is already pending (from {current_requester}). "
                f"Override with **{new_animal}**? Reply `Wordle Animal Request: Yes` or "
                f"`Wordle Animal Request: No`."
            )
            return

        # No pending request — save the new one.
        animal_request = {
            "date": date.today().isoformat(),
            "animal": new_animal,
            "requester": requester_name,
        }
        save_animal_request(animal_request)
        log.info(f"Animal request saved by {requester_name}: {new_animal}")
        await message.channel.send(
            f"Got it. Tomorrow's roast sign-off will feature **{new_animal}**."
        )
        return

    # ─────────────────────────────────────────────
    # SECRET ADMIN COMMAND — output the current system prompt
    # Restricted to users whose display name contains ADMIN_USERNAME.
    # Set ADMIN_SECRET_CMD in .env to customize. NOT listed in Wordle Help.
    # ─────────────────────────────────────────────
    if content.strip() == ADMIN_SECRET_CMD:
        if ADMIN_USERNAME not in message.author.display_name.lower():
            return  # silently ignore — do not reveal the command exists
        prompt = build_system_prompt()
        chunk_size = 1990
        for i in range(0, len(prompt), chunk_size):
            await message.channel.send(f"```\n{prompt[i:i+chunk_size]}\n```")
        return

    # ROAST TRIGGER
    # "ROAST TEST" = manual test mode — skips the once-per-day guard
    # so you can fire roasts repeatedly during testing.
    # ─────────────────────────────────────────
    is_test = "ROAST TEST" in content

    if not is_test and "Here are yesterday's results" not in content:
        return

    # Only roast once per calendar day (unless testing)
    today = date.today()
    if not is_test and last_roast_date == today:
        log.info("Already roasted today — skipping.")
        return

    log.info(f"{'[TEST] ' if is_test else ''}Results detected! Generating roast for:\n{content}")

    # ── Download any images attached to the results message ──
    # Discord attaches Wordle grid images as URLs. We download each one,
    # convert it to base64, and pass it to Claude alongside the text so
    # it can analyze the actual grid patterns — not just the score numbers.
    image_blocks = []
    async with httpx.AsyncClient() as http:
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith("image/"):
                try:
                    resp = await http.get(attachment.url)
                    resp.raise_for_status()
                    # Detect the real image type from the first few bytes
                    # (Discord's content_type header is sometimes wrong)
                    img_bytes = resp.content
                    if img_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                        media_type = "image/png"
                    elif img_bytes[:3] == b'\xff\xd8\xff':
                        media_type = "image/jpeg"
                    elif img_bytes[:6] in (b'GIF87a', b'GIF89a'):
                        media_type = "image/gif"
                    else:
                        media_type = "image/png"  # safe fallback

                    img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
                    image_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": img_b64,
                        }
                    })
                    log.info(f"Attached image: {attachment.filename}")
                except Exception as e:
                    log.warning(f"Failed to download image {attachment.filename}: {e}")

    # Build the user message — text first, then any grid images
    # In test mode, tell the model to ignore history and roast fresh.
    msg_text = content
    if is_test:
        msg_text = "[TEST MODE — ignore prior roast history and generate a completely fresh roast. Do not reference this prefix in your output.]\n\n" + msg_text
    user_content = [{"type": "text", "text": msg_text}] + image_blocks

    # If a specific animal was requested for today's sign-off, pull it here so
    # we both inject it into the prompt and know to clear the request on success.
    requested_animal = animal_request.get("animal") if animal_request else None
    if requested_animal:
        log.info(f"{'[TEST] ' if is_test else ''}Using requested animal: {requested_animal}")

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=build_system_prompt(requested_animal=requested_animal),
            messages=[
                {"role": "user", "content": user_content}
            ]
        )

        roast_text = response.content[0].text
        log.info(f"Roast generated:\n{roast_text}")

        await message.channel.send(roast_text)

        # Only update history files for real roasts — test roasts should not
        # pollute roast_history.json, score_history.json, or animal_history.json,
        # since test data would then show up as fake callbacks and skew trend analysis.
        if not is_test:
            # The Wordle message says "Here are yesterday's results", so the puzzle
            # these scores correspond to was played the day before this roast runs.
            # Store everything under the puzzle date so history aligns with reality
            # (prevents Claude from misreading trends across adjacent days).
            puzzle_date = today - timedelta(days=1)
            puzzle_key = puzzle_date.isoformat()

            # Skip if we already have an entry for this puzzle date (e.g. the bot
            # restarted and the once-per-day in-memory guard got reset).
            already_saved = (
                (score_history and score_history[-1]["date"] == puzzle_key)
                or (roast_history and roast_history[-1]["date"] == puzzle_key)
                or puzzle_key in roast_archive
            )
            if already_saved:
                log.info(f"History already contains puzzle date {puzzle_key} — skipping save.")
            else:
                # Save this roast to history so future roasts can reference it.
                # We store the puzzle date, the raw scores text, and the roast output.
                roast_history.append({
                    "date": puzzle_key,
                    "scores": content,
                    "roast": roast_text,
                })
                save_history(roast_history)
                # Keep the in-memory list trimmed to match what's on disk
                while len(roast_history) > MAX_ROAST_HISTORY:
                    roast_history.pop(0)
                log.info(f"Roast history now has {len(roast_history)} entry/entries.")

                # Save parsed scores to the long-term score history.
                # parse_scores_from_text runs in Python so scores are guaranteed
                # accurate — no LLM involved in this step.
                parsed_scores = parse_scores_from_text(content)
                if parsed_scores:
                    score_history.append({
                        "date": puzzle_key,
                        "scores": parsed_scores,
                    })
                    save_score_history(score_history)
                    while len(score_history) > MAX_SCORE_HISTORY:
                        score_history.pop(0)
                    log.info(f"Score history: saved {parsed_scores} — {len(score_history)} day(s) total.")
                else:
                    log.warning("Could not parse any scores from the message text.")

                # Extract and save the animal used in today's sign-off fact.
                animal = extract_animal_from_roast(roast_text)
                if animal:
                    animal_history.append({
                        "date": puzzle_key,
                        "animal": animal,
                    })
                    save_animal_history(animal_history)
                    while len(animal_history) > MAX_ANIMAL_HISTORY:
                        animal_history.pop(0)
                    log.info(f"Animal history: saved '{animal}' — {len(animal_history)} animal(s) total.")
                else:
                    log.warning("Could not extract animal name from roast (no **bold** text found).")

                # Archive the full roast text so users can look it up by date later.
                roast_archive[puzzle_key] = roast_text
                save_roast_archive(roast_archive)
                log.info(f"Roast archive: saved {puzzle_key} — {len(roast_archive)} day(s) total.")

            # If this roast consumed a pending animal request, clear it so the
            # next roast returns to normal no-repeat behavior.
            if requested_animal:
                clear_animal_request()
                animal_request = {}
                log.info(f"Cleared pending animal request ({requested_animal}).")

            # Wipe temp roast notes now that this real roast has consumed them.
            if temp_roasts:
                consumed = list(temp_roasts.keys())
                temp_roasts.clear()
                clear_temp_roasts()
                log.info(f"Cleared temp roast notes for: {consumed}")
        else:
            log.info("[TEST] Skipping history update — test roasts do not modify history files.")

        # Only mark the day as "roasted" for real triggers — test roasts
        # shouldn't block the actual daily roast from firing later.
        if not is_test:
            last_roast_date = today

    except anthropic.APIError as e:
        log.error(f"Claude API error: {e}")
    except discord.DiscordException as e:
        log.error(f"Discord send error: {e}")


# ─────────────────────────────────────────────
# START THE BOT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN is not set in your .env file!")
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY is not set in your .env file!")
    if not TARGET_CHANNEL_IDS:
        raise ValueError("WORDLE_CHANNEL_IDS is not set in your .env file!")
    if not REMINDER_CHANNEL_ID:
        raise ValueError("REMINDER_CHANNEL_ID is not set in your .env file!")

    discord_client.run(DISCORD_TOKEN, log_handler=None)
