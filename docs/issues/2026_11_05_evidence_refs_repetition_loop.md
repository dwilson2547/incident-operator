# An unbounded `evidence_refs` array let the model repeat until the output cap, so incident 60 failed triage with cut-off JSON

**Date:** 2026-11-05  
**Component:** `src/incident_operator/investigate.py` — `FINDINGS`; `src/incident_operator/triage.py` — `Engine._runbook_turn_done`  
**Severity:** Medium — the incident got no analysis (the email went out as failed), but nothing else was affected and it is the only failure in 28 triaged incidents.

---

## Observed symptom

Incident 60 (`ScrapeJobFailing`, `gyopart/parts-direct-acura`) went to `triage_failed` 22 s after it opened, with
"the model's investigation reply wasn't the JSON asked for". It looked like too much log input overflowing the context.

Turn 1 on `qwen35-9b`, worker `ainode`:

| | Tokens | Limit |
|---|---|---|
| Input | 9,053 | 65,536 context |
| Output | 1,536 | 1,536 (`investigate_max_new_tokens`) |

The context was not overflowed: the prompt used about 14% of it. The 1,536 output tokens are the cap, and the reply is
cut off mid-string.

---

## Root cause

### The model fell into a repetition loop inside `evidence_refs`

The reply starts well: `reason`, `where`, `what_failed` and the first nine references are correct (exit 65, Cloudflare
block, shared across `parts-direct` sites). The references then degrade into generated strings, each a small variation
of the last, until the cap:

```
"runbook_interpretation_fix_into_nfs_not_applicable_here_as_not_suspended",
"runbook_interpretation_fix_into_trueNAS_not_applicable_here_as_not_suspended",
...
"runbook_interpretation_fix_into_postgresclientclient_not_applicable_here_as_not_suspended",
"runbook_interpretation_fix_into_cronjob
```

### The schema gives the loop nowhere to stop

Constrained decoding guarantees the shape only while the reply finishes. `evidence_refs` has no `maxItems` and its
strings have no length limit, so the grammar accepts this indefinitely. Temperature is 0, so the loop is deterministic:
replaying the exact prompt looped again.

### A cut-off reply is final

`first_json` returns nothing for the truncated text, and `_runbook_turn_done` sets `triage_failed`. The job counts as
`done`, so the fallback model never runs, and the usable text already produced is discarded. The error message doesn't
say the reply hit the token cap.

---

## Troubleshooting steps taken

1. **Checked prompt size against the context** — 9,053 input tokens of 65,536. Overflow ruled out. A prompt over the
   context would also be refused by llama-server rather than answered.

2. **Read the stored reply** — valid JSON up to the loop, cut off at exactly 1,536 tokens. Output cap confirmed.

3. **Compared other turns** — across 25 investigation turns, the next-longest reply was 563 tokens and the most
   references in a parsed reply was 5. Incidents 59 and 63 had prompts of the same size (about 28k characters) and
   finished normally, so prompt size alone doesn't produce the failure.

4. **Replayed #60's prompt on `qwen35-9b`, 20 jobs** — the exact prompt, then four copies with one harmless reference
   added to the text, each under four variants:

   | Variant | Exact prompt | Reworded (4) | Input tokens |
   |---|---|---|---|
   | Unchanged | looped, 1,536 out | 4 of 4 fine | ~9,050 |
   | Adjacent-line dedupe | fine, 358 out | 4 of 4 fine | ~4,610 |
   | `evidence_refs` `maxItems: 8` | fine, 440 out | 4 of 4 fine | ~9,050 |
   | Both | fine, 358 out | 4 of 4 fine | ~4,610 |

   The loop is rare and sensitive to the exact prompt: one added reference avoided it every time. This doesn't show
   dedupe prevents loops, because any change to the prompt avoided this one. `maxItems` did stop it on the failing
   prompt, and it bounds this array whatever the prompt.

5. **Measured adjacent-line dedupe on stored `scrape-last-run` evidence** — 8 of the 13 non-empty results shrank by
   62–86% (#60: 17,885 → 3,888 characters); the other 5 had no repeats.

---

## Fix

_TBD_ — not applied yet. Proposed:

### `investigate.py` — bound `evidence_refs`

Add `"maxItems": 8` to `FINDINGS`. Tested on the failing prompt. A loop inside a free-text string would still be possible.

### `triage.py` — treat a cut-off reply as its own case

When `output_tokens` reaches the cap and the reply doesn't parse, retry once on the fallback model or with a changed
prompt, since the same prompt at temperature 0 loops again. Say "hit the token cap" in the error.

### Evidence step — dedupe log lines

Collapse adjacent identical lines to `(n) line`, before the character cap so the budget reaches further back. This is
worthwhile for prompt size and speed (see `TODO.md`), but it isn't the fix for this failure.

---

## Files changed

_TBD_ — none yet.
