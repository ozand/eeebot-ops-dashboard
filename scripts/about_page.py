"""The 'how the project is organized' page (issue #273).

Split into its own module deliberately: `techtree_viewer.py` is the file
issue #278 is splitting for size, and this page's content has nothing to
do with live state rendering, so it does not belong in that file's growth
at all. `techtree_viewer.py` only imports :func:`build_about_panel` and
registers ``about.html`` in its site-page list and page dict -- see the
comment marked "#273" in that file for the two-line registration.

Unlike every other page on the site, this one describes STRUCTURE, not
STATE: which repositories exist, what runs on a schedule, what a cycle is,
which model serves which role, and the vocabulary the other six pages
already use. None of that depends on the live SSH state fetch
(`data` is accepted for signature symmetry with the other `build_*_panel`
functions and to read `host`/`generated_at`, but nothing here reads
`data.get(...)` for its factual content) -- so a fetch failure that would
blank a state-driven page must not blank this one, and does not.

Every fact below cites the file it came from, per the issue's own
instruction not to write prose that can drift silently. Sources used
(as of this page's writing, 2026-09-17):

- `ozand/eeebot` (the harness/product repo): AGENTS.md, docs/CURRENT_ARCHITECTURE.md,
  docs/ACTIVE_GOAL.md, docs/model-routing-evidence-1362.md,
  docs/specs/demo_category_determination.md, docs/adr/ADR-020..021,
  nanobot/runtime/model_registry.py, nanobot/runtime/scorecard.py,
  host/eeepc/systemd/*.service + *.timer.
- `ozand/eeebot-ops-dashboard` (this repo): systemd/eeebot-techtree-publish.service,
  systemd/drop-ins/.../20-techtree-publish.conf, README.md.
- The live host, `systemctl list-timers --all` (pasted into the issue's
  implementing session, dated 2026-09-17) -- used ONLY to cross-check the
  repo-sourced unit list below, never as this page's own live data source
  (this page is static structure, not a fetch).

Where a fact could not be traced to one of the above, it says
"not documented" rather than guess -- see `HOST_SPEC_NOTE` for one such
case (RAM figure disagrees across two harness-repo sources) and
`UNDOCUMENTED_LIVE_UNITS` for one confirmed on the live host but absent
from the harness repo's checked-in unit files.
"""
from __future__ import annotations

import html
from typing import Any


def esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


# ─── the four repositories ───────────────────────────────────────────────────
# Source: docs/CURRENT_ARCHITECTURE.md "Three-repo model" (ozand/eeebot,
# ozand/eeebot-self-evolving, ozand/eeebot-ops-dashboard) plus the fourth,
# ozand/eeebot-channel-app, confirmed via `gh repo list ozand` (description:
# "Home page, privacy policy and terms for the eeebot channel publisher
# OAuth client") -- CURRENT_ARCHITECTURE.md predates its existence on the
# site's identifier list and does not mention it.

REPOS: tuple[dict[str, str], ...] = (
    {
        "name": "ozand/eeebot",
        "role": "harness / product",
        "holds": "the runtime code (nanobot/), specs, host deploy tooling, and the fitness function (scorecard, targets, held-out checkers)",
        "identifiers": "issue numbers (#NNNN); commit shas for the harness's own history",
        "source": "docs/CURRENT_ARCHITECTURE.md",
    },
    {
        "name": "ozand/eeebot-self-evolving",
        "role": "instance",
        "holds": "the mutable workspace the loop edits (scripts/, surfaces/, tests/, memory/, docs/); checked out on the host beside the state root",
        "identifiers": "cycle ids (cycle-&lt;hex&gt;) mark commits the bridge integrates onto this repo's main; commit shas here are cycle integrations",
        "source": "docs/CURRENT_ARCHITECTURE.md",
    },
    {
        "name": "ozand/eeebot-ops-dashboard",
        "role": "dashboard (this repo)",
        "holds": "the WSGI observability app (dormant, not deployed) and the standalone techtree_viewer.py static-site generator that produces the page you are reading",
        "identifiers": "issue numbers (#NNNN) for dashboard-only work; no cycle ids or shas of its own appear on the published site",
        "source": "README.md",
    },
    {
        "name": "ozand/eeebot-channel-app",
        "role": "channel app",
        "holds": "the home page, privacy policy and terms for the eeebot channel publisher's OAuth client",
        "identifiers": "not surfaced on this dashboard",
        "source": "gh repo list ozand (repo description)",
    },
)

# ─── the cycle ────────────────────────────────────────────────────────────────
# Source: docs/CURRENT_ARCHITECTURE.md, "The live loop, end to end" (the
# numbered list). Page links map to SITE_PAGES in techtree_viewer.py; a
# stage with no dedicated page says so explicitly rather than omitting a
# row (issue #273 acceptance criterion).

CYCLE_STAGES: tuple[dict[str, str], ...] = (
    {
        "stage": "1. Demand selection",
        "detail": "demand.py scans state and yields items in trust order: priority &gt; defect &gt; goal-gap &gt; hypothesis &gt; decay.",
        "page": (
            'the demand kind and target for the current/most recent cycle appear in '
            '<a href="cycles.html">cycles</a>; hypothesis-kind demand is detailed on '
            '<a href="hypotheses.html">hypotheses</a>'
        ),
    },
    {
        "stage": "2. Propose",
        "detail": "the LLM proposer (llm_proposer.py) selects and refines one presented demand item -- it never invents from a bare inventory. No demand means zero LLM calls that cycle.",
        "page": 'what the proposer/executor is given (charter, derived priorities, skills) is on <a href="agent.html">agent</a>',
    },
    {
        "stage": "3. Dedup chain",
        "detail": "a proposal passes exact-tag replay protection, recent-failure suppression, and an FTS5 existence-index check before any subagent spawns.",
        "page": "not yet surfaced as its own section on this site",
    },
    {
        "stage": "4. Bounded executor",
        "detail": "exactly one fresh-context subagent runs and commits on a cycle branch.",
        "page": 'subagent records and prompts for one cycle are on the per-cycle detail page (linked from <a href="cycles.html">cycles</a>)',
    },
    {
        "stage": "5. Gate",
        "detail": "the bounded smoke gate: py_compile of changed files, the tests they affect, and a small fixed core set -- not the full suite. A mutation-surface violation is a hard block. The gate fails safe: any error, timeout, or missing pytest counts as failure.",
        "page": 'PASS / BLOCK / unknown badges per cycle are on <a href="cycles.html">cycles</a>',
    },
    {
        "stage": "6. Integrate",
        "detail": "only on a green gate does the bridge merge the cycle onto the instance repo's main (script tier) or, for an operator-approved runtime-slice cycle, land it as a pending promotion candidate for a product PR instead of auto-integrating.",
        "page": 'merged commits and the direction/lever state after integration are on <a href="lineage.html">lineage</a> and <a href="index.html">now</a>',
    },
    {
        "stage": "7. Scorecard + demand feeds the next cycle",
        "detail": "scorecard.py computes a deterministic, LLM-free 7-day snapshot; demand.py reads it to decide what the loop is offered next, closing the loop.",
        "page": 'the scorecard-derived KPI strip appears at the top of every page, including <a href="index.html">now</a>; held-out pass rate is one of its rows',
    },
)

# ─── the schedule ─────────────────────────────────────────────────────────────
# Source: ozand/eeebot host/eeepc/systemd/*.service + *.timer (Description=
# and the schedule directive, read directly from each file). This is the
# harness repo's OWN checked-in unit inventory, not a live host fetch --
# see UNDOCUMENTED_LIVE_UNITS below for the one confirmed live gap.

HARNESS_UNITS: tuple[dict[str, str], ...] = (
    {"unit": "eeepc-self-evolving-subagent-bridge", "cadence": "every 15 min (OnBootSec=4m, OnUnitActiveSec=15m)", "produces": "one bridge cycle: demand -> propose -> gate -> integrate", "page": "cycles.html, lineage.html"},
    {"unit": "eeebot-reflector", "cadence": "every 30 min (OnBootSec=25m, OnUnitActiveSec=30m)", "produces": "per-cycle reflections (findings, recommendations) -- the night contour's transcript reader", "page": "not yet surfaced as its own page"},
    {"unit": "eeebot-knowledge-curator", "cadence": "daily (OnCalendar=daily, i.e. 00:00)", "produces": "curates lessons into the bounded knowledge base", "page": "lessons.html"},
    {"unit": "eeebot-strategist", "cadence": "daily at 03:00 (OnCalendar=*-*-* 03:00:00)", "produces": "periodic archive review", "page": "not yet surfaced as its own page"},
    {"unit": "eeepc-promotion-verifier", "cadence": "every 15 min (OnBootSec=5m, OnUnitActiveSec=15m)", "produces": "root-verified re-check of runtime-slice promotion candidates", "page": "not yet surfaced as its own page"},
    {"unit": "eeebot-validator-harness", "cadence": "every 6 h (OnBootSec=20m, OnUnitActiveSec=6h)", "produces": "runs built validator scripts and records findings", "page": "not yet surfaced as its own page"},
    {"unit": "eeebot-skill-evals", "cadence": "daily at 04:30 (OnCalendar=*-*-* 04:30:00)", "produces": "harness A/B skill evals, measured with/without delta", "page": "not yet surfaced as its own page"},
    {"unit": "eeebot-local-ci", "cadence": "every 4 h 15 min (OnCalendar=*-*-* 00/4:15:00)", "produces": "bounded local CI check", "page": "not yet surfaced as its own page"},
    {"unit": "eeebot-action-index", "cadence": "daily at 00:05 (OnCalendar=*-*-* 00:05:00)", "produces": "extracts the durable per-cycle action index before prompt rotation", "page": "not yet surfaced as its own page"},
    {"unit": "eeebot-archive-subagent-requests", "cadence": "hourly (OnBootSec=10m, OnUnitActiveSec=1h)", "produces": "archives stale subagent requests older than 24h", "page": "not yet surfaced as its own page"},
    {"unit": "eeebot-host-metrics", "cadence": "every 6 h (OnBootSec=10min, OnUnitActiveSec=6h)", "produces": "refreshes the host metrics feed", "page": "not yet surfaced as its own page"},
    {"unit": "eeebot-host-capabilities", "cadence": "daily at 01:00 (OnCalendar=*-*-* 01:00:00, plus OnBootSec=10min)", "produces": "refreshes the host capability inventory", "page": "not yet surfaced as its own page"},
    {"unit": "eeebot-dashboard", "cadence": "always-on (no timer)", "produces": "the OTHER dashboard -- scripts/eeebot_dashboard.py, port 8080, in ozand/eeebot -- not this repo's WSGI app (dormant) and not the static site this page is part of", "page": "n/a (not this site)"},
)

# This dashboard repo's own publish pipeline -- a different mechanism from
# the harness units above: not a timer, an OnSuccess= hook on the bridge
# unit. Source: systemd/eeebot-techtree-publish.service and
# systemd/drop-ins/eeepc-self-evolving-subagent-bridge.service.d/20-techtree-publish.conf
# (this repo).
PUBLISH_PIPELINE_NOTE = (
    "eeebot-techtree-publish.service (installed from this repo's systemd/ directory) "
    "runs periodically via eeebot-techtree-publish.timer (every 15 minutes, issue #1905) "
    "and on completed cycles, digest-gated so an unchanged snapshot does not re-publish."
)

# Confirmed live (systemctl list-timers, pasted into this issue's session,
# 2026-09-17; host not stated in the pasted output, so not asserted here)
# but absent from host/eeepc/systemd/ in ozand/eeebot as of the same date --
# a real drift instance, not a hypothetical one. This is the citable answer
# to acceptance criterion 2 ("a unit added to the host without a
# corresponding entry is reported"): it already happened once, and this is
# that report.
UNDOCUMENTED_LIVE_UNITS: tuple[str, ...] = ("eeepc-monitor.timer",)

# ─── the models ───────────────────────────────────────────────────────────────
# Source: nanobot/runtime/model_registry.py (_ROLE_DEFAULTS + its comments),
# techtree_viewer.py's own _build_executor_model_item docstring ("the
# executor/harness role is only ever supposed to run self-hosted"), and
# docs/model-routing-evidence-1362.md (topology + the executor-time table,
# where "bridge" is the component name for the executor's own model calls).

MODEL_ROUTING: tuple[dict[str, str], ...] = (
    {
        "role": "executor (the bounded subagent that edits code; telemetry component name: bridge)",
        "model": "self-hosted, owned 3090Ti GPU on the LAN (un/qwen3.8-27b-gguf)",
        "why": "784/1004 recorded calls on the measured day; 24.30 completion tokens/request-wall-second",
        "source": "docs/model-routing-evidence-1362.md",
    },
    {
        "role": "proposer, curator, reflector, strategist (the night contour + the proposer)",
        "model": "vendor API (an/gemini-3.8-flash-high)",
        "why": "nanobot/runtime/model_registry.py's _ROLE_DEFAULTS; the operator preset overrides these per-role in production",
        "source": "nanobot/runtime/model_registry.py",
    },
)

GATEWAY_UNAVAILABLE_NOTE = (
    "Both self-hosted and vendor traffic traverse the same LAN gateway "
    "(docs/model-routing-evidence-1362.md): a gateway failure can affect either "
    "class. The dashboard's own executor-model-status check "
    "(_build_executor_model_item in techtree_viewer.py) flags a fallback when a "
    "vendor-class model serves a call the executor/harness role expected to be "
    "self-hosted -- the detectable signal, since llm_calls persists only the "
    "model that actually served the call, not a separate 'requested' field."
)

# eeepc itself: the ORCHESTRATOR, not the inference host for either model
# class (docs/model-routing-evidence-1362.md: "eeepc is an Atom N270/2 GB
# machine, not the inference host"). Two harness-repo sources disagree on
# its RAM figure -- stated honestly rather than picked:
HOST_SPEC_NOTE = (
    "eeepc: Intel Atom N270, i686/32-bit (docs/specs/demo_category_determination.md, "
    "#1619 live measurement). RAM is stated inconsistently across harness-repo "
    "sources: 1 GB per that same measurement, 2 GB per "
    "docs/adr/ADR-013-capability-tiers-probe-and-cost.md and "
    "docs/model-routing-evidence-1362.md's operator-verified topology -- not "
    "reconciled here. Core count and swap configuration: not documented in the "
    "harness repo."
)

# ─── vocabulary ────────────────────────────────────────────────────────────────
# Every entry cites the file the term is defined or used in. See
# tests/test_about_page.py for the cross-check that every term of art the
# OTHER six pages render has an entry here.

GLOSSARY: tuple[dict[str, str], ...] = (
    {
        "term": "harness",
        "definition": "ozand/eeebot: the product repo. Owns the runtime code, the fitness function, and the deploy tooling. Never edited by the loop itself.",
        "source": "docs/CURRENT_ARCHITECTURE.md",
    },
    {
        "term": "instance",
        "definition": "ozand/eeebot-self-evolving: the mutable workspace the loop edits. The bridge integrates green cycles onto this repo's main.",
        "source": "docs/CURRENT_ARCHITECTURE.md",
    },
    {
        "term": "cycle",
        "definition": "one timer-paced bridge process invocation -- demand selection through integrate-or-not. Identified by a cycle id (cycle-&lt;hex&gt;).",
        "source": "docs/CURRENT_ARCHITECTURE.md",
    },
    {
        "term": "gate",
        "definition": "the bounded smoke gate a cycle's changes must pass before integration: py_compile of changed files, the tests they affect, and a small fixed core set (not the full suite). Fails safe on any error, timeout, or missing pytest.",
        "source": "docs/CURRENT_ARCHITECTURE.md",
    },
    {
        "term": "integration",
        "definition": "the bridge merging a green, script-tier cycle onto the instance repo's main (--no-ff). origin/main never advances on a red gate.",
        "source": "docs/CURRENT_ARCHITECTURE.md",
    },
    {
        "term": "night contour",
        "definition": "the reflector -> curator -> strategist chain: components that see across many cycles, as opposed to the executor, which sees one. ADR-021's subject: the night contour may propose capability changes; it does not commit them directly.",
        "source": "docs/adr/ADR-021-whoever-sees-the-performance-may-change-the-capability.md",
    },
    {
        "term": "direction",
        "definition": "one node in the tech-tree portfolio (nanobot/runtime/tech_tree.py): an improvement domain the loop can invest cycles in, paired with a lever metric and which way that metric should move.",
        "source": "nanobot/runtime/tech_tree.py",
    },
    {
        "term": "lever",
        "definition": "the scorecard metric (section.metric) a tech-tree direction is measured against, e.g. loop.confirmed_integration_ratio.",
        "source": "nanobot/runtime/tech_tree.py",
    },
    {
        "term": "vector",
        "definition": "an operator-set, ordered purpose for the loop. Vector 1 (primary): self-improvement of the agent system. Vector 2 (secondary): operator interface and process transparency.",
        "source": "docs/ACTIVE_GOAL.md",
    },
    {
        "term": "demand kind",
        "definition": "the category of the next thing offered to the proposer, in trust order: priority &gt; defect &gt; goal-gap &gt; hypothesis &gt; decay.",
        "source": "docs/CURRENT_ARCHITECTURE.md",
    },
    {
        "term": "held-out",
        "definition": "sandboxed behavioral checkers run against instance artifacts on the scorecard recompute path; a failure becomes defect demand carrying the checker's evidence, without the instance ever seeing the checker itself.",
        "source": "docs/CURRENT_ARCHITECTURE.md",
    },
    {
        "term": "scorecard feed",
        "definition": "one of the named, freshness-monitored input paths nanobot/runtime/scorecard.py reads to compute its 7-day snapshot.",
        "source": "nanobot/runtime/scorecard.py",
    },
    {
        "term": "provenance",
        "definition": "the citable evidence trail behind a decision or claim -- which source file, ledger row, or scan produced it -- as opposed to an assertion with no traceable origin.",
        "source": "nanobot/runtime/goal_review.py",
    },
    {
        "term": "present",
        "definition": "probe state: the source exists and was read successfully, with real content.",
        "source": "scripts/techtree_viewer.py (read_local_ci_status)",
    },
    {
        "term": "absent",
        "definition": "probe state: the source file or directory does not exist at all.",
        "source": "scripts/techtree_viewer.py (read_local_ci_status)",
    },
    {
        "term": "present_uninitialized",
        "definition": "probe state: the source exists, but its own content says nothing has run yet (e.g. no rows, targets_missing) -- distinct from absent (nothing to find) and from a read failure.",
        "source": "scripts/techtree_viewer.py (read_local_ci_status)",
    },
    {
        "term": "probe_unavailable",
        "definition": "probe state: the source exists but could not be read or parsed (a transient failure) -- distinct from absent (never existed) and present_uninitialized (readable, just empty of activity).",
        "source": "scripts/techtree_viewer.py (read_local_ci_status)",
    },
)

TERMS_OF_ART: tuple[str, ...] = tuple(entry["term"] for entry in GLOSSARY)


def _repos_table() -> str:
    rows = "".join(
        f"<tr><td>{esc(r['name'])}</td><td>{esc(r['role'])}</td>"
        f"<td>{r['holds']}</td><td>{r['identifiers']}</td></tr>"
        for r in REPOS
    )
    return (
        '<table class="about-table"><thead><tr>'
        "<th>repository</th><th>role</th><th>holds</th><th>identifiers on this site</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _cycle_table() -> str:
    rows = "".join(
        f"<tr><td>{esc(s['stage'])}</td><td>{s['detail']}</td><td>{s['page']}</td></tr>"
        for s in CYCLE_STAGES
    )
    return (
        '<table class="about-table"><thead><tr>'
        "<th>stage</th><th>what happens</th><th>where the output appears</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _units_table() -> str:
    rows = "".join(
        f"<tr><td>{esc(u['unit'])}</td><td>{esc(u['cadence'])}</td>"
        f"<td>{esc(u['produces'])}</td><td>{u['page']}</td></tr>"
        for u in HARNESS_UNITS
    )
    exceptions = "".join(f"<li><code>{esc(name)}</code></li>" for name in UNDOCUMENTED_LIVE_UNITS)
    exceptions_html = (
        f'<p class="about-note">Confirmed running on the live host but with no '
        f"corresponding file in the harness repo's <code>host/eeepc/systemd/</code> "
        f"as of this page's writing:</p><ul>{exceptions}</ul>"
        if exceptions else ""
    )
    return (
        '<table class="about-table"><thead><tr>'
        "<th>unit</th><th>cadence</th><th>produces</th><th>appears on</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
        f"<p class=\"about-note\">{PUBLISH_PIPELINE_NOTE}</p>{exceptions_html}"
    )


def _models_table() -> str:
    rows = "".join(
        f"<tr><td>{r['role']}</td><td>{esc(r['model'])}</td><td>{esc(r['why'])}</td></tr>"
        for r in MODEL_ROUTING
    )
    return (
        '<table class="about-table"><thead><tr>'
        "<th>role</th><th>model</th><th>why</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
        f'<p class="about-note">{GATEWAY_UNAVAILABLE_NOTE}</p>'
        f'<p class="about-note">{HOST_SPEC_NOTE}</p>'
    )


def _glossary_list() -> str:
    items = "".join(
        f"<dt>{esc(g['term'])}</dt><dd>{g['definition']} "
        f'<span class="about-source">({esc(g["source"])})</span></dd>'
        for g in GLOSSARY
    )
    return f'<dl class="about-glossary">{items}</dl>'


# Self-contained: this page's own styling lives here, not in
# techtree_viewer.py's shared CSS constant, so the registration hunk in
# that file stays code-only (import + two dict/list entries).
_ABOUT_STYLE = '''<style>
.about-table { border-collapse: collapse; width: 100%; margin: 0.75em 0; }
.about-table th, .about-table td { border: 1px solid var(--border, #444); padding: 0.4em 0.6em; text-align: left; vertical-align: top; }
.about-table th { font-weight: 600; }
.about-note { font-size: 0.9em; opacity: 0.85; margin: 0.5em 0; }
.about-glossary dt { font-weight: 600; margin-top: 0.6em; }
.about-glossary dd { margin: 0.15em 0 0.4em 1em; }
.about-source { opacity: 0.7; font-size: 0.85em; }
</style>'''


def build_about_panel(data: dict[str, Any] | None = None, host: str = "", generated_at: str | None = None) -> str:
    """Assemble the about.html page body. Deliberately does not read
    `data` for anything factual -- see the module docstring."""
    return f'''
{_ABOUT_STYLE}
<section class="panel about-intro">
  <h2>What this is</h2>
  <p>eeebot is an autonomous improvement loop that runs bounded cycles on a
  constrained host (eeepc), editing its own instance repository under a gate.
  Every change the loop makes to its own capabilities goes through the same
  bounded gate described below; nothing here bypasses it.</p>
</section>

<section class="panel about-repos">
  <h2>The machine: four repositories</h2>
  {_repos_table()}
</section>

<section class="panel about-cycle">
  <h2>The cycle</h2>
  {_cycle_table()}
</section>

<section class="panel about-schedule">
  <h2>The schedule</h2>
  {_units_table()}
</section>

<section class="panel about-models">
  <h2>The models</h2>
  {_models_table()}
</section>

<section class="panel about-vocabulary">
  <h2>The vocabulary</h2>
  {_glossary_list()}
</section>
'''
