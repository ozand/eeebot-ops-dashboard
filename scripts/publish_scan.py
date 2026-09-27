"""ADR-036: Publication rejection scanner for ops dashboard gh-pages.

Rejects publication loudly (rc != 0) if any page contains secrets, credentials,
internal paths (/etc/eeepc-agent), or structural call-text markers.
Used by publish_to_pages and autopublish dry-run; reused by D2 masking.
"""
from __future__ import annotations

import html as _html
from html.parser import HTMLParser
import re
from typing import Iterable, NamedTuple, Pattern


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


def _unescape_until_stable(text: str, max_rounds: int = 5) -> str:
    """Unescape entities with a bound; refuse if another decode round is needed."""
    current = text
    for _ in range(max_rounds):
        decoded = _html.unescape(current)
        if decoded == current:
            return current
        current = decoded
    if _html.unescape(current) != current:
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
    """Return independently scanned text and attribute streams from one HTML fragment."""
    parser = _ScanHTMLParser()
    raw_parser = _ScanHTMLParser(collect_raw_text=True)
    try:
        parser.feed(markup)
        parser.close()
        raw_parser.feed(markup)
        raw_parser.close()
    except Exception:
        # Malformed HTML is still scanned as source; discard partial parser output.
        return []

    variants = [
        _unescape_until_stable("".join(parser.text_parts)),
        *(_unescape_until_stable(value) for value in parser.attribute_values),
    ]
    for fragment in raw_parser.raw_text_parts:
        # Raw-text nodes may contain JS strings that assemble HTML. Parse the
        # fragment with the same separation between text and attributes.
        variants.extend(_html_scan_variants(fragment))
    return variants


def scan_text(content: str) -> dict[str, int]:
    """Scan string content and return counts of all matched leak patterns."""
    findings: dict[str, int] = {}
    unescaped = _unescape_until_stable(content)
    # Parse unescaped markup so entity-encoded tag delimiters become markup
    # before tokenization. Keep raw-text bodies separately: browsers may execute
    # markup assembled from strings inside script/style/textarea/title content.
    variants = list(dict.fromkeys([content, unescaped, *_html_scan_variants(unescaped)]))

    for rule in STANDALONE_PATTERNS:
        total = max(len(rule.pattern.findall(v)) for v in variants)
        if total:
            findings[rule.name] = total

    json_hits = 0
    for v in variants:
        hits = 0
        for match in _JSON_SECRET_KEY_RE.finditer(v):
            key = match.group(1).lower()
            val = match.group(2) or match.group(3) or ""
            if is_excluded_key_name(key):
                continue
            if is_secret_value(val):
                hits += 1
        json_hits = max(json_hits, hits)
    if json_hits:
        findings["json_secret_field"] = json_hits

    env_hits = 0
    for v in variants:
        hits = 0
        for match in _ENV_SECRET_KV_RE.finditer(v):
            key = match.group(1)
            val = match.group(2) or match.group(3) or match.group(4) or ""
            if is_excluded_key_name(key):
                continue
            if is_secret_value(val):
                hits += 1
        env_hits = max(env_hits, hits)
    if env_hits:
        findings["env_secret_kv"] = env_hits

    return findings


def scan_pages(pages: dict[str, str]) -> None:
    """Scan all output pages destined for public pages before upload.

    Raises PublicationScanError if any sensitive internal path, credential,
    or call-text marker is detected in any page or inherited file.
    The exception message specifies filename, pattern name, and match count;
    the secret value itself is NEVER included.
    """
    validate_publish_allowlist(pages.keys())
    violations: list[str] = []
    for fname, content in sorted(pages.items()):
        if not isinstance(content, str):
            continue
        findings = scan_text(content)
        if findings:
            summary = ", ".join(f"{rule}: {cnt}" for rule, cnt in sorted(findings.items()))
            violations.append(f"{fname}: {summary}")

    if violations:
        details = "; ".join(violations)
        raise PublicationScanError(
            f"Publication rejected (ADR-036 rule 3): sensitive markers detected in {len(violations)} file(s): {details}"
        )
