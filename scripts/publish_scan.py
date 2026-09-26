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
_ENV_KEY_CANDIDATE_RE = re.compile(
    r'(?i)\b[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)[A-Za-z0-9_]*\s*[:=]'
)


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

    lower_cache: dict[str, str] = {}
    for rule in STANDALONE_PATTERNS:
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
    for variant in variants:
        if _JSON_CANDIDATE_RE.search(variant):
            hits = 0
            for match in _JSON_SECRET_KEY_RE.finditer(variant):
                key = match.group(1)
                value = match.group(2) or match.group(3) or ""
                if not is_excluded_key_name(key) and is_secret_value(value):
                    hits += 1
            json_count = max(json_count, hits)
        if _ENV_KEY_CANDIDATE_RE.search(variant):
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
    content_sha: str, version: str | None = None, *, mode: str = "html", extra_version: str = ""
) -> str:
    return f"{version or scanner_version(extra_version=extra_version)}:{mode}:{content_sha}"


def cache_contains_clean(
    cache: Any,
    content_sha: str,
    version: str | None = None,
    *,
    mode: str = "html",
    extra_version: str = "",
) -> bool:
    safe_cache = validate_clean_cache(cache)
    return safe_cache.get(
        clean_cache_key(content_sha, version, mode=mode, extra_version=extra_version)
    ) is True
