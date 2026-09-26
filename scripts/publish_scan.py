"""ADR-036: Publication rejection scanner for ops dashboard gh-pages.

Rejects publication loudly (rc != 0) if any page contains secrets, credentials,
internal paths (/etc/eeepc-agent), or structural call-text markers.
Used by publish_to_pages and autopublish dry-run; reused by D2 masking.
"""
from __future__ import annotations

import html as _html
from html.parser import HTMLParser
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Pattern


class PublicationScanError(Exception):
    """Raised when publication is rejected due to leaked secrets or call text."""


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


def _compile_scanner_patterns(patterns: tuple[SecretPattern, ...]) -> re.Pattern[str]:
    branches = []
    for rule in patterns:
        source = rule.pattern.pattern
        flags = rule.pattern.flags
        if source.startswith("(?i)"):
            source = source[4:]
            flags |= re.IGNORECASE
        inline = "(?i:" + source + ")" if flags & re.IGNORECASE else "(?:" + source + ")"
        branches.append(f"(?P<{rule.name}>{inline})")
    branches.extend((
        r"(?P<json_secret>(?i:['\"])(?P<json_key>[a-z0-9_]*(?:password|secret|api[_-]?key|access_token|auth_token|token)[a-z0-9_]*)(?i:['\"])\s*:\s*(?:\"(?P<json_dval>[^\"]+)\"|'(?P<json_sval>[^']+)'|(?P<json_uval>[^,}\s]+)))",
        r"(?P<env_secret>(?i:\b)(?P<env_key>[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)[A-Za-z0-9_]*)(?i:\s*[:=]\s*)(?:\"(?P<env_dval>[^\"]{8,})\"|'(?P<env_sval>[^']{8,})'|(?P<env_uval>[^\"'<>\s$]{8,})))",
    ))
    return re.compile("|".join(branches))


@lru_cache(maxsize=4)
def _combined_pattern(patterns: tuple[SecretPattern, ...]) -> re.Pattern[str]:
    return _compile_scanner_patterns(patterns)


@lru_cache(maxsize=4)
def _candidate_needles(patterns: tuple[SecretPattern, ...]) -> tuple[str, ...]:
    # Candidate detection is a skip optimization only; a hit is always followed
    # by the full merged scanner over the complete representation.
    fixed = {
        "/etc/eeepc-agent", "sk-", "ghp_", "gho_", "ghs_", "ghu_", "github_pat_",
        "bearer ", "akia", "xox", "authorization:", "http://", "https://",
        "private key", "'reasoning_content'", '"reasoning_content"',
        "'messages'", '"messages"', "'prompt'", '"prompt"',
        '"password"', "'password'", '"secret"', "'secret'", '"api_key"', "'api_key'",
        '"api-key"', "'api-key'", '"access_token"', "'access_token'",
        '"auth_token"', "'auth_token'", '"token"', "'token'", "_password=", "_password:", "_secret=", "_secret:",
        "_token=", "_token:", "_key=", "_key:", "password=", "password:",
        "secret=", "secret:", "token=", "token:", "api_key=", "api_key:",
    }
    dynamic = {
        rule.pattern.pattern.lower()
        for rule in patterns
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{8,}", rule.pattern.pattern)
    }
    # Keep in sync with the full environment / JSON secret-key regexes. These
    # candidates are intentionally broad: any matching key shape triggers the
    # full scan, which still applies exclusions and secret-value checks.
    fixed.update({"_pass=", "_pass:", "_auth=", "_auth:", "auth=", "auth:"})
    # JSON key regex accepts arbitrary prefixes/suffixes around secret terms.
    return tuple(fixed | dynamic)


_JSON_CANDIDATE_RE = re.compile(
    r"(?i:[\"'][a-z0-9_]*(?:password|secret|api[_-]?key|access_token|auth_token|token)[a-z0-9_]*[\"']\s*:)"
)


def _has_scan_candidate(text: str, patterns: tuple[SecretPattern, ...]) -> bool:
    lowered = text.lower()
    return (
        any(lowered.find(needle) >= 0 for needle in _candidate_needles(patterns))
        or _JSON_CANDIDATE_RE.search(text) is not None
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
    RAW_TEXT_TAGS = {"script", "style", "textarea", "title"}

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


def scan_text(content: str, *, html_mode: bool = True, json_mode: bool = False) -> dict[str, int]:
    """Scan text and return rule hit counts; parser work is selected by artifact type."""
    findings: dict[str, int] = {}
    source_variants = [content, _unescape_until_stable(content)]
    if json_mode:
        try:
            decoded = json.loads(content)
        except (json.JSONDecodeError, UnicodeError):
            pass  # malformed JSON: scan raw and decoded source fail-closed
        else:
            strings = [_unescape_until_stable(value) for value in _json_strings(decoded)]
            if strings:
                source_variants.append("\x00".join(strings))
    elif html_mode:
        source_variants.extend(_html_scan_variants(source_variants[-1]))
    variants = list(dict.fromkeys(source_variants))

    pattern_signature = tuple(
        SecretPattern(rule.name, rule.pattern, rule.description)
        for rule in STANDALONE_PATTERNS
    )
    combined = _combined_pattern(pattern_signature)
    standalone_names = {rule.name for rule in pattern_signature}
    # One regex traversal over every representation that contains a candidate
    # marker; ordinary prose/pages avoid a costly no-hit regex walk. If the
    # inexpensive literal guard matches, the full content still gets scanned.
    scan_variants = [variant for variant in variants if _has_scan_candidate(variant, pattern_signature)]
    if scan_variants:
        # Scan content and each parser-derived stream independently. Null
        # joining would prevent boundary-spanning matches but still causes a
        # regex engine to revisit the whole multi-megabyte body per variant.
        matches = (match for variant in scan_variants for match in combined.finditer(variant))
    else:
        matches = ()
    for match in matches:
        json_key = match.group("json_key")
        if json_key is not None:
            val = match.group("json_dval") or match.group("json_sval") or match.group("json_uval") or ""
            if not is_excluded_key_name(json_key) and is_secret_value(val):
                findings["json_secret_field"] = findings.get("json_secret_field", 0) + 1
            continue
        env_key = match.group("env_key")
        if env_key is not None:
            val = match.group("env_dval") or match.group("env_sval") or match.group("env_uval") or ""
            if not is_excluded_key_name(env_key) and is_secret_value(val):
                findings["env_secret_kv"] = findings.get("env_secret_kv", 0) + 1
            continue
        for name in standalone_names:
            if match.group(name) is not None:
                findings[name] = findings.get(name, 0) + 1
                break
    return findings


_CACHE_KEY_RE = re.compile(r"^[0-9a-f]{64}:(?:html|json):(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def validate_clean_cache(value: Any) -> dict[str, bool]:
    """Return a safe cache or empty mapping; any malformed entry invalidates all."""
    if not isinstance(value, dict):
        return {}
    if len(value) > 256 or any(
        not isinstance(key, str) or not _CACHE_KEY_RE.fullmatch(key) or clean is not True
        for key, clean in value.items()
    ):
        return {}
    return dict(value)


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
    """
    validate_publish_allowlist(pages.keys())
    cache = clean_cache if isinstance(clean_cache, dict) else None
    if cache is not None:
        validated = validate_clean_cache(cache)
        cache.clear()
        cache.update(validated)
    version = scanner_version()
    violations: list[str] = []
    clean_keys: list[str] = []
    for fname, content in sorted(pages.items()):
        if not isinstance(content, str):
            continue
        is_json = fname.lower().endswith(".json")
        mode = "json" if is_json else "html"
        blob_sha = (inherited_blob_shas or {}).get(fname)
        content_sha = blob_sha or hashlib.sha256(content.encode("utf-8")).hexdigest()
        cache_key = clean_cache_key(content_sha, version, mode=mode)
        clean_keys.append(cache_key)
        if cache is not None and cache.get(cache_key) is True:
            continue
        findings = scan_text(content, html_mode=not is_json, json_mode=is_json)
        if findings:
            summary = ", ".join(f"{rule}: {cnt}" for rule, cnt in sorted(findings.items()))
            violations.append(f"{fname}: {summary}")

    if violations:
        details = "; ".join(violations)
        raise PublicationScanError(
            f"Publication rejected (ADR-036 rule 3): sensitive markers detected in {len(violations)} file(s): {details}"
        )
    if cache is not None:
        for key in clean_keys:
            cache[key] = True
        # The cache is metadata only and bounded across content churn.
        current_prefix = f"{version}:"
        current_keys = [key for key in cache if key.startswith(current_prefix)]
        cache.clear()
        for key in current_keys[-256:]:
            cache[key] = True


def scanner_version() -> str:
    """Content-address scanner implementation and patterns to invalidate old cache entries."""
    try:
        source = Path(__file__).read_bytes()
    except OSError as exc:
        raise PublicationScanError(
            f"Publication rejected (ADR-036 rule 3): cannot fingerprint scanner version: {exc}"
        ) from exc
    patterns = "\n".join(f"{r.name}:{r.pattern.pattern}:{r.pattern.flags}" for r in STANDALONE_PATTERNS)
    return hashlib.sha256(source + patterns.encode("utf-8")).hexdigest()


def clean_cache_key(
    content_sha: str, version: str | None = None, *, mode: str = "html"
) -> str:
    return f"{version or scanner_version()}:{mode}:{content_sha}"


def cache_contains_clean(
    cache: Any, content_sha: str, version: str | None = None, *, mode: str = "html"
) -> bool:
    safe_cache = validate_clean_cache(cache)
    return safe_cache.get(clean_cache_key(content_sha, version, mode=mode)) is True
