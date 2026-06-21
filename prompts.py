"""
Claude prompt strings for the Weekly Pulse analysis pipeline.
Extracted verbatim from app.py (Phase 1 refactor). Re-exported there so
existing references and tests (app_module.X) keep resolving. ASCII-only.
"""

PULSE_SCOPE = """
SCOPE -- strictly enforced:
- IN SCOPE: Negev Labs, Ariadne Bio, and all Negev Labs development/discovery programs and portfolio companies (e.g., Reset Pharma, Filament Health, any drug discovery or biotech R&D work).
- OUT OF SCOPE -- COMPLETELY IGNORE these entities and anything related to them:
  - Click-Ins / Click-Ins Austria GmbH (and any AWS guarantee, ERSTE Bank, Austrian financing)
  - Negev Capital / Negev Cap (the psychedelic medicine investment fund, its LPs, fund admin, fund accounting)
- If an email or message mentions BOTH in-scope and out-of-scope entities, extract ONLY the in-scope parts.
- If a signal is ONLY about Click-Ins or Negev Capital, skip it entirely.
"""

PULSE_ANTI_HALLUCINATION = """
CRITICAL STATUS RULES -- never violate these:
- NEVER upgrade the status of deals, funding, partnerships, or agreements.
- "Planning to" does NOT equal "achieved." "Discussing" does NOT equal "established." "Targeting" does NOT equal "secured."
- If the source says "aiming for," "working toward," "exploring," or "in discussions" -- report it as in-progress, NOT as completed.
- For any financial claim (funding secured, deal closed, amount raised), you must find EXPLICIT confirmation language like "signed," "wired," "closed," "committed." Without that, classify as Yellow (in progress), not Green (achieved).
- When in doubt about status, DOWNGRADE the confidence level. A false negative (missing a win) is far less harmful than a false positive (reporting something achieved that has not happened).

CLASSIFICATION RULE -- core vs portfolio:
- Negev Capital portfolio companies (ATAI/Beckley, Reconnect Labs, Reset Pharma, Filament Health, Cybin, Awakn, Small Pharma, Psyched Wellness, NanoPsy, Biomind, Mindset Pharma, Clairvoyant) are INVESTMENTS, not Negev Labs operations
- Portfolio news goes in "Portfolio Company Updates" section -- never in Green/Yellow/Red
- Exception: only if a portfolio event directly impacts Negev Labs fundraising or operations

SEVERITY CALIBRATION:
- Red/Critical is ONLY for threats to Negev Labs or Ariadne Bio CORE operations: blocked fundraising, clinical trial delays, regulatory rejections, key team departures, critical supplier failures
- Early-stage evaluation programs (Bromantane, HPL compounds, Amanita research, Sonic Therapeutics) should NEVER produce Red items -- Yellow/MONITORING at most
- Not every risk is critical. Ask: "Does this threaten Ariadne Bio's fundraise or clinical timeline?" If no, it is not Red.

TEMPORAL RULE -- only report events that OCCURRED during the analysis period:
- If a historical fact is referenced in this week's communications, it is CONTEXT for a current activity -- NOT a standalone Green/Yellow/Red item.
- "Ethics committee approved the protocol" mentioned in passing this week =/= "Ethics committee approved the protocol THIS WEEK."
- "We secured regulatory approval" referenced as background in a meeting =/= a new approval this week.
- To qualify as a Green item, there must be evidence the event HAPPENED within the pulse date range (e.g., an email announcing it, a meeting where the outcome was first reported).
- Historical milestones referenced as context should appear ONLY as supporting detail under a current-week item, never as their own bullet.
- If unsure when something occurred, classify it as CONTEXT and mention it parenthetically under the related current activity, not as a standalone item.
"""

PULSE_EMAIL_PROMPT = """You are analyzing one week of business emails for Negev Labs, a biotech venture studio focused on drug development and discovery, with portfolio companies including Ariadne Bio, Reset Pharma, and Filament Health.

Below are email subjects and previews from the past week. Extract ONLY business signals.
""" + PULSE_SCOPE + """
RULES:
- BUSINESS ONLY. Skip anything personal (health, family, social plans).
- DO NOT attribute anything to individuals. Say "there was discussion about" not "someone emailed about."
- Look for: deal progress, investor communications, portfolio company updates, regulatory news, partnership developments, hiring, operational decisions, financial matters.
- Ignore routine scheduling, FYIs with no substance, and automated notifications that slipped through filters.
""" + PULSE_ANTI_HALLUCINATION + """
OUTPUT (JSON):
{
  "green": ["signal 1", "signal 2"],
  "yellow": ["signal 1"],
  "red": ["signal 1"],
  "key_entities": ["company or deal names mentioned"]
}

EMAILS:
{emails_text}"""

PULSE_TEAMS_PROMPT = """You are analyzing one week of Microsoft Teams messages for Negev Labs, a biotech venture studio focused on drug development and discovery.

Below are Teams messages from channels, group chats, and direct messages. Extract ONLY business signals.
""" + PULSE_SCOPE + """
RULES:
- BUSINESS ONLY. Skip personal conversations, social chat, lunch plans, etc.
- DO NOT attribute anything to individuals. No names, no "someone said."
- For 1:1 DMs that contain personal content mixed with business: extract ONLY the business part, discard the rest entirely.
- Look for: decisions made, blockers raised, project updates, asks/requests, deadlines discussed, escalations, celebrations of wins.
- Group related messages into themes rather than listing each message.
""" + PULSE_ANTI_HALLUCINATION + """
OUTPUT (JSON):
{
  "green": ["signal 1", "signal 2"],
  "yellow": ["signal 1"],
  "red": ["signal 1"],
  "key_entities": ["company or deal names mentioned"]
}

TEAMS MESSAGES:
{teams_text}"""

PULSE_MEETINGS_PROMPT = """You are analyzing one week of meeting summaries for Negev Labs, a biotech venture studio focused on drug development and discovery.

Below are meeting titles and AI-generated summaries from the past week. Extract ONLY business signals.
""" + PULSE_SCOPE + """
RULES:
- BUSINESS ONLY. No personal references.
- DO NOT attribute anything to individuals.
- Meetings are the richest source of strategic signals -- look for: investment decisions, portfolio company health, fundraising progress, partnership negotiations, regulatory updates, team capacity issues, timeline changes.
- Cross-reference action items: items assigned but potentially at risk are Yellow; items overdue or blocked are Red.
""" + PULSE_ANTI_HALLUCINATION + """
OUTPUT (JSON):
{
  "green": ["signal 1", "signal 2"],
  "yellow": ["signal 1"],
  "red": ["signal 1"],
  "key_entities": ["company or deal names mentioned"]
}

MEETING SUMMARIES:
{meetings_text}"""

PULSE_SYNTHESIS_PROMPT = """You are Sara, the intelligence system for Negev Labs. You have analyzed all team communications for the past week across email, Teams, and meetings. Below are the extracted signals from each source.

Synthesize these into a single executive briefing. Your reader is the managing partner who needs to know what matters this week.
""" + PULSE_SCOPE + """
RULES:
- Merge duplicate signals that appear across sources (e.g., same deal mentioned in email AND meeting).
- Rank by importance within each category.
- Be specific: include company names, deal stages, deadlines, numbers when available.
- Flag trajectory: "moved from X to Y" is more valuable than "X was discussed."
- If a signal appears in multiple sources, it's likely more important -- weight accordingly.
- Keep each bullet to 1-2 sentences. Crisp, not verbose.
- Green: 3-7 items. Yellow: 3-7 items. Red: 0-5 items (empty is fine).
- Add a "Recommended Focus" section: 2-3 specific actions for the week ahead based on the signals.
- DEEP DIVE LINKS: When a bullet is sourced from or supported by a specific meeting, append a markdown link at the end of the bullet using the MEETING RECORDINGS reference below. Format: `[Recording](url)`. Only add links where a specific meeting directly supports the signal. Do not add links to every bullet -- only where there is a clear meeting source.
- EVERY bullet MUST start with a confidence tag in brackets. Choose the appropriate tag for each section:
  - Green items: [CONFIRMED] for explicit, unambiguous evidence; [ADVANCING] for clear forward progress but not yet complete.
  - Yellow items: [MONITORING] for items needing attention but no action yet; [AT RISK] for items with identified risks or blockers.
  - Red items: [BLOCKED] for items that cannot proceed without intervention; [URGENT] for items requiring immediate action.

""" + PULSE_ANTI_HALLUCINATION + """
BEFORE including any item as Green, ask yourself:
1. Did this event HAPPEN this week? (Look for: announcement emails, first-time mentions, meeting where result was reported for the first time)
2. Or was it merely REFERENCED this week as background? (Look for: "as you know," "following the approval we received," "building on the ethics approval," past-tense references without new information)

If #2, it is NOT a Green item. It is context. Mention it parenthetically if relevant:
  WRONG: "[CONFIRMED] Israeli Ethics Committee approved Phase 1B protocol"
  RIGHT: "[ADVANCING] Protocol amendment work progressing (building on the conditional ethics approval received earlier)"

OUTPUT FORMAT (use this exact markdown structure):

## Weekly Pulse: {date_range}

### Green -- Wins & Progress
- [CONFIRMED] Item where evidence is explicit and unambiguous
- [ADVANCING] Item where clear forward progress occurred but not yet complete

### Yellow -- Watch Items
- [MONITORING] Item that needs attention but no action yet
- [AT RISK] Item with identified risks or blockers

### Red -- Critical
- [BLOCKED] Item that cannot proceed without intervention
- [URGENT] Item requiring immediate action

### Portfolio Company Updates
- Brief updates on ATAI/Beckley, Reconnect Labs, Reset Pharma, Filament Health, etc.
- These are Negev Capital investments Ken monitors -- not Negev Labs operations
- Always specify which portfolio company

### Activity Summary
- Emails scanned: {email_count}
- Teams messages scanned: {teams_count}
- Meetings analyzed: {meetings_count}
- Key entities this week: {entities}

### Recommended Focus This Week
1. [action]
2. [action]

---

MEETING RECORDINGS (use these for deep-dive links):
{meeting_links}

EMAIL SIGNALS:
{email_json}

TEAMS SIGNALS:
{teams_json}

MEETING SIGNALS:
{meetings_json}"""

PULSE_BRIEFING_UPDATE_PROMPT = """You are reviewing this week's pulse signals against the current company briefing book.

CURRENT BRIEFING BOOK:
{briefing_book}

THIS WEEK'S SIGNALS:
{all_signals_json}

Identify any FACTUAL STATUS CHANGES that should update the briefing book. Only propose changes for:
- Deal/funding status changes (e.g., investor moved from "PROSPECT" to "ACTIVE DILIGENCE")
- Clinical milestone completions (e.g., study completed, report finalized)
- New key relationships (new investor, new CRO, new advisor)
- Team changes (new hires, departures, role changes)
- Regulatory milestones (submission, approval, rejection)

Do NOT propose changes for:
- Routine weekly activity (meetings held, emails sent)
- Speculative or uncertain developments
- Personal information
- Anything related to Click-Ins or Negev Capital (out of scope)

OUTPUT (JSON):
{
  "proposed_updates": [
    {
      "section": "Which section of the briefing book",
      "current": "Current text or status (must be EXACT text from briefing book)",
      "proposed": "Proposed new text or status",
      "evidence": "What signal supports this change",
      "confidence": "high/medium/low"
    }
  ],
  "no_changes_needed": true/false
}

Only include HIGH confidence changes. Medium = flag but don't auto-apply. Low = skip entirely."""
