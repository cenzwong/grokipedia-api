# Grokipedia API Client

Unofficial, zero-configuration Python client for [grokipedia.com](https://grokipedia.com)
covering **search** and **page content retrieval**.

It reproduces the endpoints the Grokipedia website itself calls. No API key, login,
cookie setup, environment variables, or config files are needed — run it and go.

## Files

- `api_client.py` — the client + a small CLI.
- `README.md` — this document.

## Requirements

```bash
pip install requests        # required
pip install httpcloak       # optional, recommended (browser TLS/HTTP2 fingerprint)
```

`httpcloak` is used automatically when installed; otherwise the client falls back to
plain `requests`. Both work against these public endpoints.

## Authentication

**None required.** Grokipedia's read endpoints are public.

- No login, token, or API key flow exists in the traffic.
- The browser sends only `grokipedia-affinity` (routing) and `__cf_bm` (Cloudflare,
  ~30 min lifetime) cookies. Neither is required; the stable affinity cookie is
  hardcoded for fingerprint parity, and `__cf_bm` is intentionally omitted because
  it expires quickly.
- There is no token/cookie refresh flow to implement. If a stale cookie ever causes a
  `401`/`403`, the client automatically drops cookies and retries, and it falls back
  between transports (`httpcloak` → `requests`).

Responses are served through Cloudflare with a ~5-minute public cache.

## Discovered APIs

| Purpose | Method & Endpoint | Format |
|---|---|---|
| Search suggestions | `GET /api/typeahead?v=2&query=<q>&limit=<n>` | JSON |
| Full-text search | `GET /search?q=<q>&page=<n>` | HTML (parsed) |
| Page content | `GET /api/page-preview?slug=<slug>&content=1` | JSON |
| Page metadata only | `GET /api/page-preview?slug=<slug>&content=0` | JSON |
| Site statistics | `GET /api/stats` | JSON |

Notes:
- There is **no public search JSON endpoint**. `/api/search` returns 404; the search
  results page is server-rendered HTML. The client parses the result rows
  (`<a data-search-result-link="true">` → title `<h3>` + snippet `<p>`).
- `typeahead` returns all matches up to the server cap (~64 for a broad query); a
  large `limit` is honored up to that cap.
- Page content is returned as **GitHub-flavored Markdown**, including an infobox as an
  HTML comment block and internal links in `[text](/page/Slug)` form.
- `content=0` omits the `content` field but still includes metadata, stats, citations,
  and images (useful and much smaller than the full article).
- Missing pages return `{"found": false}` with HTTP 200.

## Python usage

```python
from api_client import GrokipediaClient

client = GrokipediaClient()   # or: with GrokipediaClient() as client: ...

# 1) Full-text search (server-rendered results, parsed to dicts)
hits = client.search("Albert Einstein", page=1)
print(hits["total_results"], hits["total_pages"])   # 7341 612
for r in hits["results"]:
    print(r["title"], "->", r["url"], "\n   ", r["snippet"][:80])

# 2) Typeahead / autocomplete
sugg = client.suggest("quantum computing", limit=5)
for r in sugg["results"]:
    print(r["slug"], r["title"])

# 3) Page content (Markdown) + metadata, citations, images
page = client.get_page("Albert_Einstein")
print(page["title"])
print(page["content"][:500])          # Markdown body
print(page["metadata"]["lastModified"])
print(page["citations"][0]["url"])
print(page["images"][0]["url"])

# Convenience helpers
md = client.get_page_markdown("Albert_Einstein")        # just the Markdown
summary = client.get_page_summary("Albert_Einstein")    # no content field
ok = client.page_exists("Albert_Einstein")              # -> True

# Accepts full URLs too
client.get_page("https://grokipedia.com/page/Albert_Einstein")

# Collect several search pages
all_hits = client.search_all("Albert Einstein", max_pages=3)
```

### Return shapes

`client.search(query, page)`:

```python
{
  "query": "Albert Einstein",
  "page": 1,
  "total_results": 7341,
  "total_pages": 612,
  "results": [
    {"title": "Albert Einstein", "slug": "Albert_Einstein",
     "url": "https://grokipedia.com/page/Albert_Einstein", "snippet": "..."},
    ...
  ],
}
```

`client.get_page(slug)`:

```python
{
  "found": True,
  "slug": "Albert_Einstein",
  "title": "Albert Einstein",
  "description": "...",
  "content": "# Albert Einstein\n<!--Infobox ... -->\n...",   # Markdown
  "metadata": {"lastModified": 1792278278, "contentLength": 111292,
               "version": "1.0", "categories": ["Einstein"], "language": "en",
               "isRedirect": False, "redirectTarget": "", ...},
  "stats": {"totalViews": "7868064", "qualityScore": 1, ...},
  "citations": [{"id": "227", "title": "...", "url": "https://...", ...}, ...],
  "images": [{"id": "...", "caption": "...", "url": "https://assets.grokipedia.com/...",
              "width": 1280, "height": 1280, "position": 2}, ...],
  "url": "https://grokipedia.com/page/Albert_Einstein",
}
```

`client.suggest(query, limit)`:

```python
{
  "query": "Albert Einstein",
  "count": 5,
  "search_time_ms": 33.0,
  "results": [
    {"slug": "Albert_Einstein", "title": "Albert Einstein", "snippet": "...",
     "relevanceScore": 100000, "viewCount": "7868064", ...},
    ...
  ],
}
```

Missing page: `{"found": False, "slug": "<requested>"}`.

## CLI usage

```bash
python api_client.py suggest "Albert Einstein" --limit 5
python api_client.py search "Albert Einstein" --page 1
python api_client.py search "Albert Einstein" --all-pages 3   # collect 3 pages
python api_client.py page Albert_Einstein --content            # full Markdown
python api_client.py page Albert_Einstein --no-content         # metadata only
python api_client.py stats
```

All commands print JSON to stdout. The `page` command exits non-zero if the page is
not found.

## Caveats

- **Search is HTML-based.** There is no search JSON API; the client parses the same
  markup the website renders. If Grokipedia changes its search markup, update
  `_SearchParser` / the regexes at the top of the parser section.
- **Search page size is fixed** at ~12 results per page by the site; use pagination
  (`search(page=N)` or `search_all`).
- Results are Cloudflare-cached for ~5 minutes and the index changes over time, so
  counts and ordering may drift.
- The `typeahead` endpoint has a server-side result cap (~64) regardless of `limit`.
- No headless browser is needed. These endpoints are not behind a bot challenge; if
  that ever changes, `httpcloak` improves the odds automatically.
- Be polite: reuse one client/session, and add small delays when paging in bulk.
