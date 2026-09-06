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
