"""#1772: the daily digest -- one artifact per day listing every integration
into the instance repository, with the 3-state (unavailable/empty/N)
reporting convention issue #175 already established for this dashboard
(see test_issue188_provenance_badge_three_state and _index_teasers)."""
from __future__ import annotations

from datetime import datetime, timezone

from scripts import techtree_viewer as tv


def _outcome_row(cycle_id: str, ts: str, *, files=None, tier="code-bearing") -> dict:
    return {
        "phase": "outcome", "cycle_id": cycle_id, "outcome": "success", "ts": ts,
        "files_changed": files if files is not None else [f"scripts/{cycle_id}.py"],
        "change_tier": tier,
    }


def _tree(mapping: dict) -> dict:
    return {"nodes": {sha: {"cycle_id": cid} for cid, sha in mapping.items()}}


def _completed(entries: dict) -> dict:
    return {"entries": entries}


class TestThreeStateReporting:
    def test_ledger_none_is_unavailable(self):
        html = tv.build_daily_digest(None)
        assert "unavailable" in html

    def test_empty_ledger_is_no_integrations_not_unavailable(self):
        html = tv.build_daily_digest([])
        assert "no integrations in this window" in html
        # Distinct from the unavailable case: not tv.unavailable_panel's own
        # shape ("panel-unavailable" / "ledger unavailable"), even though
        # both reuse the shared .unavailable-note CSS class for styling.
        assert "panel-unavailable" not in html
        assert "ledger unavailable" not in html

    def test_readable_ledger_with_a_real_integration_shows_a_count(self):
        rows = [_outcome_row("c1", "2026-09-19T01:00:00Z")]
        html = tv.build_daily_digest(rows, evolution_tree=_tree({"c1": "abc123"}))
        assert "1 integration(s)" in html
        assert "no integrations" not in html and "unavailable" not in html


class TestFieldsAndJoins:
    def test_all_five_fields_present(self):
        rows = [_outcome_row("c1", "2026-09-19T01:00:00Z", files=["scripts/a.py", "docs/b.md"], tier="documentation")]
        completed = _completed({"d1": {"cycle_id": "c1", "confirmed": True, "signal": "reference"}})
        html = tv.build_daily_digest(rows, demand_completed=completed, evolution_tree=_tree({"c1": "abc1234567"}))
        assert "c1" in html
        assert "abc123" in html  # short sha
        assert "scripts/a.py" in html and "docs/b.md" in html
        assert "documentation" in html
        assert "confirmed" in html

    def test_missing_completed_entry_renders_untracked_not_a_false_negative(self):
        """#1772 AC: a failed join must never read as a false 'not confirmed'."""
        rows = [_outcome_row("c1", "2026-09-19T01:00:00Z")]
        html = tv.build_daily_digest(rows, demand_completed=_completed({}), evolution_tree=_tree({"c1": "sha1"}))
        assert "untracked" in html
        assert "unconfirmed" not in html

    def test_entry_present_but_not_confirmed_renders_unconfirmed(self):
        rows = [_outcome_row("c1", "2026-09-19T01:00:00Z")]
        completed = _completed({"d1": {"cycle_id": "c1", "confirmed": False}})
        html = tv.build_daily_digest(rows, demand_completed=completed, evolution_tree=_tree({"c1": "sha1"}))
        assert "unconfirmed" in html
        assert ">untracked<" not in html

    def test_foreign_signal_on_a_confirmed_entry_does_not_count(self):
        """Mirrors the harness's own reward-hack guard (2026-07-17): a
        confirmed:true entry whose signal is not harness-authored must not
        read as confirmed here either."""
        rows = [_outcome_row("c1", "2026-09-19T01:00:00Z")]
        completed = _completed({"d1": {"cycle_id": "c1", "confirmed": True, "signal": "self-reported"}})
        html = tv.build_daily_digest(rows, demand_completed=completed, evolution_tree=_tree({"c1": "sha1"}))
        assert "unconfirmed" in html

    def test_cycle_without_an_evolution_tree_sha_is_omitted(self):
        """Nothing to link a commit to -- not shown as a phantom row."""
        rows = [_outcome_row("c1", "2026-09-19T01:00:00Z")]
        html = tv.build_daily_digest(rows, evolution_tree=None)
        assert "no integrations in this window" in html

    def test_failed_and_partial_outcomes_are_excluded(self):
        rows = [
            {"phase": "outcome", "cycle_id": "c1", "outcome": "failed", "ts": "2026-09-19T01:00:00Z"},
            {"phase": "outcome", "cycle_id": "c2", "outcome": "partial", "ts": "2026-09-19T01:00:00Z"},
        ]
        html = tv.build_daily_digest(rows, evolution_tree=_tree({"c1": "sha1", "c2": "sha2"}))
        assert "no integrations in this window" in html


class TestDayGrouping:
    def test_groups_by_calendar_day_newest_first(self):
        rows = [
            _outcome_row("c1", "2026-09-17T23:00:00Z"),
            _outcome_row("c2", "2026-09-18T01:00:00Z"),
            _outcome_row("c3", "2026-09-19T05:00:00Z"),
        ]
        tree = _tree({"c1": "sha1", "c2": "sha2", "c3": "sha3"})
        html = tv.build_daily_digest(rows, evolution_tree=tree, now=datetime(2026, 9, 19, 12, tzinfo=timezone.utc))
        first_day = html.find("2026-09-19")
        second_day = html.find("2026-09-18")
        third_day = html.find("2026-09-17")
        assert -1 < first_day < second_day < third_day

    def test_days_outside_the_window_are_excluded(self):
        rows = [_outcome_row("old", "2026-01-01T00:00:00Z")]
        html = tv.build_daily_digest(
            rows, evolution_tree=_tree({"old": "sha1"}),
            now=datetime(2026, 9, 19, tzinfo=timezone.utc), days=14,
        )
        assert "no integrations in this window" in html

    def test_dedups_multiple_outcome_rows_for_the_same_cycle(self):
        """A retried cycle can leave more than one terminal outcome row for
        the same cycle_id (e.g. push_pending -> pushed_late); the digest
        must not double-list it."""
        rows = [
            _outcome_row("c1", "2026-09-19T01:00:00Z"),
            _outcome_row("c1", "2026-09-19T01:05:00Z"),
        ]
        html = tv.build_daily_digest(rows, evolution_tree=_tree({"c1": "sha1"}))
        assert "1 integration(s)" in html
