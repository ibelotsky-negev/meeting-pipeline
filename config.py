"""
Configuration and config-domain primitives for the Sara meeting pipeline.

All environment-variable reads and derived constants live here so app.py and
the per-domain modules share one source of truth. Imported at the top of
app.py; reads os.environ at import time (tests set env in conftest before
importing app, which imports this module first).

ASCII-only comments per project rules. strip_emojis stays in app.py because
its regex relies on Unicode escape literals that must not be reflowed here.
"""

import os
import json
import logging

logger = logging.getLogger(__name__)


# API Keys (set via environment variables)
FIREFLIES_API_KEY = os.environ["FIREFLIES_API_KEY"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
HUBSPOT_API_KEY = os.environ["HUBSPOT_API_KEY"]
ASANA_API_KEY = os.environ["ASANA_API_KEY"]

# Microsoft Graph (Outlook) -> OAuth2 client credentials or delegated
MS_GRAPH_CLIENT_ID = os.environ.get("MS_GRAPH_CLIENT_ID", "")
MS_GRAPH_CLIENT_SECRET = os.environ.get("MS_GRAPH_CLIENT_SECRET", "")
MS_GRAPH_TENANT_ID = os.environ.get("MS_GRAPH_TENANT_ID", "")
MS_GRAPH_REFRESH_TOKEN = os.environ.get("MS_GRAPH_REFRESH_TOKEN", "")  # For delegated flow
# Load refresh token from persistent storage if env var not set or to get latest rotated token
try:
    if os.path.exists("/data/refresh_token.txt"):
        with open("/data/refresh_token.txt") as f: _file_token = f.read().strip()
        if _file_token: MS_GRAPH_REFRESH_TOKEN = _file_token; import sys; print("[startup] Loaded refresh token from /data/refresh_token.txt", file=sys.stderr)
except Exception as _e:
    import sys; print(f"[startup] Could not load refresh token from file: {_e}", file=sys.stderr)
# Auth mode: "delegated" (uses refresh_token, /me/ endpoints -> single user only)
# "app" (uses client_credentials, /users/{email} endpoints -> team-wide)
# Auto-detected: if refresh_token set -- delegated, else -- app
MS_GRAPH_AUTH_MODE = os.environ.get("MS_GRAPH_AUTH_MODE", "auto")

# Configuration
ASANA_WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "")
ASANA_PROJECT_GID = os.environ.get("ASANA_PROJECT_GID", "")
HUBSPOT_OWNER_ID = os.environ.get("HUBSPOT_OWNER_ID", "")  # Fallback default owner
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "5"))
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8080")  # Your deployed URL
NOTIFY_VIA = os.environ.get("NOTIFY_VIA", "email")  # "email" (per-organizer), "teams" (shared ops channel), or "email,teams" for both
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL", "")  # Optional: shared ops channel for admin visibility
BOT_SENDER_EMAIL = os.environ.get("BOT_SENDER_EMAIL", "")  # e.g. sara@palomar-labs.com (shared mailbox)
BOT_SENDER_NAME = os.environ.get("BOT_SENDER_NAME", "Sara - Palomar Chief of Staff")
# Internal domains -- emails outside these domains are never sent notifications
INTERNAL_DOMAINS = [d.strip().lower() for d in os.environ.get("INTERNAL_DOMAINS", "negevlabs.com,negevcap.com,ariadnebio.com,zirmania.com,palomar-labs.com").split(",") if d.strip()]
# HubSpot owner map: maps organizer email -- HubSpot owner ID
# Supports two formats:
#   JSON:   {"bk@negevlabs.com":"241153249","shlomi@negevlabs.com":"241153250"}
#   Simple: bk@negevlabs.com:241153249,shlomi@negevlabs.com:241153250,dan@negevlabs.com:31299775
HUBSPOT_OWNER_MAP_RAW = os.environ.get("HUBSPOT_OWNER_MAP", "")
HUBSPOT_OWNER_MAP = {}
if HUBSPOT_OWNER_MAP_RAW:
    try:
        HUBSPOT_OWNER_MAP = json.loads(HUBSPOT_OWNER_MAP_RAW)
    except (json.JSONDecodeError, TypeError):
        # Parse simple format: email:id,email:id
        for pair in HUBSPOT_OWNER_MAP_RAW.split(","):
            pair = pair.strip()
            if ":" in pair:
                email, owner_id = pair.rsplit(":", 1)
                HUBSPOT_OWNER_MAP[email.strip()] = owner_id.strip()
    logger.info(f"Parsed HUBSPOT_OWNER_MAP: {HUBSPOT_OWNER_MAP}")

# Team members for review UI dropdown (email -> display name)
# Loaded from TEAM_MEMBER_NAMES env var (JSON: {"email":"Name"}) or auto-built from HUBSPOT_OWNER_MAP keys
TEAM_MEMBER_NAMES_RAW = os.environ.get("TEAM_MEMBER_NAMES", "")
TEAM_MEMBER_NAMES = {}
if TEAM_MEMBER_NAMES_RAW:
    try:
        TEAM_MEMBER_NAMES = json.loads(TEAM_MEMBER_NAMES_RAW)
    except (json.JSONDecodeError, TypeError):
        pass
# Default team if env var not set
if not TEAM_MEMBER_NAMES:
    TEAM_MEMBER_NAMES = {
        "bk@negevlabs.com": "Ken Belotsky",
        "shlomi@negevlabs.com": "Shlomi Raz",
        "dan@negevlabs.com": "Dan Jeffries",
        "ka@negevlabs.com": "Kostia Adamsky",
    }
# Build list for template: [{email, name}, ...]
TEAM_MEMBERS_LIST = [{"email": e, "name": n} for e, n in sorted(TEAM_MEMBER_NAMES.items(), key=lambda x: x[1])]
logger.info(f"Team members for UI: {[m['name'] for m in TEAM_MEMBERS_LIST]}")

# Email alias map: alternate emails -> canonical @negevlabs.com email
# People use multiple emails across Ariadne Bio, Negev Cap, etc.
# All lookups (Asana, HubSpot, dropdown) should resolve to the canonical email.
EMAIL_ALIAS_MAP = {
    "shlomi@ariadnebio.com": "shlomi@negevlabs.com",
    "shlomi@negevcap.com": "shlomi@negevlabs.com",
    "ka@ariadnebio.com": "ka@negevlabs.com",
    "kostia@negevcap.com": "ka@negevlabs.com",
    "dan@ariadnebio.com": "dan@negevlabs.com",
    "bk@ariadnebio.com": "bk@negevlabs.com",
    # Negev Labs -> Palomar Labs rename (2026-08-06): team now also sends from
    # @palomar-labs.com. Canonical identity stays @negevlabs.com everywhere
    # downstream (HubSpot owner map, Asana, TEAM_MEMBER_NAMES) -- only the
    # alias resolves. See INTERNAL_DOMAINS below for the matching domain add.
    "ken@palomar-labs.com": "bk@negevlabs.com",
    "shlomi@palomar-labs.com": "shlomi@negevlabs.com",
    "kostia@palomar-labs.com": "ka@negevlabs.com",
    "dan@palomar-labs.com": "dan@negevlabs.com",
}
# Also load from env var for runtime updates without redeploy
EMAIL_ALIAS_MAP_RAW = os.environ.get("EMAIL_ALIAS_MAP", "")
if EMAIL_ALIAS_MAP_RAW:
    try:
        EMAIL_ALIAS_MAP.update(json.loads(EMAIL_ALIAS_MAP_RAW))
    except (json.JSONDecodeError, TypeError):
        pass
logger.info(f"Email alias map: {len(EMAIL_ALIAS_MAP)} entries")


def normalize_team_email(email: str) -> str:
    """Resolve alternate emails to canonical @negevlabs.com email."""
    if not email:
        return email
    email_lower = email.strip().lower()
    return EMAIL_ALIAS_MAP.get(email_lower, email_lower)


# Track processed transcripts and pending approvals
# Railway volume mount: attach a volume at /data for persistence across deploys
DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
PROCESSED_FILE = os.path.join(DATA_DIR, "processed_transcripts.json")
PENDING_FILE = os.path.join(DATA_DIR, "pending_approvals.json")
SYNC_MAP_FILE = os.path.join(DATA_DIR, "asana_todo_map.json")
# Fireflies transcript ids whose fetch was deferred because the daily API
# quota was spent. Drained once the quota frees up -- without this a
# webhook arriving inside a dead window is lost for good.
FIREFLIES_DEFERRED_FILE = os.path.join(DATA_DIR, "fireflies_deferred.json")

# To-Do sync config
TODO_LIST_NAME = os.environ.get("TODO_LIST_NAME", "Asana Tasks")
# TODO_SYNC_USER_EMAIL removed - app-only mode syncs all @negevlabs.com users automatically
TODO_POLL_INTERVAL = int(os.environ.get("TODO_POLL_INTERVAL", "300"))
RAILWAY_PUBLIC_URL = os.environ.get("RAILWAY_PUBLIC_URL", "https://meeting-pipeline-production.up.railway.app")

# Teams Transcript Integration
TEAMS_WEBHOOK_SECRET = os.environ.get("TEAMS_WEBHOOK_SECRET", "sara-teams-transcript-secret")
TEAMS_TRANSCRIPT_ENABLED = os.environ.get("TEAMS_TRANSCRIPT_ENABLED", "true").lower() == "true"
# User ID for the organizer whose meetings we subscribe to (from Application Access Policy)
TEAMS_ORGANIZER_USER_ID = os.environ.get("TEAMS_ORGANIZER_USER_ID", "1824e8e3-027d-440a-b224-787e6d749dae")
# Comma-separated user IDs to poll for Teams transcripts
TEAMS_POLL_USER_IDS = [u.strip() for u in os.environ.get("TEAMS_POLL_USER_IDS",
    "1824e8e3-027d-440a-b224-787e6d749dae").split(",") if u.strip()]
TEAMS_POLL_INTERVAL = int(os.environ.get("TEAMS_POLL_INTERVAL", "300"))
SUBSCRIPTION_FILE = os.path.join(DATA_DIR, "graph_subscription.json")

# ======================================================================
# WEEKLY PULSE CONFIGURATION
# ======================================================================

PULSE_RECIPIENTS = ["bk@negevlabs.com", "vu@negevcap.com"]
PULSE_SENDER = "sara@palomar-labs.com"
PULSE_DOMAINS = ["negevlabs.com", "ariadnebio.com"]
PULSE_ARCHIVE_DIR = os.path.join(DATA_DIR, "pulse")
PULSE_LOOKBACK_DAYS = 7
BRIEFING_BOOK_PATH = os.path.join(DATA_DIR, "briefing-book.md")
BRIEFING_BOOK_REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "briefing-book.md")

# Email noise filters
PULSE_SKIP_SENDERS = [
    "noreply", "no-reply", "notification", "mailer-daemon",
    "calendar-notification", "postmaster",
]
PULSE_SKIP_DOMAINS = [
    "linkedin.com", "slack.com", "asana.com", "hubspot.com",
    "calendly.com", "zoom.us", "fireflies.ai", "github.com",
    "atlassian.com", "jira.com", "confluence.com",
    # Excluded entities -- not in scope for pulse analysis
    "click-ins.com", "click-ins.at", "clickins.com", "clickins.at",
    "negevcap.com",
]
PULSE_SKIP_SUBJECTS = [
    "out of office", "ooo", "automatic reply", "auto-reply",
    "unsubscribe", "newsletter", "digest", "accepted:",
    "declined:", "tentative:", "canceled:", "updated invitation:",
    # Excluded entities
    "click-ins", "clickins", "negev capital",
]


def load_briefing_book():
    """Load company context for Claude analysis calls.
    On first run, copies the repo version to the data volume."""
    import shutil
    if not os.path.exists(BRIEFING_BOOK_PATH):
        if os.path.exists(BRIEFING_BOOK_REPO):
            shutil.copy2(BRIEFING_BOOK_REPO, BRIEFING_BOOK_PATH)
            logger.info(f"[briefing] Initialized briefing book from repo ({os.path.getsize(BRIEFING_BOOK_PATH)} bytes)")
        else:
            logger.warning("[briefing] No briefing book found at repo or data volume")
            return ""
    try:
        with open(BRIEFING_BOOK_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        logger.info(f"[briefing] Loaded briefing book ({len(content)} chars)")
        return content
    except Exception as e:
        logger.warning(f"[briefing] Failed to load briefing book: {e}")
        return ""


def is_internal_email(email: str) -> bool:
    """Check if an email belongs to an internal domain."""
    if not email or "@" not in email:
        return False
    domain = email.lower().split("@")[1]
    return domain in INTERNAL_DOMAINS


def resolve_internal_organizer(organizer_email: str, participants: list, internal_lead_email: str = "") -> str:
    """Determine internal organizer for task/draft ownership.
    If meeting organizer is internal, use them.
    If external, prefer Claude-detected internal_lead (most active speaker),
    then HUBSPOT_OWNER_MAP members, then first internal participant."""
    if organizer_email and is_internal_email(organizer_email):
        return organizer_email.lower()
    # External organizer -- Claude identified most active internal speaker
    if internal_lead_email and is_internal_email(internal_lead_email):
        logger.info(f"[organizer] External organizer {organizer_email} -> internal lead {internal_lead_email} (Claude-detected)")
        return internal_lead_email.lower()
    # Fallback: internal participant in HUBSPOT_OWNER_MAP
    for email in (participants or []):
        if is_internal_email(email) and email.lower() in HUBSPOT_OWNER_MAP:
            logger.info(f"[organizer] External organizer {organizer_email} -> internal owner {email} (from owner map)")
            return email.lower()
    # Fallback: any internal participant
    for email in (participants or []):
        if is_internal_email(email):
            logger.info(f"[organizer] External organizer {organizer_email} -> internal fallback {email}")
            return email.lower()
    logger.warning(f"[organizer] No internal participant found, keeping original: {organizer_email}")
    return organizer_email
