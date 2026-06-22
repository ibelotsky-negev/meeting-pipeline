# FYI Triage -- Gated Rollout (A -> B -> C -> D)

FYI Triage surfaces important mail buried in two high-volume auto-filed Outlook
folders -- **"4: notification"** and **"8: marketing"** -- by **moving** the
important messages into **"2: FYI"**. Because it writes to Ken's live mailbox,
it ships behind a dual gate and rolls out as a **state machine where Ken is the
reviewer at every transition**. No state self-advances; each ends at a Ken gate.

Module: `fyi_triage.py`. Endpoints: `/fyi/run`, `/fyi/status`. Cron: daily 06:00
Asia/Jerusalem (`fyi_triage_daily`). Classifier: Sonnet, one call per message,
reads the body. See CLAUDE.md "FYI Triage Module" for the full map.

## The two gates (why a move is impossible until Ken says so)

A real move happens **only when BOTH are true**:

1. the `?live=1` request flag (the daily cron passes `live=True` internally), AND
2. the **`FYI_LIVE=1`** environment variable on Railway.

Absent either, the run is **dry** regardless of the window: it classifies and
logs what it *would* move, writes no dedup ids, and moves nothing. This deploy
ships with **`FYI_LIVE` unset**, so the module **cannot move mail** until Ken
sets it. The only transition that turns on real moves is **GATE B PASS -> Ken
setting `FYI_LIVE=1` in STATE C.**

## Lookback windows (always a parameter, never a fixed date)

| Use | Window | Trigger |
|-----|--------|---------|
| Daily cron (steady state) | 24h (`FYI_LOOKBACK_HOURS`) | automatic |
| Calibration dry-run (STATE B) | 7 days | `/fyi/run?days=7` (dry by default) |
| One-time backfill (STATE C) | 30 days | `/fyi/run?days=30&live=1` (needs `FYI_LIVE=1`) |

A window beyond 30 days must be passed explicitly; the module never silently
scans the full ~13k backlog.

---

## STATE A -- SHIP (dry, gates off)   [done this session]

Deployed `2.19.0-fyi-triage` with `FYI_LIVE` unset. The daily cron runs dry and
emails a "would move N" summary each morning. A dry `/fyi/run?days=7` returns a
would-move list.

**GATE A (Ken):** trigger the 7-day dry-run yourself:

```
curl "https://meeting-pipeline-production.up.railway.app/fyi/run?days=7"
# then read the would-move list + per-message reasons:
curl "https://meeting-pipeline-production.up.railway.app/fyi/status" | python -m json.tool
```

(`/fyi/run` launches a background run and returns `{"status":"started"}`. Poll
`/fyi/status` -- `live_progress` is the heartbeat; when `phase` is `done`, the
`decisions[]` array holds every message with its IMPORTANT/NOISE verdict and
reason. Add `&sync=true` to get the result inline instead.)

---

## STATE B -- 7-DAY DRY-RUN REVIEW   [Ken reviews precision]

Ken (or a review agent he points at the `decisions[]` list) inspects every
proposed move:
- Is each **IMPORTANT** truly important? (false positives -- the expensive kind)
- Did any **NOISE** that should have moved get left behind? (false negatives)

**GATE B decision:**
- **PASS** (precision acceptable) -> advance to STATE C.
- **FAIL** (misclassifications) -> **LOOP BACK through the BUILD LOOP**: Ken tells
  the build session the specific subjects + the correct labels; the session adds
  each as a **new permanent test case** in `tests/test_fyi_triage.py`
  (test-first), fixes the classifier rubric/logic, reruns the build loop
  (check.py green), redeploys, and re-presents a fresh 7-day dry-run. Repeat
  until PASS. **Every correction becomes a permanent test -- the rubric only ever
  tightens. Precision is a ratchet.**

---

## STATE C -- GO LIVE + 30-DAY BACKFILL   [Ken flips the gate]

1. Ken sets **`FYI_LIVE=1`** in the Railway service variables (and lets the
   service restart / pick it up).
2. Ken runs the one-time backfill **once**:

```
curl "https://meeting-pipeline-production.up.railway.app/fyi/run?days=30&live=1"
```

Because STATE B wrote **no** processed-ids, the backfill sees the full 30 days
and handles each message once. The backfill may take a while (it classifies
every in-window message); the run heartbeats its lock so the daily 06:00 cron
correctly sees a run in progress and skips rather than starting a second
concurrent live run.

**GATE C (Ken):** spot-check the live "2: FYI" folder (count + the moved list in
`/fyi/status`).
- **PASS** -> advance to STATE D.
- **FAIL** (something moved that shouldn't have) -> Ken **manually moves it back**
  (the module never deletes and cannot auto-reverse a move -- only Ken moves mail
  back). The build session treats each wrong move as a **new failing test**,
  fixes, reruns the build loop, redeploys; Ken re-runs a **bounded** live window
  (e.g. `?days=2&live=1`) to confirm before resuming.

---

## STATE D -- STEADY STATE   [daily cron, live]

With `FYI_LIVE=1` set, the daily **06:00 Asia/Jerusalem** cron runs at the 24h
window, live. Dedup guarantees no overlap with the backfill. Each morning Ken
gets a short "moved N" summary email.

This is the final state. Ongoing misclassifications are handled in a future
session as new test cases (the rubric loop never closes), but the build session
is done.

---

## Rollout invariants

- **No state self-advances.** Each state ends by reporting to Ken and stopping
  for his go/no-go.
- **The ONLY transition that enables real moves is GATE B PASS -> Ken setting
  `FYI_LIVE=1` in STATE C.** Until then the dual gate makes moves impossible by
  construction.
- **Every FAIL loops back through the BUILD LOOP** with the misclassification
  captured as a permanent test *before* any redeploy. Precision is a ratchet --
  the rubric only tightens, tests are never weakened to pass.
- **Action-safety:** the module only ever moves FROM the two named sources TO
  "2: FYI"; it never deletes or modifies anything else; a moved message is
  recorded by id and never re-touched (idempotent).
