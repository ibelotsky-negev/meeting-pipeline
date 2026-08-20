# Follow-Up Engine (pilot) -- Spec

Status: agreed by Ken + Dan, 2026-08-18. Design note (decision trail):
https://claude.ai/code/artifact/0a355f93-6b36-4a47-9c63-44352ec874aa
Source example: "CRO automatic response example 1.docx" (Adgyl -> Vimta,
28-day dog tox study; two asks in one thread; chase if silent 2 days).

## Purpose

When an external counterparty (typically a CRO) goes silent on an email
thread, produce a ready-to-send reminder draft in the thread owner's Outlook
Drafts folder and report it by email -- so a chase is never forgotten and
never depends on anyone checking the Drafts folder. The machine acts only on
silence, and in the pilot it NEVER sends anything to a counterparty: it
drafts, notifies, and stops.

## The loop

1. **Register (intake, every 15 min).** A team member forwards the thread to
   Sara (`BOT_SENDER_EMAIL`) with an instruction in the new body, e.g.
   *"if Vimta does not reply within 2 days, draft a reminder for me to send --
   on both points."* Sara parses the instruction (Claude), resolves the real
   conversation in the SENDER's mailbox (the forward is a new conversation --
   resolution is by normalized subject search + counterparty match), registers
   ONE WATCH PER ASK, and replies with a confirmation listing watch ids,
   deadlines, recipients. Unresolvable thread -> honest failure reply, never
   silence. A message that is not a follow-up request (no trigger keyword, or
   the parser says NOT_A_REQUEST) is left for other handlers (x-transcribe,
   corrections) exactly as they leave mail for us.
2. **Watch (daily, 17:00 Asia/Jerusalem).** For each active watch, read new
   thread messages since last check via app-only Graph. External, non-auto-reply
   messages get a per-ask Claude verdict: ANSWERED closes the watch;
   a human reply that does NOT answer pauses the watch (owner decides);
   auto-replies/OOO/bounces never count. Internal messages are ignored
   (pilot limitation: an owner's own manual chase does not reset the clock).
3. **Act (same run).** A watch still active past its deadline gets a reminder:
   Claude drafts an escalating-tone, status-request-only body; the engine
   `createReplyAll`s the latest thread message IN THE OWNER'S MAILBOX, patches
   body (+ explicit recipients when the instruction named them, keeping
   inherited CC so the prime CRO stays in the loop), and LEAVES IT AS A DRAFT.
   `nudges_sent` increments; the next deadline advances by the watch's
   interval (business days, Mon-Fri). At `max_nudges` the watch is marked
   exhausted and escalated instead of drafted.
4. **Report (same run, one email per owner).** Sent from Sara to the owner:
   every new draft (full text inline + Graph `webLink` to open it in Outlook),
   replies detected, escalations (CC `FOLLOWUP_ALERT_CC`), and every STILL
   UNSENT draft from earlier runs -- repeated daily until sent or cancelled
   (unsent = the draft message still exists with `isDraft`; 404/false = sent
   or deleted, stop listing). Nothing happened and nothing unsent -> no email.

## Commands (deterministic, no LLM)

A reply to Sara cancels a watch on `stop|cancel|done`, or re-arms a paused
watch on `resume|continue|keep`. The command word must LEAD the reply or one
of its lines (an optional short greeting is skipped) unless the body names an
explicit `fw_xxxxxxxx` id, in which case the word may appear anywhere.
Targets: named ids, else all watches registered from that intake
conversation. Confirmed by reply.

## Live gate

`FOLLOWUP_LIVE=1` arms draft creation. Unset (ship state): report-only -- the
daily run classifies, computes, and emails what it WOULD draft (full text),
creating nothing in anyone's mailbox. There is NO auto-send mode at all;
that is parked by team decision and is out of scope for this module.

## State (all on /data, Railway volume)

- `followups.json` -- watch registry. Watch: id (`fw_` + 8 hex), owner,
  mailbox, conversation_id, anchor_message_id, anchor_received, subject, ask,
  recipients[], interval_days, deadline (ISO date), max_nudges, nudges_sent,
  status (`active|paused|answered|exhausted|cancelled`), last_checked,
  latest_message_id, drafts[{message_id, web_link, created, sent}],
  intake_conversation_id, notes[{ts, text}], created, updated.
- `followup_processed.json` -- intake dedup (internetMessageId, last 1000).
- `followup_status.json` -- last run summary for `/followup/status`.

## Endpoints / scheduling / CLI

- `/followup/run` (POST/GET, `?dry_run=1&sync=1`) -- manual daily check.
- `/followup/intake` (POST/GET, `?dry_run=1&sync=1`) -- manual intake scan.
- `/followup/status` -- last run + active watch summaries.
- APScheduler: cron `followup_daily` 17:00 Asia/Jerusalem
  (`FOLLOWUP_HOUR`, default 17); interval `followup_intake` every
  `FOLLOWUP_INTAKE_MINUTES` (default 15). Trigger locks shared with the
  manual routes (x-transcribe pattern). Single-worker topology unchanged.
- CLI: `python followup_engine.py --intake|--check [--dry-run]`.

## Reuse (no duplication)

`email_pipeline_sync as eps`: `MS_GRAPH_BASE`, `graph_get/post/patch/delete`,
`html_to_text`. `x_transcribe_email as xte`: `is_auto_reply`,
`send_threaded_reply` (confirmation/failure replies in Sara's own inbox).
`learn_digest as ld`: `_call_claude_text`. `config`: `is_internal_email`,
`normalize_team_email`. Models: parse/draft `claude-sonnet-4-6`, verdicts
`claude-haiku-4-5-20251001` (env-overridable). Lazily imported by app.py
(route + cron handlers only), never at module load.

## Env vars

`FOLLOWUP_LIVE` (unset = report-only), `FOLLOWUP_HOUR` (17),
`FOLLOWUP_INTAKE_MINUTES` (15), `FOLLOWUP_DEFAULT_BUSINESS_DAYS` (2),
`FOLLOWUP_MAX_NUDGES` (3), `FOLLOWUP_MAX_WATCHES` (100),
`FOLLOWUP_INTAKE_MAX_MESSAGES` (25), `FOLLOWUP_PARSE_MODEL`,
`FOLLOWUP_VERDICT_MODEL`, `FOLLOWUP_DRAFT_MODEL`,
`FOLLOWUP_ALERT_CC` (bk@negevlabs.com). Reuses `BOT_SENDER_EMAIL`,
`CLAUDE_API_KEY`, `MS_GRAPH_*`, `INTERNAL_DOMAINS`.

## Guardrails

- Reminder drafts request status only: no invented facts, commitments, or
  dates; deadlines cited must come from the registered ask.
- Loop safety: Sara's own outbound skipped; external senders never trigger
  intake; auto-replies never trigger anything; per-run message caps.
- Idempotency: processed-ids persisted after EACH handled message; registry
  persisted after each mutation batch; dry runs write nothing.
- GxP-adjacent: communications with CROs on GLP/GMP studies -- conservative
  wording, owner always the sender of record, full audit trail in registry.

## Out of scope (pilot)

Auto-send (parked, team decision). Asana mirroring (Dan: data buildup).
Teams notifications. Meeting-review-page and Asana-section intakes.
Owner's own manual chase resetting the clock.
