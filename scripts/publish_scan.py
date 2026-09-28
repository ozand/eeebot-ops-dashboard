"""ADR-036: Publication rejection scanner for ops dashboard gh-pages.

Rejects publication loudly (rc != 0) if any page contains secrets, credentials,
internal paths (/etc/eeepc-agent), or structural call-text markers.
Used by publish_to_pages and autopublish dry-run; reused by D2 masking.
"""
from __future__ import annotations

import ast
import functools
import html as _html
from html.parser import HTMLParser
import hashlib
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Pattern


class PublicationScanError(Exception):
    """Raised when publication is rejected due to leaked secrets or call text."""


class FingerprintUnavailableError(RuntimeError):
    """Shared scanner infrastructure could not be fingerprinted safely."""


PublishScanError = PublicationScanError


class SecretPattern(NamedTuple):
    name: str
    pattern: Pattern[str]
    description: str


EXCLUDED_EXACT_NAMES = frozenset({
    "key", "keys", "pass", "passive", "max_tokens", "token_count",
})

_METRIC_NAME_TOKENS = frozenset({
    "count", "ratio", "rate", "limit", "floor", "budget", "window",
    "duration", "hours", "seconds",
})

_METRIC_SUBSTRINGS = (
    "tokens_per_integration", "prompt_tokens", "completion_tokens",
    "total_tokens", "self_hosted_tokens", "vendor_tokens",
)


def is_excluded_key_name(key: str) -> bool:
    """True if key represents a harmless metric/counter rather than a credential."""
    k = key.lower()
    if k in EXCLUDED_EXACT_NAMES:
        return True
    parts = set(re.split(r"[_\-.]+", k))
    if parts & _METRIC_NAME_TOKENS:
        if any(sec in parts for sec in {"password", "secret", "pass", "auth"}):
            return False
        return True
    if any(sub in k for sub in _METRIC_SUBSTRINGS):
        return True
    return False

PUBLIC_PAGE_PATHS = frozenset({
    "index.html", "lineage.html", "cycles.html", "tokens.html", "lessons.html",
    "agent.html", "hypotheses.html", "about.html", "techtree.html", "cycle.html",
    "cycles-archive-index.json", "lineage-cycle-details.json",
})

_ARCHIVE_JSON_RE = re.compile(r"^cycles-archive-[0-9]+\.json$")


def is_allowed_publish_path(path: str) -> bool:
    """Return True if path is an authorized public artifact name under ADR-036."""
    return path in PUBLIC_PAGE_PATHS or bool(_ARCHIVE_JSON_RE.match(path))


def validate_publish_allowlist(paths: 'Iterable[str]') -> None:
    """Validate that all paths destined for or inherited by gh-pages are allowed.

    Raises PublicationScanError if any unlisted path is detected.
    """
    unlisted = sorted(p for p in paths if not is_allowed_publish_path(p))
    if unlisted:
        raise PublicationScanError(
            f"Publication rejected (ADR-036 rule 3): unlisted publication path(s) not in allowlist: {', '.join(unlisted)}"
        )


def is_secret_value(value: str) -> bool:
    """True if string value resembles an actual secret, token, or password."""
    v = value.strip().strip("'\"")
    if len(v) < 8:
        return False
    if re.match(r"^-?[0-9]+(?:\.[0-9]+)?$", v):
        return False
    if v.startswith(("<", "$", "{", "[")) or v.endswith((">")):
        return False
    lower = v.lower()
    if lower in {"none", "null", "true", "false", "undefined", "dummy", "example", "redacted", "placeholder"}:
        return False
    if not re.search(r"[A-Za-z0-9]", v):
        return False
    return True


# #355 (F4): the shared scan functions read these imported names by name
# (``re.split``/``re.IGNORECASE`` in ``is_excluded_key_name``/``scan_text``,
# ``json.loads``/``json.JSONDecodeError`` in ``scan_text``, ``_html.unescape``
# in ``_unescape_until_stable``, ``HTMLParser`` as ``_ScanHTMLParser``'s base
# class) but none of them has source of its own to fingerprint -- they're
# stdlib. Their behavior is pinned to the interpreter's stdlib version
# instead, since that's the only thing that can change it without a diff to
# this file. No ``releaselevel``/``serial``: the host runs one pinned
# interpreter build, so major.minor.micro is already a stable, sufficient
# identity here.
_STDLIB_IMPORT_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _import_bindings_identity() -> str:
    """Normalized (module target + local alias) text for every top-level
    ``Import``/``ImportFrom`` in this file, sorted for a stable order.

    #358: ``_STDLIB_IMPORT_VERSION`` only tracks the interpreter, so
    retargeting an import to a different module while keeping the same
    local alias (``import foo as re`` instead of ``import re``) left the
    shared fingerprint unchanged -- the alias a fingerprinted function
    reads by name stayed the same, but what it's actually bound to did
    not. This fingerprints the binding itself, straight from source, not
    just the interpreter behind it.

    Limitation: only *top-level* (module-body) ``Import``/``ImportFrom``
    are covered -- an import inside a function body is invisible here.
    ``inherited_blob_decoder_version`` imports ``techtree_viewer`` inside
    its own body so a publish-only checkout without that sibling module
    still degrades cleanly instead of failing at module load. The decoder
    itself now has a separate source fingerprint; the reader's fallback
    import target does not change that decoder-source fingerprint.
    """
    source = inspect.getsource(sys.modules[__name__])
    tree = ast.parse(source)
    bindings: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                bindings.append(f"import {alias.name} as {local}")
        elif isinstance(node, ast.ImportFrom):
            level = "." * node.level
            module = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                bindings.append(f"from {level}{module} import {alias.name} as {local}")
    return "\n".join(sorted(bindings))


_RESERVED_RULE_NAMES = frozenset({"json_secret_field", "env_secret_kv"})
_SHARED_FINGERPRINT_DEPENDENCIES = frozenset({
    "is_excluded_key_name", "EXCLUDED_EXACT_NAMES", "_METRIC_NAME_TOKENS",
    "_METRIC_SUBSTRINGS", "is_secret_value", "_unescape_until_stable",
    "_json_strings", "_ScanHTMLParser", "RAW_TEXT_TAGS",
    "_html_scan_variants", "scan_text", "_text_has_rule_anchor",
    "PublicationScanError", "FingerprintUnavailableError", "validate_rule_names", "_RESERVED_RULE_NAMES",
    "_SHARED_FINGERPRINT_DEPENDENCIES", "_RULE_DATA_DEPENDENCIES",
    "_shared_fingerprint_dependency_names", "_rule_data_dependency_names",
    "_all_top_level_dependency_objects", "_shared_scan_version", "rule_names",
    "_STDLIB_IMPORT_VERSION", "_IMPORT_BINDINGS_IDENTITY",
})
_RULE_DATA_DEPENDENCIES = frozenset({
    "STANDALONE_PATTERNS", "SCANNER_ANCHORS", "_JSON_CANDIDATE_RE",
    "_JSON_SECRET_KEY_RE", "_ENV_KEY_CANDIDATE_RE", "_ENV_SECRET_KV_RE",
})


def validate_rule_names(names: Iterable[str]) -> None:
    """Fail closed when scanner rule names are duplicate or reserved."""
    seen: set[str] = set()
    for name in names:
        if name in seen or name in _RESERVED_RULE_NAMES:
            raise RuntimeError(f"duplicate or reserved scanner rule name: {name}")
        seen.add(name)


STANDALONE_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern("eeepc_agent_path", re.compile(r"/etc/eeepc-agent"), "internal /etc/eeepc-agent path"),
    SecretPattern("openai_secret_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "OpenAI secret key format"),
    SecretPattern("github_token", re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9_]{16,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "GitHub token"),
    SecretPattern("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}\b"), "Bearer token header"),
    SecretPattern("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    SecretPattern("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "Slack API token"),
    SecretPattern("basic_auth", re.compile(r"(?i)\bAuthorization\s*:\s*Basic\s+[A-Za-z0-9+/=]{10,}\b|\bBasic\s+[A-Za-z0-9+/=]{16,}\b"), "Basic Auth header"),
    SecretPattern("url_credentials", re.compile(r"https?://[^:\s/\"']+:[^@\s/\"']+@[^/\s\"']+"), "URL containing embedded credentials"),
    SecretPattern("private_key_header", re.compile(r"-----BEGIN (?:[A-Z0-9_-]+ )?PRIVATE KEY-----"), "private key header"),
    SecretPattern("structural_reasoning_content", re.compile(r"['\"]reasoning_content['\"]"), "call marker reasoning_content"),
    SecretPattern("structural_messages", re.compile(r"['\"]messages['\"]\s*:"), "call marker messages"),
    SecretPattern("structural_prompt", re.compile(r"['\"]prompt['\"]\s*:\s*\{"), "call marker prompt object"),
)


_JSON_SECRET_KEY_RE = re.compile(
    r'(?i)[\'"]([a-z0-9_]*(?:password|secret|api[_-]?key|access_token|auth_token|token)[a-z0-9_]*)[\'"]\s*:\s*(?:["\']([^"\']+)["\']|([^,}\s]+))'
)
_ENV_SECRET_KV_RE = re.compile(
    r'(?i)\b([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)[A-Za-z0-9_]*)\s*[:=]\s*(?:"([^"]{8,})"|\'([^\']{8,})\'|([^"\'<>\s$]{8,}))'
)
_ENV_KEY_CANDIDATE_RE = re.compile(
    r'(?i)\b[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)[A-Za-z0-9_]*\s*[:=]'
)

validate_rule_names(rule.name for rule in STANDALONE_PATTERNS)

# Required literal anchors per scanner rule. A rule may have multiple
# alternatives; every successful alternative must contain at least one anchor.
SCANNER_ANCHORS: dict[str, tuple[str, ...]] = {
    "eeepc_agent_path": ("/etc/eeepc-agent",),
    "openai_secret_key": ("sk-",),
    "github_token": ("ghp_", "gho_", "ghs_", "ghu_", "github_pat_"),
    "bearer_token": ("bearer",),
    "aws_access_key": ("AKIA",),
    "slack_token": ("xox",),
    "basic_auth": ("basic",),
    "url_credentials": ("http://", "https://"),
    "private_key_header": ("-----BEGIN", "PRIVATE KEY-----"),
    "structural_reasoning_content": ("'reasoning_content'", '"reasoning_content"'),
    "structural_messages": ("'messages'", '"messages"'),
    "structural_prompt": ("'prompt'", '"prompt"'),
    "_env_secret_kv": ("_env_secret_kv",),
    "_json_secret_key": ("_json_secret_key",),
}


_JSON_CANDIDATE_RE = re.compile(
    r"(?i:[\"'][a-z0-9_]*(?:password|secret|api[_-]?key|access_token|auth_token|token)[a-z0-9_]*[\"']\s*:)"
)


def _unescape_until_stable(text: str, max_rounds: int = 5) -> str:
    """Unescape entities with a bound; refuse if another decode round is needed."""
    current = text
    for _ in range(max_rounds):
        if "&" not in current:
            return current
        decoded = _html.unescape(current)
        if decoded == current:
            return current
        current = decoded
    if "&" in current and _html.unescape(current) != current:
        raise PublicationScanError(
            f"Publication rejected (ADR-036 rule 3): HTML entity decoding exceeds {max_rounds} round limit"
        )
    return current


class _ScanHTMLParser(HTMLParser):
    """Collect text, attributes and raw-text bodies for security scanning."""
    RAW_TEXT_TAGS = frozenset({"script", "style", "textarea", "title"})

    def __init__(self, *, collect_raw_text: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.text_parts: list[str] = []
        self.attribute_values: list[str] = []
        self.raw_text_parts: list[str] = []
        self.collect_raw_text = collect_raw_text
        self._raw_text_tag: str | None = None

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
        self.text_parts.append(data)
        if self.collect_raw_text and self._raw_text_tag:
            self.raw_text_parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = [value or "" for _name, value in attrs]
        self.parts.extend(values)
        self.attribute_values.extend(values)
        if tag.lower() in self.RAW_TEXT_TAGS:
            self._raw_text_tag = tag.lower()

    def handle_endtag(self, tag: str) -> None:
        if self._raw_text_tag == tag.lower():
            self._raw_text_tag = None

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


def _html_scan_variants(markup: str) -> list[str]:
    """Return text and attribute streams; raw-text markup is parsed only when present."""
    parser = _ScanHTMLParser()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        return []  # scan source itself on malformed markup

    variants = []
    text_stream = _unescape_until_stable("".join(parser.text_parts))
    if text_stream:
        variants.append(text_stream)
    # Newlines delimit values: matches cannot be fabricated across attributes.
    attr_stream = "\x00".join(_unescape_until_stable(v) for v in parser.attribute_values)
    if attr_stream:
        variants.append(attr_stream)
    for fragment in parser.raw_text_parts:
        if "<" in fragment:
            variants.extend(_html_scan_variants(fragment))
    return variants


def _json_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _json_strings(item)


def scan_text(
    content: str, *, html_mode: bool = True, json_mode: bool = False,
    rules: "Iterable[str] | None" = None,
) -> dict[str, int]:
    """Scan text and return rule hit counts; parser work is selected by artifact type.

    ``rules`` (ADR-036/#340 per-rule cache): if given, only these named
    rules are checked -- the whole page is still parsed/decoded exactly as
    before ("every rule scans the whole page", no seams), only WHICH rules
    run against it changes. ``None`` (default) checks every rule, matching
    every caller that does not care about a partial rule set (equivalence
    tests, direct callers).
    """
    active_rules = set(rules) if rules is not None else None
    findings: dict[str, int] = {}
    source_variants = [content, _unescape_until_stable(content)]
    strings: list[str] = []
    parsed_json = False
    if json_mode:
        try:
            decoded = json.loads(content)
        except (json.JSONDecodeError, UnicodeError):
            pass  # malformed JSON: scan raw and decoded source fail-closed
        else:
            parsed_json = True
            strings = [_unescape_until_stable(value) for value in _json_strings(decoded)]
            # Scan decoded strings independently; retain the original source
            # too so assignment rules can see JSON key/colon/value structure.
            source_variants.extend(strings)
    elif html_mode:
        source_variants.extend(_html_scan_variants(source_variants[-1]))
    if parsed_json:
        # A decoded JSON value may itself embed HTML markup. Parse strings as
        # independent fragments; never parse the JSON container or join values.
        for value in strings:
            if "<" in value:
                source_variants.extend(_html_scan_variants(value))
    variants = list(dict.fromkeys(source_variants))

    lower_cache: dict[str, str] = {}
    for rule in STANDALONE_PATTERNS:
        if active_rules is not None and rule.name not in active_rules:
            continue
        anchors = SCANNER_ANCHORS.get(rule.name)
        for variant in variants:
            insensitive = bool(rule.pattern.flags & re.IGNORECASE) or rule.pattern.pattern.startswith("(?i)")
            lowered = lower_cache.setdefault(variant, variant.lower()) if insensitive else None
            if anchors and not _text_has_rule_anchor(variant, anchors, rule.pattern, lowered):
                continue
            count = sum(1 for _ in rule.pattern.finditer(variant))
            if count:
                findings[rule.name] = max(findings.get(rule.name, 0), count)

    json_count = 0
    env_count = 0
    want_json = active_rules is None or "json_secret_field" in active_rules
    want_env = active_rules is None or "env_secret_kv" in active_rules
    for variant in variants if (want_json or want_env) else ():
        if want_json and _JSON_CANDIDATE_RE.search(variant):
            hits = 0
            for match in _JSON_SECRET_KEY_RE.finditer(variant):
                key = match.group(1)
                value = match.group(2) or match.group(3) or ""
                if not is_excluded_key_name(key) and is_secret_value(value):
                    hits += 1
            json_count = max(json_count, hits)
        if want_env and _ENV_KEY_CANDIDATE_RE.search(variant):
            hits = 0
            for match in _ENV_SECRET_KV_RE.finditer(variant):
                key = match.group(1)
                value = match.group(2) or match.group(3) or match.group(4) or ""
                if not is_excluded_key_name(key) and is_secret_value(value):
                    hits += 1
            env_count = max(env_count, hits)
    if json_count:
        findings["json_secret_field"] = json_count
    if env_count:
        findings["env_secret_kv"] = env_count
    return findings


def _text_has_rule_anchor(
    text: str,
    anchors: tuple[str, ...],
    pattern: re.Pattern[str],
    lowered: str | None = None,
) -> bool:
    insensitive = bool(pattern.flags & re.IGNORECASE) or pattern.pattern.startswith("(?i)")
    if insensitive:
        normalized = lowered if lowered is not None else text.lower()
        return any(anchor.lower() in normalized for anchor in anchors)
    return any(anchor in text for anchor in anchors)

_RULE_CACHE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*:[0-9a-f]{64}:(?:html|json):(?:[0-9a-f]{40}|[0-9a-f]{64})$")

# Target number of distinct fully-approved content identities (pages or
# inherited blobs) to retain. Codex re-check on PR #343 (P1): a flat
# 256-entry bound left over from the single-key-per-page era only holds
# ~256/len(rule_names()) fully-approved pages once every rule has its own
# key -- 18 pages for today's 14 rules. The actual entry bound scales
# with the rule count so the intended page-level capacity is preserved.
_CACHE_PAGE_CAP = 256

# A fixed, generous ceiling for validate_clean_cache's size sanity check --
# NOT tied to today's live rule count, so a rule REMOVAL (which shrinks
# _cache_entry_bound()) can never wholesale-reject an otherwise-valid,
# merely-larger-than-the-new-bound cache. Headroom for far more rules
# than exist today; scan_pages's own pruning still rightsizes to the
# live bound afterward.
_CACHE_VALIDATE_CEILING = _CACHE_PAGE_CAP * 128


def _cache_entry_bound() -> int:
    return _CACHE_PAGE_CAP * len(rule_names())


def validate_clean_cache(value: Any) -> dict[str, bool]:
    """Return a safe cache or empty mapping; any malformed entry invalidates
    all -- format only. Size is checked against a stable, generous ceiling
    (``_CACHE_VALIDATE_CEILING``), never the live, rule-count-scaled
    ``_cache_entry_bound()``.

    Codex re-check on PR #343 (P2): rejecting against the LIVE bound meant
    a release that removes even one rule shrinks the bound and can
    wholesale-wipe a cache that was merely a bit larger than the new,
    smaller bound -- every remaining rule's still-valid approval lost,
    forcing a full cold scan of every page. ``scan_pages``'s own pruning
    step (rule-name + version matching, run AFTER this validation)
    already rightsizes to the live bound and correctly drops entries for
    a removed rule; this check only needs to catch genuinely malformed or
    absurdly oversized input, not track the exact current rule count.
    """
    if not isinstance(value, dict):
        return {}
    if len(value) > _CACHE_VALIDATE_CEILING or any(
        not isinstance(key, str) or not _RULE_CACHE_KEY_RE.fullmatch(key) or clean is not True
        for key, clean in value.items()
    ):
        return {}
    return dict(value)


def rule_names() -> tuple[str, ...]:
    """All named scanner rules. A function, not a module constant, so a
    monkeypatched ``STANDALONE_PATTERNS`` (tests add/replace rules) is
    reflected without re-importing anything. Validate dynamically too, so a
    runtime/test replacement cannot collide with reserved special rules.
    """
    names = tuple(rule.name for rule in STANDALONE_PATTERNS)
    validate_rule_names(names)
    return names + tuple(sorted(_RESERVED_RULE_NAMES))


def _rule_witness(rule_name: str) -> str:
    """This ONE rule's own definition, and nothing else -- changing a
    different rule's pattern must never change this witness (#340)."""
    for rule in STANDALONE_PATTERNS:
        if rule.name == rule_name:
            anchors = SCANNER_ANCHORS.get(rule.name, ())
            return f"{rule.name}:{rule.pattern.pattern}:{rule.pattern.flags}:{anchors}"
    if rule_name == "json_secret_field":
        return (
            f"json_secret_field:{_JSON_CANDIDATE_RE.pattern}:{_JSON_CANDIDATE_RE.flags}:"
            f"{_JSON_SECRET_KEY_RE.pattern}:{_JSON_SECRET_KEY_RE.flags}"
        )
    if rule_name == "env_secret_kv":
        return (
            f"env_secret_kv:{_ENV_KEY_CANDIDATE_RE.pattern}:{_ENV_KEY_CANDIDATE_RE.flags}:"
            f"{_ENV_SECRET_KV_RE.pattern}:{_ENV_SECRET_KV_RE.flags}"
        )
    raise ValueError(f"unknown scanner rule: {rule_name}")


def _shared_fingerprint_dependency_names() -> frozenset[str]:
    """Module definitions fingerprinted as shared scanner infrastructure."""
    return _SHARED_FINGERPRINT_DEPENDENCIES


def _rule_data_dependency_names() -> frozenset[str]:
    """Rule definitions fingerprinted only by their per-rule witnesses."""
    return _RULE_DATA_DEPENDENCIES


def _canonical_fingerprint_value(value: Any, *, name: str = "dependency") -> str:
    """Serialize fingerprint inputs recursively and deterministically.

    No repr fallback is permitted: unsupported values make cache identity
    unavailable rather than silently introducing process-specific state.

    #355 (F3): every built-in-type branch below matches on ``type(value) is
    ...``, never ``isinstance`` -- a subclass (which could override
    iteration, equality, or hashing) falls through to the ``unsupported``
    branch instead of being silently treated as its base type.
    """
    value_type = type(value)
    if value is None:
        encoded: Any = ["NoneType", None]
    elif value_type is bool:
        encoded = ["bool", value]
    elif value_type is str:
        encoded = ["str", value]
    elif value_type is int:
        encoded = ["int", value]
    elif value_type is float:
        encoded = ["float", value.hex()]
    elif value_type is re.Pattern:
        if type(value.pattern) is not str or type(value.flags) is not int:
            # #357 (ChatGPT external review): a str/int subclass here (e.g.
            # re.compile() fed a str subclass) must not be silently treated
            # as the exact type it isn't -- no str()/int() coercion either,
            # since that would hide the very subclass mismatch this exists
            # to catch.
            raise FingerprintUnavailableError(
                f"unsupported scanner fingerprint value for {name}: "
                f"re.Pattern with non-exact pattern/flags type "
                f"({type(value.pattern).__name__}/{type(value.flags).__name__})"
            )
        encoded = ["pattern", value.pattern, value.flags]
    elif value_type is list or value_type is tuple:
        encoded = [value_type.__name__, [
            _canonical_fingerprint_value(item, name=name) for item in value
        ]]
    elif value_type is set or value_type is frozenset:
        values = [_canonical_fingerprint_value(item, name=name) for item in value]
        encoded = [value_type.__name__, sorted(values)]
    elif value_type is dict:
        pairs = [
            (_canonical_fingerprint_value(key, name=name), _canonical_fingerprint_value(item, name=name))
            for key, item in value.items()
        ]
        encoded = ["dict", sorted(pairs)]
    elif inspect.isfunction(value) or inspect.isclass(value):
        try:
            source = inspect.getsource(value)
        except (OSError, TypeError) as exc:
            raise FingerprintUnavailableError(
                f"cannot fingerprint scanner dependency {name}: {type(exc).__name__}"
            ) from exc
        encoded = ["source", source]
    else:
        raise FingerprintUnavailableError(
            f"unsupported scanner fingerprint value for {name}: {type(value).__name__}"
        )
    return json.dumps(encoded, ensure_ascii=False, separators=(",", ":"))


def _all_top_level_dependency_objects() -> dict[str, Any]:
    """Objects/bindings that must stay represented in the shared fingerprint."""
    objects: dict[str, Any] = {}
    for name in sorted(_shared_fingerprint_dependency_names()):
        if name == "RAW_TEXT_TAGS":
            value = _ScanHTMLParser.RAW_TEXT_TAGS
        elif name == "_shared_scan_version":
            value = inspect.unwrap(globals()[name])
        elif name == "_IMPORT_BINDINGS_IDENTITY":
            value = _import_bindings_identity()
        else:
            value = globals()[name]
        objects[name] = value
    return objects


@functools.lru_cache(maxsize=1)
def _shared_scan_version() -> str:
    """Fingerprint of the scanning infrastructure every rule depends on --
    HTML/JSON extraction, unescaping, and the excluded/secret-value
    heuristics. A change here invalidates EVERY rule's cache at once
    (correct and safe: none of them can be trusted to still behave the
    same way); a change to one rule's own pattern only invalidates that
    rule (see ``_rule_witness``). Cached for the process lifetime -- tests
    that monkeypatch shared helpers should not expect this to notice.

    Codex re-check on PR #343 (P1): the functions' own source text does
    NOT change when a table or class they read BY NAME changes --
    ``is_excluded_key_name`` references ``EXCLUDED_EXACT_NAMES``/
    ``_METRIC_NAME_TOKENS``/``_METRIC_SUBSTRINGS`` and
    ``_html_scan_variants`` references ``_ScanHTMLParser`` without either
    appearing in those functions' own source. Every transitive
    constant/class reachable from the hashed functions must be included
    explicitly, or an edit to one silently leaves stale approvals in
    place.

    #355 (F1/F2): scanner configuration -- every name in
    ``_SHARED_FINGERPRINT_DEPENDENCIES``/``_RULE_DATA_DEPENDENCIES``, plus
    ``_ScanHTMLParser``'s keyword defaults and ``RAW_TEXT_TAGS`` -- is
    defined only by this module's source and never changes in-process.
    ``inspect.getsource``/``lru_cache`` cannot see a live reassignment
    (e.g. ``_ScanHTMLParser.RAW_TEXT_TAGS = ...`` after import) or a
    closure/keyword-default mutation; a test that changes one of these in
    place must call ``cache_clear()`` on this function afterward, and must
    not assume that alone makes the new value visible to
    ``inspect.getsource``-based fingerprinting.

    #355 (F3): the whole build below -- dependency collection,
    serialization, hashing -- is one failure boundary. Every exception here
    (an unsupported/mis-shaped dependency, a cyclic container, a string
    that can't round-trip through UTF-8, an ``inspect.unwrap`` cycle)
    becomes ``FingerprintUnavailableError``, matching every other failure
    mode this function already fails safe on -- callers already treat that
    exception as "no trustworthy version; scan everything, cache nothing."
    """
    try:
        parts = []
        for name, value in sorted(_all_top_level_dependency_objects().items()):
            parts.append(f"{name}:{_canonical_fingerprint_value(value, name=name)}")
        parts.append(_canonical_fingerprint_value(_shared_fingerprint_dependency_names()))
        parts.append(_canonical_fingerprint_value(_rule_data_dependency_names()))
        payload = "\x00".join(parts).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
    except FingerprintUnavailableError:
        raise
    except Exception as exc:  # F3: any failure here must fail safe, never propagate
        # #357 (ChatGPT external review): never interpolate the caught
        # exception into this message -- an exception whose own __str__
        # raises would then blow up while WE are handling it, leaking past
        # this fallback instead of becoming a clean FingerprintUnavailableError.
        raise FingerprintUnavailableError("scanner fingerprint construction failed") from exc


def rule_version(rule_name: str, *, extra_version: str = "") -> str:
    """Version of a single named rule: shared scanning infra + this rule's
    OWN definition + the inherited-blob-decoder version. Two rules share a
    version only by coincidence; the same rule's version changes only when
    its own pattern, the shared scanning code, or the decoder changes."""
    payload = f"{_shared_scan_version()}\x00{_rule_witness(rule_name)}\x00{extra_version}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rule_cache_key(
    rule_name: str, content_sha: str, *, mode: str = "html", extra_version: str = "",
) -> str:
    return f"{rule_name}:{rule_version(rule_name, extra_version=extra_version)}:{mode}:{content_sha}"


def scan_pages(
    pages: dict[str, str],
    *,
    clean_cache: dict[str, bool] | None = None,
    inherited_blob_shas: dict[str, str] | None = None,
) -> None:
    """Scan all output pages destined for public pages before upload.

    Raises PublicationScanError if any sensitive internal path, credential,
    or call-text marker is detected in any page or inherited file.
    The exception message specifies filename, pattern name, and match count;
    the secret value itself is NEVER included.

    #340: each page's approval is keyed by content sha + the version of
    EACH rule, and records which rules it covers. When only one rule's
    version changes, every page's approval for every OTHER rule is reused
    as-is -- only the changed rule re-scans (the whole page, same as
    always; there is no fragment-level splitting here, so there are no
    seams to miss a canary at).
    """
    validate_publish_allowlist(pages.keys())
    original_cache = clean_cache if isinstance(clean_cache, dict) else None
    # Validate into a private working copy. If scanner versioning fails, caller
    # cache bytes/entries remain untouched and are not used during this scan.
    cache = validate_clean_cache(original_cache) if original_cache is not None else None
    if original_cache is not None and cache != original_cache:
        original_cache.clear()
        original_cache.update(cache)
    names = rule_names()
    try:
        extra_version = inherited_blob_decoder_version()
        artifact_mode = inherited_blob_artifact_mode
        page_modes = {fname: artifact_mode(fname) for fname in pages}
        _shared_scan_version()
        # Force every per-rule witness inside the fail-closed boundary; a
        # source-less rule definition must not bypass approval invalidation.
        for _name in names:
            _rule_witness(_name)
        fingerprint_available = True
    except FingerprintUnavailableError as exc:
        fingerprint_available = False
        page_modes = {
            fname: ("json" if fname.lower().endswith(".json") else "html")
            for fname in pages
        }
        cache = None  # never trust/read/write approvals without a stable version
        print(f"publish-scan: shared scanner fingerprint unavailable ({exc}); performing uncached full scan", file=sys.stderr)
    violations: list[str] = []
    # Every fresh (page, rule) approval this run confirms clean -- written
    # to the cache only once every page has passed, never partially (a
    # rejected run must not populate the cache at all, same as before).
    fresh_keys: list[str] = []

    for fname, content in sorted(pages.items()):
        if not isinstance(content, str):
            continue
        # If the viewer helper is absent in a publish-only checkout, its
        # canonical artifact selector is unavailable too. Use the equivalent
        # filename rule only for this uncached full scan; never reuse approvals.
        mode = page_modes[fname]
        is_json = mode == "json"
        blob_sha = (inherited_blob_shas or {}).get(fname)
        content_sha = blob_sha or hashlib.sha256(content.encode("utf-8")).hexdigest()

        page_rule_keys = (
            {
                name: rule_cache_key(name, content_sha, mode=mode, extra_version=extra_version)
                for name in names
            }
            if fingerprint_available and cache is not None else {}
        )
        if cache is not None:
            stale_rules = [name for name, key in page_rule_keys.items() if cache.get(key) is not True]
        else:
            stale_rules = list(names)
        if not stale_rules:
            continue  # every current rule already approved this exact content

        findings = scan_text(content, html_mode=not is_json, json_mode=is_json, rules=stale_rules)
        if findings:
            summary = ", ".join(f"{rule}: {cnt}" for rule, cnt in sorted(findings.items()))
            violations.append(f"{fname}: {summary}")
            continue
        if cache is not None:
            fresh_keys.extend(page_rule_keys[name] for name in stale_rules)

    if violations:
        details = "; ".join(violations)
        raise PublicationScanError(
            f"Publication rejected (ADR-036 rule 3): sensitive markers detected in {len(violations)} file(s): {details}"
        )
    if cache is not None:
        for key in fresh_keys:
            cache[key] = True
        # Drop any entry whose embedded version no longer matches that
        # rule's CURRENT version -- garbage-collects approvals a rule
        # version bump made stale, one rule at a time, never the whole
        # cache (the point of #340).
        current_versions = {name: rule_version(name, extra_version=extra_version) for name in names}
        kept: dict[str, bool] = {}
        for key, value in cache.items():
            rule_name, _, rest = key.partition(":")
            version, _, _ = rest.partition(":")
            if current_versions.get(rule_name) == version:
                kept[key] = value
        cache.clear()
        for key in list(kept)[-_cache_entry_bound():]:
            cache[key] = True
        if original_cache is not None:
            original_cache.clear()
            original_cache.update(cache)


def _inherited_blob_decoder_module():
    try:
        from scripts import techtree_viewer
    except ImportError:
        try:
            import techtree_viewer
        except ImportError as exc:
            raise FingerprintUnavailableError(
                "inherited blob decoder implementation unavailable"
            ) from exc
    return techtree_viewer


def inherited_blob_artifact_mode(path: str) -> str:
    """Select the artifact scan mode through the decoder's canonical helper."""
    try:
        selector = getattr(_inherited_blob_decoder_module(), "_inherited_blob_artifact_mode", None)
        if not callable(selector):
            raise FingerprintUnavailableError("inherited blob artifact-mode selector unavailable")
        return selector(path)
    except FingerprintUnavailableError:
        raise
    except Exception as exc:
        raise FingerprintUnavailableError("inherited blob artifact-mode selection failed") from exc


def inherited_blob_decoder_version() -> str:
    """Version of the external inherited-blob decode/interpretation pipeline.

    Bump when techtree_viewer changes base64, concatenated-gzip, UTF-8, or
    artifact-mode handling before inherited content reaches scan_pages.
    """
    decoder_module = _inherited_blob_decoder_module()
    decoder_version = getattr(decoder_module, "_inherited_blob_decoder_version", None)
    if not callable(decoder_version):
        raise FingerprintUnavailableError("inherited blob decoder source fingerprint unavailable")
    return decoder_version()


def scanner_version(*, extra_version: str = "") -> str:
    """Content-address scanner plus upstream decoders that gate inherited approval."""
    try:
        source = Path(__file__).read_bytes()
    except OSError as exc:
        raise PublicationScanError(
            f"Publication rejected (ADR-036 rule 3): cannot fingerprint scanner version: {exc}"
        ) from exc
    patterns = "\n".join(f"{r.name}:{r.pattern.pattern}:{r.pattern.flags}" for r in STANDALONE_PATTERNS)
    return hashlib.sha256(source + patterns.encode("utf-8") + extra_version.encode("utf-8")).hexdigest()


def clean_cache_key(
    content_sha: str,
    version: str | None = None,
    *,
    mode: str = "html",
    extra_version: str | None = None,
) -> str:
    if version is None:
        extra_version = inherited_blob_decoder_version() if extra_version is None else extra_version
        version = scanner_version(extra_version=extra_version)
    return f"{version}:{mode}:{content_sha}"


def cache_contains_clean(
    cache: Any,
    content_sha: str,
    version: str | None = None,
    *,
    mode: str = "html",
    extra_version: str = "",
) -> bool:
    """True only if EVERY current rule has an approval for this content sha
    (#340: a partial, some-rules-stale approval is not enough here -- this
    check gates skipping a fetch of content the caller has not even
    downloaded yet, so there is no page text available to re-scan the
    stale rules against). ``version`` is accepted for backward-compatible
    call signatures but unused -- versioning is per-rule now."""
    del version
    try:
        safe_cache = validate_clean_cache(cache)
        return all(
            safe_cache.get(rule_cache_key(name, content_sha, mode=mode, extra_version=extra_version)) is True
            for name in rule_names()
        )
    except FingerprintUnavailableError as exc:
        print(f"publish-scan: inherited cache fingerprint unavailable ({exc}); treating blob as cache miss", file=sys.stderr)
        return False
