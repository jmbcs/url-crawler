from __future__ import annotations

from selectolax.lexbor import LexborHTMLParser, LexborNode

from url_crawler.urls import resolve_href

LINK_SELECTOR = "a[href], area[href]"


def extract_links(body: bytes, base_url: str) -> list[str]:
    """Crawlable hyperlinks of an HTML body, deduplicated in document order."""
    tree = LexborHTMLParser(body)
    base = _effective_base(tree, base_url)
    hrefs = (_href(node) for node in tree.css(LINK_SELECTOR))
    resolved = (resolve_href(href, base) for href in hrefs if href is not None)
    return list(dict.fromkeys(url for url in resolved if url is not None))


def _href(node: LexborNode) -> str | None:
    """None when the attribute is absent or holds bytes that are not valid UTF-8."""
    try:
        return node.attrs.get("href")
    except UnicodeDecodeError:
        return None


def _effective_base(tree: LexborHTMLParser, base_url: str) -> str:
    node = tree.css_first("base[href]")
    href = _href(node) if node is not None else None
    resolved = resolve_href(href, base_url) if href else None
    return resolved or base_url
