"""Navigation policy enforced on every top-level / frame document request.

The agent is general purpose, so the open web is allowed by default, but:
  * only http(s) is permitted (no file:, javascript:, chrome:, etc.),
  * loopback / private-network addresses are refused (prevents the agent from
    being steered by a web page into poking at local services),
  * ``BLOCKED_DOMAINS`` are always refused,
  * when ``ALLOWED_DOMAINS`` is configured, only those domains are reachable.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from config import settings

INTERNAL_SCHEMES = {"about", "blob", "data", "chrome-error"}


def _domain_matches(host: str, domain: str) -> bool:
    domain = domain.lstrip(".")
    return host == domain or host.endswith("." + domain)


def _is_private_host(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def check_url(url: str, *, internal_ok: bool = True) -> tuple[bool, str | None]:
    """Return (allowed, reason_if_blocked)."""
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False, "malformed URL"
    scheme = (parsed.scheme or "").lower()
    if scheme in INTERNAL_SCHEMES:
        return (True, None) if internal_ok else (False, f"'{scheme}:' URLs are not allowed")
    if scheme not in {"http", "https"}:
        return False, f"'{scheme or 'unknown'}:' URLs are not allowed"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "URL has no host"
    if not settings.allow_private_network and _is_private_host(host):
        return False, f"private/local network address '{host}' is blocked"
    for domain in settings.blocked_domains:
        if _domain_matches(host, domain):
            return False, f"domain '{host}' is on the blocklist"
    if settings.allowed_domains and not any(_domain_matches(host, d) for d in settings.allowed_domains):
        return False, f"domain '{host}' is not on the allowlist"
    return True, None


def normalize_user_url(url: str) -> str:
    """Accept 'youtube.com' style input from the LLM and make it a URL."""
    url = url.strip()
    if "://" not in url and not url.startswith(("about:", "data:")):
        url = "https://" + url
    return url
