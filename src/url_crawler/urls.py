from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize(url: str) -> str | None:
    """Return the canonical request form of url, or None if it is not a crawlable http(s) URL."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return None

    host = (parts.hostname or "").lower()
    if not host or any(character.isspace() for character in host):
        return None

    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != DEFAULT_PORTS[scheme]:
        authority = f"{authority}:{port}"
    userinfo, separator, _ = parts.netloc.rpartition("@")
    if separator:
        authority = f"{userinfo}@{authority}"

    normalized = urlunsplit((scheme, authority, parts.path or "/", parts.query, ""))
    return normalized if len(normalized) <= MAX_URL_LENGTH else None


def resolve_href(href: str, base_url: str) -> str | None:
    """Strip whitespace, join href against base_url, normalize."""
    return normalize(urljoin(base_url, href.strip()))


def canonical_key(url: str) -> str:
    """Dedup identity for a normalized url: same as url but with query parameters sorted."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = "&".join(sorted(parts.query.split("&")))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


@dataclass(frozen=True, slots=True)
class HostScope:
    host: str
    port: int | None

    @classmethod
    def from_url(cls, url: str) -> HostScope:
        """Build the scope of an already normalized URL."""
        parts = urlsplit(url)
        return cls(host=(parts.hostname or "").lower(), port=parts.port)

    def allows(self, url: str) -> bool:
        normalized = normalize(url)
        return normalized is not None and HostScope.from_url(normalized) == self
