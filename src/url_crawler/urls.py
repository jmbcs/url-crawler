from __future__ import annotations

import re
import string
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}

UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_SUB_DELIMITERS = "!$&'()*+,;="
_PATH_SAFE = f"/%:@{_SUB_DELIMITERS}"
_QUERY_SAFE = f"{_PATH_SAFE}?"
_PERCENT_ESCAPE = re.compile("%([0-9A-Fa-f]{2})")
_CONTROL_CHARACTER = re.compile("[\x00-\x1f\x7f]")


def normalize(url: str) -> str | None:
    """Return the canonical request form of url, or None if it is not a crawlable http(s) URL."""
    text = url.strip()
    # urlsplit drops tab and newline silently, and a printed ESC or BEL drives the reader's
    # terminal, so control bytes are rejected before anything parses them.
    if _CONTROL_CHARACTER.search(text):
        return None

    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return None

    host = (parts.hostname or "").lower()
    if not host or any(character.isspace() for character in host):
        return None

    # Credentials identify the caller, not the resource, so they are never crawled or printed.
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != DEFAULT_PORTS[scheme]:
        authority = f"{authority}:{port}"

    path = _remove_dot_segments(parts.path or "/")
    normalized = urlunsplit((scheme, authority, path, parts.query, ""))
    return normalized if len(normalized) <= MAX_URL_LENGTH else None


def _remove_dot_segments(path: str) -> str:
    """Resolve . and .. segments per RFC 3986 section 5.2.4, keeping any trailing slash."""
    segments = path.split("/")
    output: list[str] = []
    for index, segment in enumerate(segments):
        if segment not in (".", ".."):
            output.append(segment)
            continue
        if segment == ".." and len(output) > 1:
            output.pop()
        if index == len(segments) - 1:
            output.append("")
    return "/".join(output) or "/"


def prepare_seed(url: str) -> str:
    """Default a missing scheme to https and reject anything but http(s)."""
    if "://" not in url:
        return f"https://{url}"
    scheme = url.split("://", 1)[0].lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"unsupported URL scheme {scheme!r}: use http:// or https://")
    return url


def resolve_href(href: str, base_url: str) -> str | None:
    """Strip whitespace, join href against base_url, normalize."""
    return normalize(urljoin(base_url, href.strip()))


def canonical_key(url: str) -> str:
    """Dedup identity for a normalized url: escapes folded and query parameters sorted by key."""
    parts = urlsplit(url)
    path = _canonical_escapes(parts.path, _PATH_SAFE)
    query = _canonical_escapes(_sorted_query(parts.query), _QUERY_SAFE)
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def _sorted_query(query: str) -> str:
    """Parameters ordered by key; the stable sort keeps repeated keys in their given order."""
    return "&".join(sorted(query.split("&"), key=lambda pair: pair.split("=", 1)[0]))


def _canonical_escapes(text: str, safe: str) -> str:
    return quote(_PERCENT_ESCAPE.sub(_fold_escape, text), safe=safe)


def _fold_escape(match: re.Match[str]) -> str:
    character = chr(int(match.group(1), 16))
    return character if character in UNRESERVED else f"%{match.group(1).upper()}"


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
