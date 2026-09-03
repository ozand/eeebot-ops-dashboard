from pathlib import Path
import json

import nanobot_ops_dashboard.app as dashboard_app
from nanobot_ops_dashboard.app import create_app
from test_app import _call_app, _cfg, _seed_dashboard_data, _seed_experiment_telemetry, _seed_hypothesis_backlog
from nanobot_ops_dashboard.storage import init_db


def test_system_page_shows_guarded_evolution_status(tmp_path: Path, monkeypatch):
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    _seed_experiment_telemetry(tmp_path)
    _seed_hypothesis_backlog(tmp_path)

    def _patched_structured(path):
        path = str(path)
        if path.endswith('workspace/state/control_plane/current_summary.json'):
            return {'approval_gate': {'state': 'fresh'}}
        return original_structured(path)

    original_control_plane_summary = dashboard_app._control_plane_summary

    def _patched_control_plane_summary(repo_latest, eeepc_latest, current_experiment, current_blocker, cfg):
        payload = original_control_plane_summary(repo_latest, eeepc_latest, current_experiment, current_blocker, cfg)
        payload['guarded_evolution'] = {
            'schema_version': 'autoevolve-state-v1',
            'current_candidate': {'candidate_id': 'candidate-1'},
            'latest_request': {'request_id': 'request-1', 'objective': 'repair loop'},
            'last_apply': {'release_dir': '/tmp/release-1'},
            'last_rollback': {'rolled_back_to_release_dir': '/tmp/release-0'},
            'last_failure_learning': {'candidate_id': 'candidate-bad', 'learning_summary': 'repair first'},
        }
        return payload

    monkeypatch.setattr(dashboard_app, '_control_plane_summary', _patched_control_plane_summary)

    cfg = _cfg(tmp_path, db)
    app = create_app(cfg)
    status, body = _call_app(app, '/system')
    assert status.startswith('200')
    assert 'Guarded evolution' in body
    assert 'candidate-1' in body
    assert 'request-1' in body
    assert '/tmp/release-1' in body
    assert '/tmp/release-0' in body
    assert 'repair first' in body


def test_system_page_says_guarded_evolution_is_retired_when_state_is_absent(tmp_path: Path):
    """eeebot-ops-dashboard#205: state/self_evolution/ has had no writer since
    autoevolve was decommissioned (eeebot#1224). The card must say so, not
    render a stack of 'not_collected' rows that reads as a quiet, healthy panel."""
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    _seed_experiment_telemetry(tmp_path)
    _seed_hypothesis_backlog(tmp_path)
    assert not (tmp_path / 'nanobot' / 'workspace' / 'state' / 'self_evolution').exists()

    cfg = _cfg(tmp_path, db)
    app = create_app(cfg)
    status, body = _call_app(app, '/system')
    assert status.startswith('200')
    assert 'Guarded evolution' in body
    assert 'retired' in body
    assert 'eeebot#1224' in body
    assert 'Current candidate' not in body
    assert 'Last failure learning' not in body
    # The proof row survives the retirement branch and carries #207's label.
    assert 'Decommissioned (selfevo runtime artifacts retired per eeebot#1224)' in body

    # The same absence is labelled in the API, next to the two None values.
    status, api_body = _call_app(app, '/api/system')
    assert status.startswith('200')
    system = json.loads(api_body)
    dynamics = system['hypothesis_dynamics']
    assert dynamics['terminal_selfevo_issue'] is None
    assert dynamics['terminal_selfevo_pr'] is None
    assert dynamics['terminal_selfevo_evidence'] == {
        'status': 'unavailable',
        'retired': True,
        'reason': 'decommissioned (autoevolve removed in eeebot#1224)',
    }
    assert system['control_plane']['guarded_evolution']['retired'] is True
