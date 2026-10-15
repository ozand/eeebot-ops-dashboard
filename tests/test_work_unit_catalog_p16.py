"""p16 #421 regression: bounded JSON parser failures keep safe diagnostics."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import work_unit_catalog as wc


@pytest.fixture
def integer_limit_document(tmp_path):
    # Synthetic only; stay well below the existing byte cap, exceed the parser cap.
    payload = b'{"synthetic":' + b'7' * 4301 + b',"label":"SYNTHETIC_P16_CANARY"}'
    assert len(payload) < wc.MAX_BYTES
    path = tmp_path / 'synthetic-catalog.json'
    path.write_bytes(payload)
    return path, payload


def test_load_json_normalizes_integer_limit_without_exposing_payload(integer_limit_document):
    path, payload = integer_limit_document
    previous_limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(4300)
        # Prove this fixture reaches the actual JSON decoder's digit limit.
        with pytest.raises(ValueError):
            json.loads(payload)
        with pytest.raises(wc.CatalogError) as caught:
            wc.load_json(path)
        assert str(caught.value) == 'invalid_json'
        assert sys.get_int_max_str_digits() == 4300
    finally:
        sys.set_int_max_str_digits(previous_limit)
    assert path.read_bytes() == payload


def test_catalog_cli_integer_limit_emits_only_generic_diagnostic(integer_limit_document):
    path, payload = integer_limit_document
    env = dict(os.environ, PYTHONINTMAXSTRDIGITS='4300')
    result = subprocess.run(
        [sys.executable, str(Path(wc.__file__).resolve()), str(path)],
        env=env, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 1
    assert result.stdout == 'work-unit catalog: invalid or unreadable\n'
    assert result.stderr == ''
    assert path.read_bytes() == payload


@pytest.mark.parametrize(('payload', 'code'), [
    (b'{"x":1,"x":2}', 'duplicate_json_key'),
    (b'{"x":NaN}', 'nonfinite_json'),
    (b' ' * (wc.MAX_BYTES + 1), 'input_too_large'),
    (b'{', 'invalid_json'),
])
def test_parser_normalization_preserves_existing_error_codes(tmp_path, payload, code):
    path = tmp_path / 'synthetic.json'
    path.write_bytes(payload)
    with pytest.raises(wc.CatalogError) as caught:
        wc.load_json(path)
    assert str(caught.value) == code
    assert path.read_bytes() == payload
