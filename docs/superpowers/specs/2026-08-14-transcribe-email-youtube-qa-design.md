# Transcribe-by-email: YouTube support + questions about the video

Date: 2026-08-14
Status: approved design, not yet implemented
Target version: `2.28.0-transcribe-qa`
Module: [x_transcribe_email.py](../../../x_transcribe_email.py)

## Context

Vadim Uzberg asked for access to the video-transcription capability without
having to route requests through Ken. The deployed `x-transcribe-email` module
already delivers most of this: email Sara a link, get back a structured summary
plus the full transcript as a `.md` attachment, on a 15-minute inbox scan.

Two gaps separate what exists from what was promised:

1. It only handles x.com / twitter.com links. YouTube was promised.
2. There is no way to ask anything about a video -- you get one fixed summary
   shape and nothing else.

Vadim is being onboarded at `vu@negevcap.com`. `negevcap.com` is already in
`INTERNAL_DOMAINS` ([config.py](../../../config.py)), so he is internal by the
existing gate and no eligibility work is required for him.

## Scope

In scope:

- YouTube links: captions first, Grok STT fallback.
- Spotify / podcast links: detected and honestly reported as unsupported.
- A question asked in the same email as the link, answered from the transcript.
- Follow-up questions asked by replying in-thread, answered from a cached
  transcript with no re-transcription.
- Loop and cost guards for the new "Sara answers link-less mail" behavior.

Explicit non-goals:

- Spotify transcription. Spotify audio is DRM'd; yt-dlp cannot fetch it and the
  Web API exposes only a 30-second preview. The existing `_fetch_spoken`
  resolver in [learn_digest.py](../../../learn_digest.py) has no key set and its
  request shape was never validated against a live API. Deferred to its own
  piece of work; until then the reply says so.
- Telegram as a second front door.
- A `Zira@zirmania.com` mailbox. Zira is reserved for Zirmania family-office
  reporting and is not this product.
- An external-guest tier with spend caps. Vadim is internal, so this has no
  user; adding it would be speculative.
- Any change to `config.is_internal_email` or other shared config helpers.
  Widening that function would leak into FYI Triage's internal-domain NOISE
  guard and Pulse's team detection.

## Eligibility

The sender gate at [x_transcribe_email.py:382](../../../x_transcribe_email.py)
keeps its existing rule and gains one extension list:

- `config.is_internal_email(sender)` -> served. Covers all five internal
  domains, so the whole team plus `vu@negevcap.com` qualifies.
- `sender.lower() in XTE_TEAM_EXTRA` -> served. New comma-separated env list,
  lowercased and stripped on read, **empty by default**. Exists so a personal
  address (e.g. `ibelotsky@gmail.com`) can be added without a deploy.
- Sara's own address -> skipped. Existing loop guard, now load-bearing because
  Sara replies in-thread and would otherwise read her own reply as a follow-up.
- Anyone else -> ignored and marked processed, so it is not reconsidered every
  run. Existing behavior.

## Sources

`find_x_links` generalizes to `find_media_links(body_html) -> [(url, kind)]`,
with `kind` from `learn_digest.classify_url`. Existing behavior preserved:
links read from `uniqueBody` only (a quoted link in thread history never
re-fires), de-duplicated, order preserved, `_X_NONPOST` filter still drops bare
domains and navigation pages, and `XTE_MAX_LINKS` (default 5) still caps one
email.

| kind | Behavior |
|---|---|
| `x` | Transcribed. Unchanged path: `ld.extract_x_post_audio` -> `ld._grok_stt_from_file`. |
| `youtube` | Transcribed. `ld._fetch_youtube_transcript` first; if it returns nothing, the same yt-dlp + Grok STT path as `x`. |
| `podcast` | Returned by the detector, never transcribed. Reported in the reply as not yet supported. |
| `article` / anything else | Not returned at all. A message carrying only article links is not a transcription request. |

A message carrying **only** podcast links still gets a reply saying podcasts are
not supported yet. Silence there would read as a broken service and send the
sender to Ken, which is the outcome this whole feature exists to avoid.

`extract_x_post_audio` at [learn_digest.py:840](../../../learn_digest.py) is a
generic yt-dlp wrapper despite its name -- it takes any URL and enforces the
`LEARN_STT_MAX_DURATION_SEC` (60 min) and `LEARN_STT_MAX_BYTES` caps. It needs
no changes to serve YouTube. Captions-first matters for cost and latency: most
YouTube videos have captions, so the common case makes no STT call at all.

`transcribe_link` takes a `kind` argument and dispatches accordingly. A
`podcast` returns `ok=False` with a specific reason and calls no resolver.
Temp-directory cleanup is unchanged.

## Questions about the video

### In the first email

`extract_note(body_html) -> str` runs `eps.html_to_text` over `uniqueBody`,
removes every URL that was detected, collapses whitespace, and truncates to
2000 characters.

If the note is non-empty it is passed to the summarizer as the requester's
note. The prompt instructs the model to answer it first from the transcript,
then give the standard summary; to ignore the note if it is not a question; and
to state plainly that the video does not cover it rather than speculating.

Deliberately no regex or heuristic decides "is this a question" -- forwarded-mail
boilerplate would fool one. The model judges, and an unhelpful note degrades to
the normal summary.

If the note is empty, the existing summary prompt is used unchanged.

### In replies

A new store caches transcripts per conversation so a follow-up costs one Claude
call and no re-transcription.

The message gate at [x_transcribe_email.py:380](../../../x_transcribe_email.py)
currently drops any message with no supported links. It becomes: no supported
links **and** the `conversationId` is in the thread cache **and** the sender is
eligible **and** the message is not auto-submitted **and** the per-conversation
question count is under its cap -> handle as a follow-up question. Anything
else still falls through to the existing skip.

The question is `extract_note` of `uniqueBody`, so quoted history is excluded.
An empty question is skipped. The answer is grounded strictly in the cached
transcripts for that conversation; if they do not contain the answer, the reply
says so. No attachment is resent. The question counter increments and the
message is marked processed.

Any eligible sender in the conversation may ask, not only whoever sent the
original link -- if a colleague was copied on the thread, the transcript is
already shared with them and refusing them would be arbitrary.

### Threading (load-bearing)

Follow-ups only work if Sara's replies stay in the same Exchange conversation,
so `send_reply` moves off `sendMail`:

- **First reply** (carries `.md` attachments): `POST /messages/{id}/createReply`
  -> `PATCH` the draft body -> `POST` each attachment to the draft -> `POST
  /send`. Inherits `conversationId`, subject and recipient.
- **Follow-up reply** (no attachments): `POST /messages/{id}/reply` with a
  `comment`. Single call.

This replaces a working deployed path, so it is covered by tests. It is also a
correctness improvement on its own -- today's `sendMail` reply threads only by
subject heuristics.

## State

New store at `/data/x_transcribe_threads.json`, alongside the existing
`x_transcribe_email.json` and `x_transcribe_email_status.json`:

```
{ "<conversationId>": {
    "created_at": "<iso>",
    "updated_at": "<iso>",
    "questions": 0,
    "links": [ {"url": ..., "title": ..., "transcript": ...} ]
} }
```

Written after a first reply succeeds **and at least one link transcribed
successfully** -- a conversation where everything failed has nothing to answer
questions from, so it is not cached and a reply to it falls through to the
existing skip. Evicted by `XTE_THREAD_TTL_DAYS` (default
30) and by `XTE_THREAD_MAX` (default 200 conversations, oldest `updated_at`
first). Resolver-side caps already hold transcripts near 20k characters, so the
worst case is single-digit megabytes.

## Safety guards

- **Auto-responder loop breaker.** Sara now answers link-less mail in threads
  she owns, so an out-of-office or ticketing auto-reply could ping-pong with her
  indefinitely. Two defenses: skip any message with `Auto-Submitted` (other than
  `no`), `X-Autoreply`, `X-Autorespond`, or `Precedence: auto_reply|bulk|junk`
  (requires adding `internetMessageHeaders` to the Graph `$select`); and cap
  follow-ups per conversation at `XTE_THREAD_MAX_QUESTIONS` (default 20). On
  hitting the cap Sara stops replying **silently** -- a "you hit the limit"
  reply would itself feed the loop.
- **No usage cap.** Per Ken, no per-sender daily limit. The per-conversation cap
  above is a loop breaker, not a usage limit. The pre-existing `XTE_MAX_LINKS`
  (5 per email) is unchanged.
- **No fabrication.** Every failure is reported with its specific reason: an
  unfetchable X post, a YouTube video with neither captions nor downloadable
  audio, a video over the 60-minute cap, a Spotify link. Unchanged discipline.
- **No collision with corrections ingest.** `sara_corrections.py` also reads
  Sara's mailbox for replies to pulse and biweekly emails. No conflict: xte only
  treats a link-less message as a follow-up when its `conversationId` is already
  in the transcript cache, which a pulse reply's never is.

## Reply format

- **First reply**: per-link summary in the body (TITLE / TL;DR / KEY POINTS /
  NOTABLE QUOTES), preceded by the answer when a question was asked; one `.md`
  transcript attached per successful link; failures named with reasons.
- **Follow-up reply**: answer only, in-thread, no attachment.
- **Footer on both**: one line naming what is supported (X and YouTube), that
  replying asks a question about the video, and that Spotify is not supported
  yet. Applied to every reply -- there is no guest/team distinction to condition
  on, and the team equally does not know follow-ups now exist.

## Testing

Offline only, mocking Graph, Claude and the resolvers. Ships in the same commit.

1. **Eligibility**: internal domain served; `XTE_TEAM_EXTRA` address served;
   stranger ignored and marked processed; Sara's own mail skipped.
2. **`find_media_links`**: x and youtube returned with correct kind; podcast
   returned as `podcast`; article omitted entirely; `_X_NONPOST` still filtered;
   de-dup and order preserved.
3. **Dispatch**: YouTube uses captions when present; YouTube falls back to STT
   when captions are empty; x path unchanged; podcast returns unsupported
   without calling any resolver; a podcast-only message still gets a reply.
4. **`extract_note`**: URLs stripped; link-only body yields empty note; long
   body truncated.
5. **First reply**: question present -> answer-first path; absent -> summary
   only; one attachment per successful link; footer present.
6. **Thread cache**: written after a successful reply; **not** written when every
   link failed; TTL eviction; max-entry eviction by oldest `updated_at`.
7. **Follow-up**: link-less mail in a known conversation from an eligible sender
   is answered; unknown conversation ignored; question cap reached -> no reply
   at all; auto-submitted header -> skipped.
8. **Threading**: first reply uses the createReply/attach/send sequence;
   follow-up uses the single-call reply endpoint.

## Deploy

No `app.py` logic changes -- routes (`/transcribe-email/run`, `/transcribe-email/status`)
and the 15-minute APScheduler job already exist and are already named
generically. `app.py` is touched only for the version string.

1. Bump `2.28.0-transcribe-qa` in **both** `/version` and `/test`, preserving
   CRLF.
2. Fresh `CACHEBUST`.
3. Update CLAUDE.md in the same commit: the x-transcribe-email module section
   (sources, Q&A, new state file, new env vars) and any new Common Failure Modes
   rows.
4. Commit, push, poll `/version` for this exact version.

New env vars, all defaulted so nothing must be set on Railway to ship:
`XTE_TEAM_EXTRA` (empty), `XTE_THREAD_TTL_DAYS` (30), `XTE_THREAD_MAX` (200),
`XTE_THREAD_MAX_QUESTIONS` (20).

## Prerequisites

- The `vu@negevcap.com` mailbox must exist and be able to send before Vadim can
  use this.
- Verify no Graph `ApplicationAccessPolicy` restricts Sara's app registration in
  a way that blocks the reply endpoints.

## Known consequence

`negevcap.com` is also in FYI Triage's internal-domain NOISE guard
([fyi_triage.py:115](../../../fyi_triage.py)), so mail from `vu@negevcap.com`
landing in the auto-filed notification or marketing folders is deterministically
classified NOISE. Direct mail to Ken does not land in those folders, so this is
a footnote rather than a problem -- recorded here so it is not rediscovered as a
bug later.

## Amendment -- 2026-08-15: container URLs are not requests

This supersedes the Sources section above on one point. That section kept the
`_X_NONPOST` filter as the only detection-time exclusion, i.e. ANY other X link
was returned "so a link with no video still earns an honest no-video reply".
That decision is REVERSED.

A CONTAINER url -- one naming a channel, profile, playlist or show rather than a
single item -- now produces no entry at all from `find_media_links`, exactly like
an article URL: an X path with no `/status/<id>`, a YouTube URL with no parseable
video id, and a podcast `/show/` or `/playlist/` page. A podcast `/episode/` URL
is an item and still earns the honest "not supported yet" reply.

Why: the original rationale assumed the only way to reach Sara was to
deliberately email her a link. As built, ordinary mail and follow-up questions
flow through the same gate, and Sara's inbox is shared with
`sara_corrections.py`, so team replies to Pulse/biweekly/digest reports are
routine traffic in the same 25-message window. A container URL in an email
signature therefore dropped the follow-up question (the headline feature of this
design silently failed) and earned an unsolicited reply on ordinary mail. The
already-cached-link gate added later cannot rescue it: a container URL can never
be cached -- a channel cannot transcribe, a profile has no video, a show is
always unsupported -- so it could never self-heal. The cost of losing an honest
reply to someone who deliberately pasted a profile URL is smaller than the cost
of unsolicited replies and dropped questions.

Consequence for YouTube: `ld._youtube_video_id` matches only `youtu.be/`, `v=`,
`/shorts/` and `/embed/`, so `youtube.com/live/<id>` and `youtube.com/v/<id>`
resolve to no id, and dropping every id-less YouTube URL would have silently
swallowed two forms that transcribed fine before this branch. `learn_digest` is
deployed and shared with the Read/Learn digest, so its regex was NOT widened;
`x_transcribe_email._youtube_id` delegates to it and additionally recognizes
those two forms, and is the module's single notion of "the video id".

## Amendment 2 -- 2026-08-15: narrowing "container", and the follow-up gate

This NARROWS the 2026-08-15 amendment above on two points. It does not reopen
it: a channel, profile, nav page, Spotify show or playlist still produces no
entry at all.

### Three ITEM shapes were swept up

`x.com/i/spaces/<id>`, `x.com/i/broadcasts/<id>` and `youtube.com/clip/<id>`
name single ITEMS, not containers, but the blanket rules ("an X path with no
`/status/<id>`", "a YouTube URL with no parseable video id") dropped all three.
They produced no entry, so `run()` hit a bare `continue` and the sender got
TOTAL SILENCE -- where before this branch they reached the resolvers and
produced either a transcript or an honest capped/failed reply. That contradicts
the module's own principle: a Spotify episode, which can NEVER transcribe, gets
an honest reply, while an X Space, which might actually work, got nothing.

They are items now. `_x_item_id` is the single notion of "this X url names one
item" (post status id, or a Space / broadcast id) and backs both the container
check and the attachment name. A clip is kept SEPARATE from a watch id
(`_youtube_clip_id`): a clip URL does not carry the underlying watch id at all,
so `_youtube_id` can never resolve one, captions cannot be looked up (they are
keyed by watch id, so that lookup returns None first) and yt-dlp fetches the
clip itself. The clip id is namespaced wherever it is an identity --
`youtube:clip:<id>` in `_link_key`, `clip_<id>` in the attachment filename --
so it can never collide with a watch id made of the same characters.

### A podcast link never counts as a new transcription request

The podcast container rule (`/show/`, `/playlist/`) is Spotify path-shaped, so
a SHOW page on Apple, anchor.fm, pod.link, overcast, castbox or podbean is still
detected as an item, as is any Spotify `/episode/`. A podcast result is never
`ok`, so `remember_thread` can never cache it: while such a link counted as a
request, `_has_new_link` stayed True forever and EVERY follow-up question in
that conversation was dropped in favour of a repeat "not supported yet" reply --
the original signature-link defect surviving in a narrow lane.

Fixed at the GATE, not at the host regex: only a TRANSCRIBABLE kind (`x`,
`youtube`) can make a message count as a new request in a conversation Sara has
already transcribed. A regex enumerating podcast hosts would rot with every new
host, and widening it would ALSO have silenced podcast-only mail, which is the
opposite of the principle above. Detection is untouched: a podcast link is still
an item, and with no cached conversation the follow-up branch is not taken at
all, so a podcast-only email still earns its honest unsupported reply. Ordering
is load-bearing -- the transcribable filter runs INSIDE the gate, after the
"no cached entry -> this is a request" exit.
