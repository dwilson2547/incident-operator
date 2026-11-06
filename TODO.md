# incident-operator backlog

## Next

- [ ] **Triage a group of alerts as one** (user, 2026-11-05; future step).
  When several alerts land together, triage one, attach the others, send one email, instead of one model run and one
  email each. Today each is triaged alone and sees the others through the context block (design §6.7). Worth doing if
  that block shows the model naming shared causes reliably. Needs a rule for what counts as a group, because nearness
  in time alone is a poor one.

- [ ] **Run-now and resume as one-click fixes** (user, 2026-11-05: discuss before building).
  For a failed scheduled job, and a paused job. The runbooks describe these as manual steps today. They need a new
  annotate verb in the operation kinds and matching rights for the executor, so the scope is the thing to talk through.

- [ ] **Shrink scraper logs further, if prompts still hurt** (idea 2026-11-02, from the Qwen3.8 27B replay).
  Adjacent repeats collapse to `(n) line` since 2026-11-05, which halved the scraper prompts replayed (~8.7k to ~4.3k
  tokens). Not done: collapsing lines that differ only by a counter or URL, keeping error lines with context while
  trimming the rest, a small-model extraction pass. `LOG_LINES` is still 300, so collapsing frees budget that isn't
  used yet; raising it would reach further back and give some of the savings back. Earlier measurements:
  `ai/gpu-scheduler/docs/remote-worker-mac.md` "Big-model replay".

- [ ] **Ignore timestamps when collapsing repeated log lines** (user, 2026-11-05; do it when it bites).
  `collapse_repeats` only matches exact repeats. That works for the scrapers, whose lines carry no timestamp, but most
  logs start with one, so every line looks unique and nothing collapses. Compare lines with a leading timestamp
  stripped, and keep one timestamp on the collapsed line. Sign it's needed: a timestamped service's logs filling the
  character cap with near-identical lines.
