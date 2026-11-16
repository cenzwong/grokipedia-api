"""
Grokipedia API client
=====================

Unofficial, zero-configuration client for https://grokipedia.com that exposes
the endpoints the website itself uses:

    1. Search suggestions (typeahead JSON API)
       GET /api/typeahead?v=2&query=<q>&limit=<n>

    2. Full-text search (server-rendered HTML, parsed here into dicts)
       GET /search?q=<q>&page=<n>

    3. Page content (JSON API, returns the page as Markdown)
       GET /api/page-preview?slug=<slug>&content=1   -> page with Markdown content
       GET /api/page-preview?slug=<slug>&content=0   -> metadata/citations only

    4. Site statistics (bonus)
       GET /api/stats

Authentication
--------------
None. Grokipedia's read API is public; no login, token or API key is required.
The only cookies the browser sends are an affinity-routing cookie and
Cloudflare's short-lived `__cf_bm` cookie. Both are optional: every endpoint
below works without any cookie. The affinity cookie observed in traffic is
hardcoded (`AFFINITY_COOKIE`) for fingerprint parity, and the client
transparently drops cookies and retries if a stale cookie ever causes a
401/403. There is no token/login flow to refresh, so there is nothing to
re-authenticate.

Transport
---------
`httpcloak` is used first (browser TLS/HTTP2 fingerprint) when installed, with
automatic fallback to `requests`. Both talk plain HTTPS; no headless browser is
needed because Cloudflare does not challenge these public endpoints.

Quick start
-----------
    from api_client import GrokipediaClient

    client = GrokipediaClient()

    hits = client.search("Albert Einstein", page=1)
    for hit in hits["results"]:
        print(hit["title"], hit["slug"])

    page = client.get_page("Albert_Einstein")
    print(page["title"], len(page["content"]))          # Markdown content
    print(page["page"]["citations"][0]["url"])

    print(client.suggest("quantum", limit=5))

CLI
---
    python api_client.py search "Albert Einstein" --page 1
    python api_client.py suggest "Albert Einstein" --limit 5
    python api_client.py page Albert_Einstein --content
    python api_client.py page Albert_Einstein --no-content
    python api_client.py stats

Notes / caveats
---------------
* The search results page is HTML (there is no public search JSON endpoint).
  This client parses the same markup the site renders; if Grokipedia changes
  its markup the `_SearchParser` may need a tweak.
* Results are Cloudflare-cached for ~5 minutes and can shift over time.
* Be polite: reuse a single client/session and avoid hammering the endpoints.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from typing import Any, Optional

import requests

try:  # preferred transport (browser TLS / HTTP2 fingerprint)
    import httpcloak  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    httpcloak = None  # type: ignore


BASE_URL = "https://grokipedia.com"

# Cookies observed in the browser traffic. `grokipedia-affinity` is a stable
# routing cookie; `__cf_bm` is Cloudflare's ~30 min bot-management cookie and is
# intentionally NOT sent by default (it expires fast and is not required).
AFFINITY_COOKIE = (
    "grokipedia-affinity="
    "f6880ce46b6eee344e1fbd2ee7c34771|ed66aa1bc0dc796acaf359230d461129"
)
CF_BM_COOKIE = "YxLS1LSbZUo4kkKOPOqjOeD6L"  # ephemeral, informational only

# Browser-like default headers (the API happily serves these with no cookies).
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/157.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE_URL}/",
}

_DEFAULT_COOKIES = {"grokipedia-affinity": AFFINITY_COOKIE.split("=", 1)[1]}


class GrokipediaError(RuntimeError):
    """Raised when a Grokipedia request cannot be completed."""


class _Response:
    """Minimal transport-agnostic response wrapper."""

    __slots__ = ("status_code", "text")

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        return json.loads(self.text)


# --------------------------------------------------------------------------- #
# HTML parsing for /search (server-rendered results)
# --------------------------------------------------------------------------- #
class _SearchParser(HTMLParser):
    """Extract result rows from a Grokipedia /search HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, Any]] = []
        self._cur: Optional[dict[str, Any]] = None
        self._in_title = False
        self._in_snippet = False
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        a = dict(attrs)
        if tag == "a" and a.get("data-search-result-link") == "true":
            self._cur = {
                "href": a.get("href"),
                "slug": a.get("data-slug") or "",
                "snippet": a.get("data-search-snippet") or "",
                "title": "",
            }
            self._title_parts = []
            self._snippet_parts = []
        if self._cur is not None and tag == "h3":
            self._in_title = True
        if self._cur is not None and tag == "p":
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3":
            self._in_title = False
        elif tag == "p":
            self._in_snippet = False
        elif tag == "a" and self._cur is not None:
            title = " ".join("".join(self._title_parts).split())
            parsed_snippet = " ".join("".join(self._snippet_parts).split())
            self._cur["title"] = title
            if not self._cur["snippet"]:
                self._cur["snippet"] = parsed_snippet
            href = self._cur.get("href") or ""
            self._cur["url"] = href if href.startswith("http") else BASE_URL + href
            self.results.append(self._cur)
            self._cur = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)


_TOTAL_RE = re.compile(r"([\d,]+)\s*</span>\s*results?\b")
_PAGE_RE = re.compile(r"Page\s+(\d+)\s+of\s+([\d,]+)")


def _parse_search_html(html_text: str, query: str, page: int) -> dict[str, Any]:
    parser = _SearchParser()
    parser.feed(html_text)

    total: Optional[int] = None
    m = _TOTAL_RE.search(html_text)
    if m:
        total = int(m.group(1).replace(",", ""))

    total_pages: Optional[int] = None
    m = _PAGE_RE.search(html_text)
    if m:
        page = int(m.group(1))
        total_pages = int(m.group(2).replace(",", ""))

    return {
        "query": query,
        "page": page,
        "total_results": total,
        "total_pages": total_pages,
        "results": parser.results,
    }


_SLUG_RE = re.compile(r"/page/([^/?#]+)")


def _normalize_slug(slug: str) -> str:
    """Accept a bare slug or a full Grokipedia page URL."""
    slug = slug.strip()
    m = _SLUG_RE.search(slug)
    if m:
        slug = m.group(1)
    # Page URLs percent-encode; API expects the readable slug.
    from urllib.parse import unquote

    return unquote(slug).strip("/")


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class GrokipediaClient:
    """Zero-config client for Grokipedia search and page content."""

    def __init__(
        self,
        timeout: int = 30,
        prefer_httpcloak: bool = True,
        cookies: Optional[dict[str, str]] = None,
    ) -> None:
        self.timeout = timeout
        self.cookies: dict[str, str] = dict(cookies or _DEFAULT_COOKIES)

        self._hc = None
        if prefer_httpcloak and httpcloak is not None:
            try:
                self._hc = httpcloak.Session(
                    preset="chrome-latest", timeout=timeout, allow_redirects=True
                )
            except Exception:
                self._hc = None

        self._rs = requests.Session()
        self._rs.headers.update(DEFAULT_HEADERS)

    # -- transports -------------------------------------------------------- #
    def _get_httpcloak(self, url: str, params: dict, headers: dict) -> _Response:
        r = self._hc.get(url, params=params, headers=headers, cookies=self.cookies or None)
        return _Response(int(r.status_code), r.text)

    def _get_requests(self, url: str, params: dict, headers: dict) -> _Response:
        r = self._rs.get(
            url,
            params=params,
            headers=headers,
            cookies=self.cookies or None,
            timeout=self.timeout,
        )
        return _Response(r.status_code, r.text)

    def _transports(self) -> list:
        transports = [self._get_requests]
        if self._hc is not None:
            transports.insert(0, self._get_httpcloak)
        return transports

    def _request(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        accept: str = "application/json",
    ) -> _Response:
        url = BASE_URL + path
        headers = {**DEFAULT_HEADERS, "Accept": accept}
        errors: list[str] = []

        for transport in self._transports():
            attempts = 2
            for attempt in range(attempts):
                try:
                    resp = transport(url, params or {}, headers)
                except Exception as exc:  # network / TLS error -> try next transport
                    errors.append(f"{transport.__name__}: {exc}")
                    break

                # Stale cookie can occasionally trip Cloudflare: drop and retry.
                if resp.status_code in (401, 403) and self.cookies and attempt == 0:
                    self.cookies = {}
                    continue

                if resp.status_code == 200:
                    return resp

                errors.append(f"{transport.__name__}: HTTP {resp.status_code}")
                break

        raise GrokipediaError(
            f"GET {url} failed after trying all transports: {'; '.join(errors)}"
        )

    # -- search ------------------------------------------------------------ #
    def suggest(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Autocomplete/typeahead search.

        Calls the JSON endpoint the site's search box uses.

        Args:
            query: search text.
            limit: max suggestions (server returns at most all matches; ~64
                for a broad query even if a larger limit is requested).

        Returns:
            ``{"query": str, "count": int, "results": [ {...}, ... ]}`` where
            each result has slug, title, snippet, relevanceScore, viewCount...
        """
        resp = self._request(
            "/api/typeahead",
            {"v": "2", "query": query, "limit": str(limit)},
        )
        data = resp.json()
        results = data.get("results", []) if isinstance(data, dict) else []
        return {
            "query": query,
            "count": len(results),
            "search_time_ms": data.get("searchTimeMs") if isinstance(data, dict) else None,
            "results": results,
        }

    def search(self, query: str, page: int = 1) -> dict[str, Any]:
        """Full-text search (parsed from the server-rendered results page).

        Args:
            query: search text.
            page: 1-based results page.

        Returns:
            Dict with query, page, total_results, total_pages and a ``results``
            list of ``{title, slug, url, snippet}`` dicts.
        """
        resp = self._request(
            "/search",
            {"q": query, "page": str(page)},
            accept="text/html,application/xhtml+xml",
        )
        return _parse_search_html(resp.text, query, page)

    def search_all(
        self, query: str, max_pages: int = 1, delay: float = 0.5
    ) -> list[dict[str, Any]]:
        """Convenience helper: collect results across several pages."""
        import time

        collected: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            data = self.search(query, page=page)
            if not data["results"]:
                break
            collected.extend(data["results"])
            if data["total_pages"] and page >= data["total_pages"]:
                break
            if page < max_pages:
                time.sleep(delay)
        return collected

    # -- page content ------------------------------------------------------ #
    def get_page(
        self,
        slug: str,
        include_content: bool = True,
        follow_redirect: bool = True,
        _max_redirects: int = 3,
    ) -> dict[str, Any]:
        """Fetch a Grokipedia page.

        Args:
            slug: page slug (e.g. ``"Albert_Einstein"``), a full page URL, or a
                title-ish string.
            include_content: when True fetches the full Markdown article body
                (``content=1``); when False fetches metadata/citations only.
            follow_redirect: follow ``metadata.redirectTarget`` automatically.

        Returns:
            ``{"found": True, "slug", "title", "description", "content",
            "metadata", "stats", "citations", "images"}`` or
            ``{"found": False, "slug": ...}`` when the page does not exist.

        The ``content`` field is GitHub-flavoured Markdown (with infobox HTML
        comments and ``[text](/page/Slug)`` links).
        """
        norm = _normalize_slug(slug)
        resp = self._request(
            "/api/page-preview",
            {"slug": norm, "content": "1" if include_content else "0"},
        )
        data = resp.json()

        if not isinstance(data, dict) or not data.get("found"):
            return {"found": False, "slug": norm}

        page = data.get("page") or {}
        metadata = page.get("metadata") or {}

        if (
            follow_redirect
            and _max_redirects > 0
            and metadata.get("isRedirect")
            and metadata.get("redirectTarget")
            and metadata["redirectTarget"] != norm
        ):
            return self.get_page(
                metadata["redirectTarget"],
                include_content=include_content,
                follow_redirect=True,
                _max_redirects=_max_redirects - 1,
            )

        result: dict[str, Any] = {
            "found": True,
            "slug": page.get("slug", norm),
            "title": page.get("title", ""),
            "description": page.get("description", ""),
            "metadata": metadata,
            "stats": page.get("stats") or {},
            "citations": page.get("citations") or [],
            "images": page.get("images") or [],
            "url": f"{BASE_URL}/page/{page.get('slug', norm)}",
        }
        if include_content:
            result["content"] = page.get("content", "")
        return result

    def get_page_content(self, slug: str) -> str:
        """Return just the Markdown body of a page (``""`` if not found)."""
        page = self.get_page(slug, include_content=True)
        return page.get("content", "") if page.get("found") else ""

    def get_page_markdown(self, slug: str) -> str:
        """Alias for :meth:`get_page_content`."""
        return self.get_page_content(slug)

    def get_page_summary(self, slug: str) -> dict[str, Any]:
        """Fetch metadata, description, stats, citations and images only."""
        return self.get_page(slug, include_content=False)

    def page_exists(self, slug: str) -> bool:
        """Return True if the page exists."""
        return bool(self.get_page(slug, include_content=False).get("found"))

    # -- misc -------------------------------------------------------------- #
    def get_stats(self) -> dict[str, Any]:
        """Site-wide statistics shown on the homepage API."""
        return self._request("/api/stats").json()

    def close(self) -> None:
        try:
            self._rs.close()
        except Exception:
            pass

    def __enter__(self) -> "GrokipediaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Grokipedia API client")
    sub = parser.add_subparsers(dest="cmd")

    p_search = sub.add_parser("search", help="full-text search (parsed HTML)")
    p_search.add_argument("query")
    p_search.add_argument("--page", type=int, default=1)
    p_search.add_argument("--all-pages", type=int, default=0,
                          help="collect up to N pages instead of a single page")

    p_suggest = sub.add_parser("suggest", help="typeahead suggestions")
    p_suggest.add_argument("query")
    p_suggest.add_argument("--limit", type=int, default=10)

    p_page = sub.add_parser("page", help="fetch page content")
    p_page.add_argument("slug")
    grp = p_page.add_mutually_exclusive_group()
    grp.add_argument("--content", dest="content", action="store_true", default=True)
    grp.add_argument("--no-content", dest="content", action="store_false")

    sub.add_parser("stats", help="site statistics")

    args = parser.parse_args(argv)
    client = GrokipediaClient()

    if args.cmd == "search":
        if args.all_pages:
            _print_json(client.search_all(args.query, max_pages=args.all_pages))
        else:
            _print_json(client.search(args.query, page=args.page))
    elif args.cmd == "suggest":
        _print_json(client.suggest(args.query, limit=args.limit))
    elif args.cmd == "page":
        page = client.get_page(args.slug, include_content=args.content)
        if not page.get("found"):
            _print_json(page)
            return 1
        if not args.content:
            page.pop("content", None)
        _print_json(page)
    elif args.cmd == "stats":
        _print_json(client.get_stats())
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GrokipediaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
