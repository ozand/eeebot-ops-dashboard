# ADR-002: The Agent Context Page Is a Reader — One Source per Figure, and Every Count Carries Its Read Status

- **Status**: Proposed (filed with Issues #260 and #261, ahead of implementation)
- **Date**: 2026-09-14
- **Deciders**: @ozand
- **Consulted**: Issue #227 (two-tier model), #241 (corpus states), #1471/#1482 (prompt-fit telemetry), `ozand/eeebot` #1173, #1379, #1447, #1563

---

## Context

`agent.html` exists to answer one question: what is the agent's context made of, and what does each part cost. The runtime half of that question is answered honestly and completely. `ContextBuilder.build_system_prompt` assembles five sections in a fixed order, joins the non-empty ones with `SECTION_SEPARATOR`, and fits them under `MAX_SYSTEM_PROMPT_CHARS = 24000`; the operator charter is appended *after* the fit and is deliberately outside both the cap and the recorded totals (#1379). Every figure the page needs is written to the `phase: system_prompt` ledger row on every spawn: per-section chars, the fit rung, dropped and trimmed sections, the catalogue's load/bound evidence including named omissions (#1563), and the memory index's four-state status with matched and missing resident labels (#1447).

The render half has drifted from it. Four separate issues each added a reader over the same ledger rows, and the page now shows:

1. Tier 2 as `missing` for skills, lessons and memory, because the instance repository path is resolved in one place and the unresolved argument is passed to the corpus reader in another (`techtree_viewer.py:1327` vs `:1464`).
2. A fabricated `0` in the KPI and the Tier 1 link badges ten lines from the honest `corpus_count()` that #241 introduced for the cards.
3. Two different separator counts on one screen — a hardcoded `4 × 7 = 28` in the Tier 1 column against a computed `3 × 7 = 21` in the reconciliation table, where only the computed one reconciles with the recorded `chars`.
4. `goals` labelled `absent (not configured/emitted)` while the same page prints the charter below it.
5. `cap = sys_prompt.get("cap", 30000)` — a default that exists nowhere in the runtime, and that every percentage on the page would be computed from.
6. `skills_catalogue` and `memory_index` unread entirely, so a truncated catalogue and a complete one render identically.

These are not six unrelated bugs. Items 2, 3 and 5 are the same mechanism: the page holds its own copy of something the assembler owns — a count, a structural constant, a default — and the two copies drift. Items 1 and 6 are the other half of the same mechanism: where the page has no copy and no read, it prints the value a healthy empty system would have.

This is the fourth instrument in sequence to report a number that answers a different question than the one its label asks: Tier 2 reported an empty corpus against a populated instance repository (`git ls-tree -r --name-only origin/main | grep -c 'SKILL\.md$'` returned 40 on 2026-09-14, with 81 `lessons/*.md` and 62 files under `memory/`); `lessons.html` reported `v2 Entries: 0` against 41 v2 rows; `hypotheses.html` reports attempt budgets instead of verdicts; this page reports a missing corpus as an empty one and its own arithmetic twice.

---

## Decision

**The agent context page renders what the runtime recorded. It does not recompute, does not default, and does not omit.** Four rules, all render-side; no runtime change is required by this record.

### 1. One computation site per figure

Every displayed figure is computed once. A second appearance of the same figure is an anchor or a reference to the first, never an independent derivation, and never a literal. Structural constants the assembler owns — the separator count, the section list and its order, the cap — are read from the record or from the assembler's own published constants, never restated in the renderer.

This rule is what makes finding 3 impossible rather than fixed: two derivations of one number will eventually disagree, and the page has no way to tell the reader which one to believe.

### 2. A count derived from a read states the read's status

Every count that comes from reading a corpus, a file or a ledger key carries one of `present` / `missing` / `unavailable` / `empty`, and renders that status wherever the count renders — KPI, badge, card and table alike. A failed or unattempted read never renders as `0`, and a missing key never renders as a default constant.

This extends the `ozand/eeebot` #1173 four-state reader contract from the state readers to the render layer. #241 applied it to the Tier 2 cards; the gap this rule closes is that it was applied to *some* display sites of the same value.

### 3. A recorded figure is rendered or declared unrendered

A key the runtime writes to the `system_prompt` row is either displayed or listed, on the page, as recorded-but-not-shown. Silence is not an option, because a telemetry key that no surface reads is indistinguishable from one that is never written — which is the reason #1447 recorded `resident_missing` in the first place.

### 4. Outside the cap is reported as outside the cap

A section the assembler deliberately places beyond the budget is labelled as such, with its size, and is never reported as absent, missing or zero. The page reports two budget figures: what the cap governs, and what the model actually receives.

---

## Consequences

### What gets easier

The page becomes checkable. Each figure has one owner, so a disagreement is a test failure rather than a reader's judgement call about which of two numbers to trust. The reconciliation table stops being a second opinion and becomes the single source the rest of the page anchors to.

Rule 3 turns the runtime's existing telemetry into the page's backlog: `skills_catalogue` and `memory_index` are already written and already complete, so the work in #261 is reading, not instrumenting. It also makes the next such gap visible on the day the runtime adds a key, instead of on the day someone audits the page.

Rule 2 makes the difference between "the loop has no skills" and "the dashboard cannot see the skills" legible at every display site, which is the difference #260 exists to fix.

### What gets harder

One source per figure means the layout cannot casually repeat a number for emphasis; repetition now has to be an anchor, which costs markup. Rule 3 means adding a runtime telemetry key creates a render obligation, and a page that must show everything recorded will grow — the offsetting discipline is that a key nobody displays should be questioned on the runtime side rather than quietly dropped here.

Rules 1 and 4 both point at a layout the current page does not have: figures grouped by what they describe rather than by which issue added them. That recomposition is deliberately **not** part of this decision; it is the follow-up once #260 and #261 have landed, so the layout is built once against the final set of blocks.

### What does not change

The two-tier model itself (#227) and its Tier 1 / Tier 2 vocabulary. The assembly order, the cap, `_bound_skills_catalogue`'s budget derivation, the compaction constants, and the placement of the charter outside the cap. The prompt-fit event block (#1482) and the scorecard-published source behind it. Nothing in `ozand/eeebot`.

---

## Alternatives considered

- **Fix the corpus plumbing and stop there.** Rejected. It is two lines and it is the most visible defect, but the fabricated `0` in the KPI, the hardcoded separator count and the `30000` default all survive it, and all three are the same class. #241 is the precedent: it fixed honesty in the Tier 2 cards and left the identical fabrication ten lines away in the KPI, because the fix was scoped to a symptom rather than to the rule.

- **Compute the missing figures in the dashboard instead of reading them.** Rejected. The renderer would become a second implementation of the assembler, and finding 3 is what that already costs at a scale of one hardcoded integer. The assembler's arithmetic is recorded per cycle precisely so no other surface has to repeat it.

- **Move the render back into `ozand/eeebot`, next to the assembler.** Rejected. `ozand/eeebot#617` made this repository the canonical home, and the coupling being complained about is a data contract, not a code-locality problem. The right fix is to read the recorded contract, not to move the reader next to the writer.

- **Recompose the page first, then fix the figures inside the new layout.** Rejected as ordering, not as substance. The window and catalogue blocks in #261 change what the page contains; laying it out before they exist means laying it out twice, and the honest-status work in #260 has no verifiable target until Tier 2 is reachable.

- **Render an aggregate "context health" verdict instead of the individual figures.** Rejected. The operator's stated purpose for this page is per-block detail; an aggregate would compress exactly the distinctions the four rules exist to preserve, and a green verdict computed over an unread corpus is the failure mode this whole record is about.

---

## Test Contract

- Two fixtures, one with a readable corpus and one without: every display site of a corpus count — KPI, Tier 1 badges, Tier 2 cards, deep-section headers — renders the status in the second, and none renders `0`.
- A fixture whose `active_skills` is `0` and one where it is non-empty both reconcile: the single separator figure equals `max(0, non_empty - 1) × len(SECTION_SEPARATOR)` and the section sum plus separators equals the recorded `chars`.
- A `system_prompt` row with no `cap` renders `unavailable`, and no percentage or bar width is computed for that row.
- A fixture with the charter tail present reports both the capped total and the full system-message total, and `goals` is labelled outside-the-cap with a size rather than `absent`.
- A fixture whose `skills_catalogue.truncated` is true renders every `omitted_names` entry; one whose `memory_index.resident_missing` is non-empty renders every label by name; an absent key in either renders `unavailable`, not an empty list.
- An assertion that no figure displayed more than once on the page has more than one computation site — enforced on the renderer, by construction of the template, not by inspection.

---

## References

- #260 — the honesty and self-consistency issue (findings 1–5 above).
- #261 — the unread-telemetry issue (finding 6, plus the dialogue window).
- #227 — the two-tier agent context model this page implements.
- #241 — Tier 2 corpus states; the four-state contract this record generalises to every display site.
- #1482 / #1471 — published prompt-fit events and the fit rung.
- `ozand/eeebot#1173` — empty is not unavailable; the reader contract.
- `ozand/eeebot#1379` — the ledger row describes the capped builder prompt, not the operator-owned tail.
- `ozand/eeebot#1447` — `memory_index`: status, `resident_matched`, `resident_missing`.
- `ozand/eeebot#1563` — `_bound_skills_catalogue` and its named omissions.
- `nanobot/agent/context.py` — assembly order, `SECTION_SEPARATOR`, `MAX_SYSTEM_PROMPT_CHARS`.
- `nanobot/agent/subagent.py:746-751` — `system_context` appended after the fit.
- `nanobot/runtime/context_compaction.py` — `WINDOW_TOKENS`, `RESERVE_TOKENS`, `KEEP_RESULTS`, the journal's write condition.
