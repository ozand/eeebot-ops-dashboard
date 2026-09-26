"""ADR-036: Publication rejection scanner for ops dashboard gh-pages.

Rejects publication loudly (rc != 0) if any page contains secrets, credentials,
internal paths (/etc/eeepc-agent), or structural call-text markers.
Used by publish_to_pages and autopublish dry-run; reused by D2 masking.
"""
from __future__ import annotations

import html as _html
import re
from typing import NamedTuple, Pattern


class PublicationScanError(Exception):
    """Raised when publication is rejected due to leaked secrets or call text."""


PublishScanError = PublicationScanError


class SecretPattern(NamedTuple):
    name: str
    pattern: Pattern[str]
    description: str


EXCLUDED_NAME_SUBSTRINGS = frozenset({
    "count", "ratio", "rate", "limit", "floor", "budget", "window",
    "duration", "hours", "seconds", "tokens_per_integration", "prompt_tokens",
    "completion_tokens", "total_tokens", "self_hosted_tokens", "vendor_tokens",
})

EXCLUDED_EXACT_NAMES = frozenset({
    "key", "keys", "pass", "passive", "max_tokens", "token_count",
})


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
    if re.search(r"[;{}()\[\]]|==|!=|=>", v):
        return False
    return True


STANDALONE_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern("eeepc_agent_path", re.compile(r"/etc/eeepc-agent"), "internal /etc/eeepc-agent path"),
    SecretPattern("openai_secret_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "OpenAI secret key format"),
    SecretPattern("github_token", re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9_]{16,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "GitHub token"),
    SecretPattern("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}\b"), "Bearer token header"),
    SecretPattern("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    SecretPattern("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "Slack API token"),
    SecretPattern("basic_auth", re.compile(r"(?i)\bAuthorization\s*:\s*Basic\s+[A-Za-z0-9+/=]{10,}\b|\bBasic\s+[A-Za-z0-9+/=]{16,}\b"), "Basic Auth header"),
    SecretPattern("url_credentials", re.compile(r"https?://[^:\s/\"']+:[^@\s/\"']+@[^/\s\"']+"), "URL containing embedded credentials"),
    SecretPattern("structural_reasoning_content", re.compile(r'"reasoning_content"'), "call marker reasoning_content"),
    SecretPattern("structural_messages", re.compile(r'"messages"\s*:'), "call marker messages"),
    SecretPattern("structural_prompt", re.compile(r'"prompt"\s*:\s*\{'), "call marker prompt object"),
)


_JSON_SECRET_KEY_RE = re.compile(
    r'(?i)"([a-z0-9_]*(?:password|secret|api[_-]?key|access_token|auth_token|token)[a-z0-9_]*)"\s*:\s*"([^"]+)"'
)
_ENV_SECRET_KV_RE = re.compile(
    r'(?i)\b([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)[A-Za-z0-9_]*)\s*[:=]\s*["\']?([^"\'<>\s$]{8,})["\']?'
)


def scan_text(content: str) -> dict[str, int]:
    """Scan string content and return counts of all matched leak patterns."""
    findings: dict[str, int] = {}
    unescaped = _html.unescape(content)
    has_entities = unescaped != content

    for rule in STANDALONE_PATTERNS:
        raw_count = len(rule.pattern.findall(content))
        une_count = len(rule.pattern.findall(unescaped)) if has_entities else 0
        total = max(raw_count, une_count)
        if total:
            findings[rule.name] = total

    json_raw = 0
    for match in _JSON_SECRET_KEY_RE.finditer(content):
        key, val = match.group(1).lower(), match.group(2)
        if any(sub in key for sub in EXCLUDED_NAME_SUBSTRINGS) or key in EXCLUDED_EXACT_NAMES:
            continue
        if is_secret_value(val):
            json_raw += 1
    json_une = 0
    if has_entities:
        for match in _JSON_SECRET_KEY_RE.finditer(unescaped):
            key, val = match.group(1).lower(), match.group(2)
            if any(sub in key for sub in EXCLUDED_NAME_SUBSTRINGS) or key in EXCLUDED_EXACT_NAMES:
                continue
            if is_secret_value(val):
                json_une += 1
    json_hits = max(json_raw, json_une)
    if json_hits:
        findings["json_secret_field"] = json_hits

    env_raw = 0
    for match in _ENV_SECRET_KV_RE.finditer(content):
        key, val = match.group(1), match.group(2)
        key_lower = key.lower()
        if any(sub in key_lower for sub in EXCLUDED_NAME_SUBSTRINGS) or key_lower in EXCLUDED_EXACT_NAMES:
            continue
        if is_secret_value(val):
            env_raw += 1
    env_une = 0
    if has_entities:
        for match in _ENV_SECRET_KV_RE.finditer(unescaped):
            key, val = match.group(1), match.group(2)
            key_lower = key.lower()
            if any(sub in key_lower for sub in EXCLUDED_NAME_SUBSTRINGS) or key_lower in EXCLUDED_EXACT_NAMES:
                continue
            if is_secret_value(val):
                env_une += 1
    env_hits = max(env_raw, env_une)
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
