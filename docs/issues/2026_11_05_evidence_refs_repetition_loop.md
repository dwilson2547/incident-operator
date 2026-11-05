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

### `investigate.py` — `FINDINGS`: bound `evidence_refs`

`"maxItems": 8`. #60's exact prompt now finishes in 440 tokens on the deployed schema. A loop inside a free-text string
is still possible; the retry below is what handles that.

### `triage.py` — `Engine._runbook_turn_done`: a reply at the cap gets one retry

If the reply doesn't parse and `output_tokens` reached `investigate_max_new_tokens`, the same turn is submitted once
more, with a note appended (`investigate.retry_messages`). The note is needed because the identical prompt at
temperature 0 loops identically: with the note, #60's prompt on the old unbounded schema finished in 344 tokens. A
second reply at the cap fails with "hit the 1536-token limit again on retry"; other unparseable replies keep the
"wasn't the JSON" error. Retries share a turn number, so the page and email count distinct turns.

### `evidence.py` — `collapse_repeats`: adjacent identical log lines become `(n) line`

Applied before the character cap in `evidence._logs` and `ops.Runner._logs`, so the budget reaches further back. The
primer explains `(n)`. This shrinks prompts; it isn't the fix for this failure. Stored evidence holds the collapsed
text, not the raw lines.

Replayed the first turn of seven stored scraper incidents (24, 25, 27, 42, 59, 60, 63) raw and collapsed, with the
bounded schema: all 14 finished at high confidence with the same cause, and input fell from 7,483–9,053 tokens to
3,480–4,697. Those stored logs had already been capped, so this measures the same text made shorter, not the extra
history a real run can now reach.

---

## Files changed

- `src/incident_operator/investigate.py` — `FINDINGS`, `retry_messages`, `PROMPT_VERSION`
- `src/incident_operator/triage.py` — `Engine._runbook_turn_done`
- `src/incident_operator/notify.py` — `compose` (turn count)
- `src/incident_operator/static/index.html` — turn count
- `src/incident_operator/evidence.py` — `collapse_repeats`, `_logs`
- `src/incident_operator/ops.py` — `Runner._logs`
- `helm/incident-operator/files/primer.md` — what `(n)` means
- `tests/test_runbooks.py`, `tests/test_evidence.py`
