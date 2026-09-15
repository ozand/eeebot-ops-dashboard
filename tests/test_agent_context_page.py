"""Tests for issue #227: agent.html two-tier context visualization and honesty rules."""
from __future__ import annotations

from typing import Any


from scripts import techtree_viewer as tv


def _base_fixture() -> dict[str, Any]:
    return {
        'portfolio': None,
        'ledger_tail': [],
        'ledger_history': [],
        'demand_rotation': None,
        'demand_completed': None,
        'skill_reads': None,
        'skill_evals': [],
        'llm_stats': {},
        'proposer_stats': None,
        'token_heatmap': None,
        'reflections': [],
        'bridge_exit_streak': None,
        'bridge_exits': None,
        'strategist_decisions': None,
        'demand_futility': None,
        'goal_text': None,
        'agents_md': '# AGENTS.md test content',
        'cycle_titles': None,
        'cycle_files': {},
        'cycle_titles_error': None,
        '_newest_source_age_seconds': None,
    }



def _telemetry_fixture(**overrides: Any) -> dict[str, Any]:
    fixture = _base_fixture()
    fixture['agent_context'] = {
        'system_prompt': {
            'cycle_id': 'cycle-261', 'chars': 100, 'cap': 24000,
            'sections': {'identity': 10, 'skills_catalogue': 90},
            'skills_catalogue': {
                'budget': 100, 'retained_count': 1, 'total_count': 3,
                'omitted_count': 2, 'omitted_names': ['lost-skill', 'second-lost'],
                'truncated': True,
            },
            'memory_index': {
                'status': 'present', 'resident_matched': 2,
                'resident_missing': ['memory-rule-missing'],
            },
        },
        'prompt_text': '# Identity\\n\\n---\\n\\n# Skills\\n<skill><name>kept-skill</name></skill>',
        'task_text': None, 'tier2_skills': [
            {'name': 'lost-skill', 'size_bytes': 10},
            {'name': 'disk-only', 'size_bytes': 10},
        ],
        'skill_reads': {'reads': [{'skill': 'read-only', 'confirmed': False}]},
        'executor_llm_stats': {'prompt_tokens': 12345},
        'compaction': {'status': 'empty', 'rows': []},
        'tier2_lessons': {'corpus_status': 'present', 'corpus_count': 0, 'total_size_bytes': 0, 'files': []},
        'tier2_memory': {'corpus_status': 'present', 'total_files': 0, 'total_size_bytes': 0, 'files': []},
    }
    fixture['agent_context'].update(overrides)
    return fixture


def test_issue261_catalogue_truncation_and_omissions_are_visible():
    html = tv.render_pages(_telemetry_fixture(), host='eeepc', generated_at='now')['agent.html']
    assert 'budget 100c' in html and 'retained 1/3' in html and 'truncated' in html
    assert 'lost-skill' in html and 'second-lost' in html
    assert 'unreachable' in html


def test_issue261_complete_catalogue_is_explicit():
    fixture = _telemetry_fixture()
    fixture['agent_context']['system_prompt']['skills_catalogue'] = {
        'budget': 100, 'retained_count': 3, 'total_count': 3,
        'omitted_count': 0, 'omitted_names': [], 'truncated': False,
    }
    html = tv.render_pages(fixture, host='eeepc', generated_at='now')['agent.html']
    assert 'retained 3/3' in html and 'complete' in html and 'omitted: none' in html


def test_issue261_skill_sources_are_joined_without_dropping_single_source_rows():
    html = tv.render_pages(_telemetry_fixture(), host='eeepc', generated_at='now')['agent.html']
    assert 'Skill source join' in html
    assert 'disk-only' in html and 'read-only' in html and 'lost-skill' in html
    assert '<th>In catalogue</th>' in html and '<th>On disk</th>' in html


def test_issue261_memory_index_and_compaction_empty_are_honest():
    html = tv.render_pages(_telemetry_fixture(), host='eeepc', generated_at='now')['agent.html']
    assert 'resident missing: memory-rule-missing' in html
    assert '90,000 tokens' in html and '12,345 prompt tokens' in html
    assert 'compaction did not fire' in html
    assert 'results compacted: 0' not in html


def test_issue261_missing_memory_index_is_unavailable():
    fixture = _telemetry_fixture()
    fixture['agent_context']['system_prompt'].pop('memory_index')
    html = tv.render_pages(fixture, host='eeepc', generated_at='now')['agent.html']
    assert 'Memory index:</strong> status unavailable' in html
    assert 'resident missing: unavailable' in html

def test_agent_page_renders_published_prompt_fit_event_telemetry():
    fixture = _base_fixture()
    fixture['agent_context'] = {
        'system_prompt': {
            'phase': 'system_prompt', 'cycle_id': 'cycle-event', 'chars': 21257,
            'cap': 24000, 'rung': 'full', 'dropped': [], 'ts': '2026-09-13T16:33:06Z',
        },
        'prompt_fit': {
            'schema_version': 'prompt-fit-v1', 'source_status': 'valid',
            'reader_status': 'complete', 'rows_considered': 25,
            'rows_with_drops': 0, 'rows_with_trims': 1, 'window_rows': 25,
            'window_kind': 'newest_system_prompt_rows', 'window_days': 90,
            'prompt_covered_from': '2026-09-13T15:00:00Z',
            'prompt_covered_to': '2026-09-13T16:33:06Z',
            'latest': {
                'rung': 'full',
                'dropped': {'status': 'empty', 'count': 0, 'chars': 0, 'sections': []},
                'trimmed': {'status': 'measured', 'count': 1, 'chars': 14, 'sections': ['bootstrap']},
            },
        },
        'prompt_text': None, 'task_text': None, 'tier2_skills': [],
        'tier2_lessons': {'index_status': 'missing', 'corpus_count': 0, 'total_size_bytes': 0, 'files': []},
        'tier2_memory': {'index_status': 'missing', 'total_files': 0, 'total_size_bytes': 0, 'files': []},
    }
    html = tv.render_pages(fixture, host='eeepc', generated_at='2026-09-13 16:54:54')['agent.html']
    assert 'Prompt Fit Event Telemetry' in html
    assert '0 sections / 0 chars' in html
    assert '1 sections / 14 chars' in html
    assert '0 / 25' in html
    assert '1 / 25' in html
    assert 'newest_system_prompt_rows' in html
    assert '2026-09-13T16:33:06Z' in html
    assert 'bootstrap' in html


def test_agent_page_renders_non_full_prompt_fit_rung():
    fixture = _base_fixture()
    fixture['agent_context'] = {
        'system_prompt': {
            'phase': 'system_prompt',
            'cycle_id': 'cycle-uniform-trim',
            'chars': 24000,
            'cap': 24000,
            'rung': 'uniform_trim',
            'sections': {'identity': 1446, 'bootstrap': 9333, 'skills_catalogue': 9200, 'memory': 4000},
            'trimmed': [{'section': 'bootstrap', 'chars': 14, 'how': 'uniform-trim'}],
            'dropped': [],
            'ts': '2026-09-11T05:00:00Z',
        },
        'prompt_text': None,
        'task_text': None,
        'tier2_skills': [],
        'tier2_lessons': {'index_status': 'missing', 'corpus_count': 0, 'total_size_bytes': 0, 'files': []},
        'tier2_memory': {'index_status': 'missing', 'total_files': 0, 'total_size_bytes': 0, 'files': []},
    }

    html = tv.render_pages(fixture, host='eeepc', generated_at='2026-09-11 08:00:00')['agent.html']

    assert 'Prompt fit degradation:' in html
    assert 'uniform_trim' in html


def test_structural_prompt_parser_preserves_outside_cap_sections_and_lengths():
    from scripts.agent_context import parse_prompt_sections

    prompt = "identity-body\n\n---\n\nbootstrap-body\n\n---\n\n# Immutable operator charter\n\ncharter\n\n# Loop agent identity\n\nidentity"
    parsed = parse_prompt_sections(
        prompt,
        {"identity": len("identity-body"), "bootstrap": len("bootstrap-body")},
    )

    assert parsed["status"] == "exact"
    assert [item["name"] for item in parsed["outside_cap"]] == ["goals", "loop_identity"]
    assert parsed["outside_cap"][0]["actual_chars"] == len("# Immutable operator charter\n\ncharter")


def test_structural_prompt_parser_surfaces_length_mismatch():
    from scripts.agent_context import parse_prompt_sections

    parsed = parse_prompt_sections(
        'short',
        {'identity': 99},
    )

    assert parsed['status'] == 'mismatch'
    assert parsed['mismatches'] == [{'name': 'identity', 'recorded_chars': 99, 'actual_chars': 5}]


def test_agent_page_renders_two_tier_context_and_reconciliation():
    """Issue #227: Tier 1 strict assembly order, arithmetic reconciliation, and Tier 2 link."""
    fixture = _base_fixture()
    fixture['agent_context'] = {
        'system_prompt': {
            'phase': 'system_prompt',
            'cycle_id': 'cycle-test-1',
            'chars': 27184,
            'cap': 30000,
            'overflow': False,
            'over_by': 0,
            'sections': {
                'identity': 1446,
                'bootstrap': 9636,
                'skills_catalogue': 12051,
                'memory': 4030,
            },
            'dropped': [],
            'droppable_reserve_chars': 0,
            'ts': '2026-09-06T04:00:00Z',
        },
        'prompt_text': '# nanobot 🐈\n\n---\n\n## AGENTS.md\n\n---\n\n# Skills\n\n---\n\n# Memory',
        'task_text': 'Task instructions',
        'tier2_skills': [
            {'name': 'batch_grep', 'size_bytes': 3500, 'desc': 'Search tool', 'content': '# batch_grep'},
            {'name': 'chained_exec', 'size_bytes': 4200, 'desc': 'Execution helper', 'content': '# chained'},
        ],
        'tier2_lessons': {
            'index_status': 'missing',
            'corpus_count': 41,
            'total_size_bytes': 150000,
            'files': [{'name': 'KB-001.md', 'size_bytes': 2500}],
        },
        'tier2_memory': {
            'index_status': 'present',
            'total_files': 60,
            'total_size_bytes': 80000,
            'files': [{'name': 'memory/facts/fact1.md', 'size_bytes': 1200}],
        },
    }

    pages = tv.render_pages(fixture, host='eeepc', generated_at='2026-09-06 12:00:00')
    html = pages['agent.html']

    # Two tiers present and clearly marked
    assert 'TIER 1' in html
    assert 'TIER 2' in html
    assert 'In Active Context' in html
    assert 'Reachable on Disk' in html

    # Strict assembly order: identity -> bootstrap -> active_skills -> skills_catalogue -> memory -> user
    idx_id = html.find('<code>identity</code>')
    idx_boot = html.find('<code>bootstrap</code>')
    idx_act = html.find('<code>active_skills</code>')
    idx_cat = html.find('<code>skills_catalogue</code>')
    idx_mem = html.find('<code>memory</code>')
    idx_usr = html.find('user (runtime_context + task)')

    assert idx_id != -1 and idx_boot != -1 and idx_act != -1 and idx_cat != -1 and idx_mem != -1 and idx_usr != -1
    assert idx_id < idx_boot < idx_act < idx_cat < idx_mem < idx_usr

    # Arithmetic reconciliation
    assert 'Arithmetic Character Reconciliation' in html
    assert '27,163' in html  # sum of sections
    assert '21' in html      # 3 separators * 7 chars
    assert '27,184' in html  # reconciled total
    assert 'Reconciliation verified' in html

    # Headroom / Capacity gauge
    assert '27,184 / 30,000' in html
    assert '+2,816 chars spare' in html
    assert 'context-badge-safe' in html

    # Tier 2 resources visible
    assert 'batch_grep' in html
    assert 'chained_exec' in html
    assert 'lessons/index.md: MISSING' in html
    assert '41 lessons' in html
    assert '60 files' in html

    # Visual linkage: connector arrow / link to Tier 2
    assert 'Indexes 2 Skills in Tier 2' in html
    assert 'tier-link-origin' in html


def test_agent_page_subject_oriented_context_group_order_is_stable():
    fixture = _base_fixture()
    fixture['agent_context'] = {
        'system_prompt': {'chars': 100, 'cap': 24000, 'sections': {'identity': 10}},
        'prompt_text': None, 'task_text': None, 'tier2_skills': [],
        'tier2_lessons': {'corpus_count': 0, 'total_size_bytes': 0, 'files': []},
        'tier2_memory': {'total_files': 0, 'total_size_bytes': 0, 'files': []},
    }
    html = tv.render_pages(fixture, host='eeepc', generated_at='now')['agent.html']
    budget = html.index('Prompt Budget &amp; Fit')
    assembly = html.index('Prompt Assembly &amp; Context Architecture')
    knowledge = html.index('Reachable Knowledge &amp; Access Paths')
    assert budget < assembly < knowledge
    assert html.count('Arithmetic Character Reconciliation') == 1
    assert html.count('Dialogue Window:') == 1


def test_agent_page_renders_goals_as_outside_capped_prompt_section():
    fixture = _base_fixture()
    fixture['agent_context'] = {
        'system_prompt': {
            'cycle_id': 'cycle-goals',
            'chars': 23,
            'cap': 24000,
            'sections': {'identity': 11, 'bootstrap': 5},
        },
        'prompt_text': 'identity text\n\n---\n\nboot!\n\n---\n\n# Immutable operator charter\n\ncharter body\n\n# Loop agent identity\n\nidentity body',
        'task_text': None,
        'tier2_skills': [],
        'tier2_lessons': {'corpus_count': 0, 'total_size_bytes': 0, 'files': []},
        'tier2_memory': {'total_files': 0, 'total_size_bytes': 0, 'files': []},
    }

    html = tv.render_pages(fixture, host='eeepc', generated_at='now')['agent.html']

    assert 'outside capped prompt' in html
    assert '<strong class="block-title">goals</strong>' in html
    assert 'charter body' in html
    assert 'absent (not configured/emitted)' not in html


def test_agent_page_reports_capped_and_actual_system_prompt_sizes():
    fixture = _base_fixture()
    prompt = 'identity text\n\n---\n\n# Immutable operator charter\n\ncharter body'
    fixture['agent_context'] = {
        'system_prompt': {
            'cycle_id': 'cycle-load',
            'chars': len('identity text'),
            'cap': 24000,
            'sections': {'identity': len('identity text')},
        },
        'prompt_text': prompt,
        'task_text': None,
        'tier2_skills': [],
        'tier2_lessons': {'corpus_count': 0, 'total_size_bytes': 0, 'files': []},
        'tier2_memory': {'total_files': 0, 'total_size_bytes': 0, 'files': []},
    }

    html = tv.render_pages(fixture, host='eeepc', generated_at='now')['agent.html']

    assert 'Prompt Load' in html
    assert 'capped chars' in html
    assert f'{len(prompt):,} chars received by model' in html
    assert 'capped chars' in html and 'chars received by model' in html


def test_agent_page_handles_missing_sections_before_1379_honestly():
    """Issue #227: Pre-#1379 cycles without sections must show unavailable, not reconstructed."""
    fixture = _base_fixture()
    fixture['agent_context'] = {
        'system_prompt': {
            'phase': 'system_prompt',
            'cycle_id': 'cycle-old-748dd0c2a71f',
            'chars': 24960,
            'cap': 30000,
            'overflow': False,
            'over_by': 0,
            'sections': None,  # Pre-#1379 historical row
            'dropped': [],
            'droppable_reserve_chars': 0,
            'ts': '2026-09-06T04:17:30Z',
        },
        'prompt_text': None,
        'task_text': None,
        'tier2_skills': [],
        'tier2_lessons': {'index_status': 'missing', 'corpus_count': 0, 'total_size_bytes': 0, 'files': []},
        'tier2_memory': {'index_status': 'missing', 'total_files': 0, 'total_size_bytes': 0, 'files': []},
    }

    pages = tv.render_pages(fixture, host='eeepc', generated_at='2026-09-06 12:00:00')
    html = pages['agent.html']

    # Must honestly report sections unavailable
    assert 'sections breakdown: unavailable' in html
    assert 'recorded prior to structured section logging' in html
    # Capacity still shown accurately
    assert '24,960 / 30,000' in html
    assert '+5,040 chars spare' in html


def test_agent_page_renders_overflow_and_dropped_sections():
    """Issue #227: Overflow rows clearly indicate overage and dropped sections struck through."""
    fixture = _base_fixture()
    fixture['agent_context'] = {
        'system_prompt': {
            'phase': 'system_prompt',
            'cycle_id': 'cycle-overflow-bca5',
            'chars': 27184,
            'cap': 24000,
            'overflow': True,
            'over_by': 3184,
            'sections': {
                'identity': 1446,
                'bootstrap': 9636,
                'skills_catalogue': 12051,
                'memory': 4030,
            },
            'dropped': ['droppable_notes', 'extra_context'],
            'droppable_reserve_chars': 0,
            'ts': '2026-09-06T00:49:04Z',
        },
        'prompt_text': None,
        'task_text': None,
        'tier2_skills': [],
        'tier2_lessons': {'index_status': 'missing', 'corpus_count': 0, 'total_size_bytes': 0, 'files': []},
        'tier2_memory': {'index_status': 'missing', 'total_files': 0, 'total_size_bytes': 0, 'files': []},
    }

    pages = tv.render_pages(fixture, host='eeepc', generated_at='2026-09-06 12:00:00')
    html = pages['agent.html']

    # Overflow status clearly rendered
    assert 'OVERFLOW' in html
    assert 'context-badge-overflow' in html
    assert '-3,184 chars (OVERFLOW)' in html

    # Dropped sections struck-through
    assert 'Trimming applied' in html
    assert '<s>droppable_notes</s>' in html
    assert '<s>extra_context</s>' in html


def test_agent_page_handles_unavailable_context_gracefully():
    """Issue #227: Graceful fallback when agent_context is None."""
    fixture = _base_fixture()
    fixture['agent_context'] = None

    pages = tv.render_pages(fixture, host='eeepc', generated_at='2026-09-06 12:00:00')
    html = pages['agent.html']

    assert 'Agent Context &amp; Working Memory' in html or 'Agent Context & Working Memory' in html
    assert 'context unavailable' in html
    # Standard agent sections still render
    assert 'host-identity' in html


def test_agent_context_tier2_corpus_states_are_explicit(tmp_path):
    """Missing, genuinely empty, and reader-failure corpus states cannot render as fabricated zero."""
    from scripts.agent_context import read_agent_context_dict

    state = tmp_path / "state"
    repo = tmp_path / "instance"
    state.mkdir()
    repo.mkdir()

    missing = read_agent_context_dict(state, repo)
    assert missing["tier2_skills_status"] == "missing"
    assert missing["tier2_lessons"]["corpus_status"] == "missing"
    assert missing["tier2_memory"]["corpus_status"] == "missing"
    assert "Skills Store: missing skills" in tv.build_two_tier_context_html(missing)

    for directory in ("skills", "lessons", "memory"):
        (repo / directory).mkdir()
    empty = read_agent_context_dict(state, repo)
    assert empty["tier2_skills_status"] == "present"
    assert empty["tier2_lessons"]["corpus_status"] == "present"
    assert empty["tier2_memory"]["corpus_status"] == "present"
    assert "Skills Store: 0 skills" in tv.build_two_tier_context_html(empty)
    assert "Lessons Corpus: 0 lessons" in tv.build_two_tier_context_html(empty)
    assert "Memory Store: 0 files" in tv.build_two_tier_context_html(empty)

    (repo / "skills").rmdir()
    (repo / "skills").write_text("not a directory", encoding="utf-8")
    failed = read_agent_context_dict(state, repo)
    assert failed["tier2_skills_status"] == "unavailable"
    html = tv.build_two_tier_context_html(failed)
    assert "Skills Store: unavailable skills" in html
    assert "Skills Store: unavailable skills" in html


def test_agent_page_unreadable_corpus_never_renders_zero_or_fabricated_total(tmp_path):
    from scripts.agent_context import read_agent_context_dict

    state = tmp_path / "state"
    repo = tmp_path / "instance"
    state.mkdir()
    repo.mkdir()
    (repo / "skills").write_text("not a directory", encoding="utf-8")
    (repo / "lessons").write_text("not a directory", encoding="utf-8")
    (repo / "memory").write_text("not a directory", encoding="utf-8")

    context = read_agent_context_dict(state, repo)
    html = tv.build_two_tier_context_html(context)

    assert "Skills Store: unavailable skills" in html
    assert "Lessons Corpus: unavailable lessons" in html
    assert "Memory Store: unavailable files" in html
    assert "unavailable files reachable via read_file" in html
    assert "Skills Store: 0 skills" not in html


def test_agent_page_preserves_non_empty_active_skills_and_separator_reconciliation():
    fixture = _base_fixture()
    fixture["agent_context"] = {
        "system_prompt": {
            "chars": 100,
            "cap": 24000,
            "sections": {
                "identity": 10,
                "bootstrap": 20,
                "active_skills": 30,
                "skills_catalogue": 40,
            },
        },
        "prompt_text": None,
        "task_text": None,
        "tier2_skills": [{"name": "one", "size_bytes": 1}],
        "tier2_skills_status": "present",
        "tier2_lessons": {"corpus_status": "present", "corpus_count": 0, "total_size_bytes": 0, "files": []},
        "tier2_memory": {"corpus_status": "present", "total_files": 0, "total_size_bytes": 0, "files": []},
    }

    html = tv.render_pages(fixture, host="eeepc", generated_at="now")["agent.html"]

    assert "30c (empty under loop profile)" not in html
    assert "30c</span>" in html
    assert "3 &times;" in html
    assert ">21c</span>" in html
    assert "Recorded chars: 100" in html


def test_agent_page_missing_prompt_cap_is_unavailable():
    fixture = _base_fixture()
    fixture["agent_context"] = {
        "system_prompt": {"chars": 1234, "sections": {"identity": 1234}},
        "prompt_text": None,
        "task_text": None,
        "tier2_skills": [],
        "tier2_lessons": {"corpus_status": "present", "corpus_count": 0, "total_size_bytes": 0, "files": []},
        "tier2_memory": {"corpus_status": "present", "total_files": 0, "total_size_bytes": 0, "files": []},
    }

    html = tv.render_pages(fixture, host="eeepc", generated_at="now")["agent.html"]

    assert "Context Budget Cap" in html
    assert "recorded cap unavailable" in html
    assert "Prompt Budget Utilization: <strong>unavailable</strong>" in html
    assert "30,000" not in html


def test_agent_page_weight_under_budget():
    """Issue #227: Total agent.html byte length must be under 500 KB."""
    fixture = _base_fixture()
    # Populate with 31 realistic skills
    skills = [
        {
            'name': f'skill_{i}',
            'size_bytes': 3500,
            'desc': f'Description of skill {i} for testing context page weight.',
            'content': '# SKILL ' + str(i) + '\n\nDetailed skill instructions.\n' * 50,
            'path': f'skills/skill_{i}/SKILL.md',
        }
        for i in range(31)
    ]
    fixture['agent_context'] = {
        'system_prompt': {
            'phase': 'system_prompt',
            'cycle_id': 'cycle-full',
            'chars': 27184,
            'cap': 30000,
            'overflow': False,
            'over_by': 0,
            'sections': {'identity': 1446, 'bootstrap': 9636, 'skills_catalogue': 12051, 'memory': 4030},
            'dropped': [],
            'droppable_reserve_chars': 0,
            'ts': '2026-09-06T04:00:00Z',
        },
        'prompt_text': '# Prompt text\n' * 200,
        'task_text': '# Task text\n' * 50,
        'tier2_skills': skills,
        'tier2_lessons': {
            'index_status': 'missing',
            'corpus_count': 41,
            'total_size_bytes': 150000,
            'files': [{'name': f'KB-{i:03d}.md', 'size_bytes': 3000} for i in range(41)],
        },
        'tier2_memory': {
            'index_status': 'present',
            'total_files': 60,
            'total_size_bytes': 80000,
            'files': [{'name': f'memory/fact_{i}.md', 'size_bytes': 1200} for i in range(60)],
        },
    }

    pages = tv.render_pages(fixture, host='eeepc', generated_at='2026-09-06 12:00:00')
    html = pages['agent.html']
    byte_len = len(html.encode('utf-8'))
    print(f'\nagent.html page weight: {byte_len:,} bytes ({byte_len/1024:.1f} KB)')
    assert byte_len < 500_000, f'agent.html is too heavy: {byte_len} bytes (budget: 500,000 bytes)'


def test_issue234_arithmetic_reconciliation_exact_and_overflow_and_distinct_states():
    """Issue #234: Exact arithmetic reconciliation, overflow rows with missing chars, and distinct active_skills states."""
    # 1. Overflow row with chars missing (chars is None or key absent)
    fixture_overflow = _base_fixture()
    fixture_overflow['agent_context'] = {
        'system_prompt': {
            'phase': 'system_prompt',
            'cycle_id': 'cycle-6fac1e4f8a03',
            'cap': 24000,
            'over_by': 2922,
            'overflow': True,
            'sections': {
                'identity': 1446,
                'bootstrap': 9374,
                'skills_catalogue': 12051,
                'memory': 4030,
            },
            'dropped': [],
            'ts': '2026-09-06T04:00:00Z',
        },
        'prompt_text': None,
        'task_text': None,
        'tier2_skills': [],
        'tier2_lessons': {'index_status': 'missing', 'corpus_count': 0, 'total_size_bytes': 0, 'files': []},
        'tier2_memory': {'index_status': 'missing', 'total_files': 0, 'total_size_bytes': 0, 'files': []},
    }
    pages = tv.render_pages(fixture_overflow, host='eeepc', generated_at='2026-09-06 12:00:00')
    html_ov = pages['agent.html']
    # Total chars must be computed as cap + over_by (26,922), not fabricated as 0!
    assert 'OVERFLOW (+2,922 chars over cap)' in html_ov
    assert 'Recorded chars: 26,922' in html_ov
    assert 'Exact Match' in html_ov
    # active_skills is absent in this row
    assert 'absent from breakdown' in html_ov

    # 2. active_skills present-and-zero
    fixture_zero = _base_fixture()
    fixture_zero['agent_context'] = {
        'system_prompt': {
            'phase': 'system_prompt',
            'cycle_id': 'cycle-zero',
            'cap': 30000,
            'chars': 27184,
            'overflow': False,
            'sections': {
                'identity': 1446,
                'bootstrap': 9636,
                'active_skills': 0,
                'skills_catalogue': 12051,
                'memory': 4030,
            },
            'dropped': [],
            'ts': '2026-09-06T04:00:00Z',
        },
        'prompt_text': None,
        'task_text': None,
        'tier2_skills': [],
        'tier2_lessons': {'index_status': 'missing', 'corpus_count': 0, 'total_size_bytes': 0, 'files': []},
        'tier2_memory': {'index_status': 'missing', 'total_files': 0, 'total_size_bytes': 0, 'files': []},
    }
    pages = tv.render_pages(fixture_zero, host='eeepc', generated_at='2026-09-06 12:00:00')
    html_zero = pages['agent.html']
    assert '0c (empty under loop profile)' in html_zero
    assert 'Exact Match' in html_zero

    # 3. active_skills dropped-by-the-fit
    fixture_dropped = _base_fixture()
    fixture_dropped['agent_context'] = {
        'system_prompt': {
            'phase': 'system_prompt',
            'cycle_id': 'cycle-dropped',
            'cap': 24000,
            'chars': 23487,
            'overflow': False,
            'sections': {
                'identity': 1446,
                'bootstrap': 9347,
                'skills_catalogue': 8643,
                'memory': 4030,
            },
            'dropped': [{'name': 'active_skills', 'chars': 1200}],
            'ts': '2026-09-06T04:00:00Z',
        },
        'prompt_text': None,
        'task_text': None,
        'tier2_skills': [],
        'tier2_lessons': {'index_status': 'missing', 'corpus_count': 0, 'total_size_bytes': 0, 'files': []},
        'tier2_memory': {'index_status': 'missing', 'total_files': 0, 'total_size_bytes': 0, 'files': []},
    }
    pages = tv.render_pages(fixture_dropped, host='eeepc', generated_at='2026-09-06 12:00:00')
    html_dropped = pages['agent.html']
    assert '<s>active_skills</s> (1,200c)' in html_dropped

