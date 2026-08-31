---
name: web-fetching
description: Local Python tools for fetching and extracting web content — no API keys needed.
---

# Web Fetching Tools

Local Python tools for fetching and extracting web content — no external API keys required.

## Environment

Python venv: `~/.venvs/webtools/`

Activate with:
```bash
source ~/.venvs/webtools/bin/activate
```

Or prefix commands with:
```bash
~/.venvs/webtools/bin/python3
```

## Tools

### 1. `requests` + `BeautifulSoup4` — Simple fetch + parse

Best for: static pages, docs, API endpoints.

```python
import requests
from bs4 import BeautifulSoup

url = 'https://example.com'
r = requests.get(url, timeout=10)
soup = BeautifulSoup(r.text, 'html.parser')

# Extract specific elements
title = soup.title.string
headlines = [h.text for h in soup.find_all('h2')]
links = [a['href'] for a in soup.find_all('a', href=True)]
text = soup.get_text(separator='\n', strip=True)
```

### 2. `trafilatura` — Clean article extraction

Best for: blog posts, news articles, Substack — extracts main content, strips ads/nav.

```python
import trafilatura

url = 'https://example.com/article'
result = trafilatura.fetch_url(url)
if result:
    extracted = trafilatura.extract(result)
    print(extracted)
```

Note: Returns full HTML on non-article pages (marketing sites, docs). Use BeautifulSoup for those.

### 3. `playwright` — JavaScript-rendered pages

Best for: pages that require JS to render (SPAs, dashboards, lazy-loaded content).

```python
from playwright.sync_api import sync_playwright

url = 'https://example.com/'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(url, timeout=15000)
    title = page.title()
    content = page.content()
    # Or extract specific elements:
    # elem = page.query_selector('h1')
    # text = elem.inner_text() if elem else ''
    browser.close()
```

## Choosing a Tool

| Use case | Tool |
|---|---|
| Static page, docs | `requests + BeautifulSoup` |
| Blog/article/post | `trafilatura` |
| JS-rendered page | `playwright` |
| Quick test | `requests` first — escalate to playwright if content is empty/wrong |
| **`web_search` / `web_extract` offline, need to verify a URL exists** | **browser tools (see below)** |

### Browser-tool fallback (when `web_search` and `web_extract` are unavailable)

`web_search` and `web_extract` sometimes go offline (no API key configured, firecrawl not provisioned, etc.). The browser tool is the fallback — but **don't use a search engine**. As of mid-2026:

- **Google, DuckDuckGo**: CAPTCHA wall from headless/non-personal IPs. Page loads but returns "unusual traffic from your computer network."
- **Bing**: Loads, but results are unreliable for product/catalog queries. Also treats generic model numbers as brand names (e.g. `"RLC-833A"` returns R+L Carriers).
- **Direct vendor product pages** (`amazon.com/dp/ASIN`, `adafruit.com/product/NNN`, `pishop.us/...`): load cleanly.
- **Brave Search** (`search.brave.com`): the one search engine that actually works from this IP for **community/forum research**. Surfaces Reddit thread snippets and Discourse community posts that Google/DuckDuckGo cannot reach. Verified 2026-07-22 in a Reolink-camera-tuning research session.

**Pattern: skip search engines entirely. Navigate straight to the vendor's product/catalog page.**

```python
# browser_navigate to the exact product page URL
browser_navigate(url="https://www.amazon.com/dp/B0F1G6XT38")

# Extract spec-relevant text via innerText + regex
result = browser_console(expression="document.body.innerText.match(/(?:chipset|antenna|class|RP-SMA|range)/gi)")
```

**Amazon search results extraction** (when you need to compare listings):

```javascript
(() => {
  const items = document.querySelectorAll('[data-component-type="s-search-result"]');
  return Array.from(items).slice(0,12).map(el => {
    const t = el.querySelector('h2 a, h2 span');
    const a = el.querySelector('h2 a');
    const p = el.querySelector('.a-price .a-offscreen');
    return {
      title: t?.innerText?.trim()?.slice(0,90),
      price: p?.innerText,
      asin: el.getAttribute('data-asin'),
    };
  }).filter(x => x.title);
})()
```

Returns compact JSON array — agent can filter on chipset, class, antenna keywords.

**Caveats:**
- Returned JSON sometimes serializes as `{}` if the expression has bare destructuring or throws silently. Wrap the body in `(() => { ... })()` IIFE and return a plain object/array.
- Some Amazon detail pages require adding `/dp/ASIN` to URL to bypass seller redirects.
- StarTech.com and Adafruit product pages are JS-rendered but the `innerText` extraction works (their `<title>` is populated server-side).

### Research integrity: when search is down

If the user asks for something that requires finding real, current data and `web_search` is offline:

1. **Do NOT fabricate URLs, prices, ratings, or part numbers** — even if you're "pretty sure" a product exists. The research-integrity rule overrides delivery speed.
2. **Try browser-tool direct navigation** to known canonical vendor pages. If they load, you have a real URL to cite.
3. **For commodity items** (cables, SD cards, junction boxes), cite the search URL with the appropriate filter query rather than a specific product URL. Mark these as "unverified" in deliverables.
4. **If browser tool also fails** (site blocks all non-personal traffic), say so directly and offer to wait or take an alternative path. Never invent a "verified" link.

This pattern has been verified live (2026-07-21): researched a hardware BoM with zero `web_search` calls by going directly to `amazon.com/dp/ASIN`, `adafruit.com/product/NNN`, and `pishop.us/...` URLs.

### Community / forum research — Brave Search + Wayback CDX recipe

When the question is *"what do other people do about X"* (community advice, KB article recommendations, real-world tuning recipes), the working recipe as of 2026-07-22 is:

1. **Brave Search via `browser_navigate("https://search.brave.com/search?q=...")`** — only search engine that returns useful results from this IP for community content. Surfaces:
   - Reddit thread snippets (with the top-voted answer text)
   - Discourse forum topics (Reolink Community, ipcamtalk, etc.) — even though the live site is login-walled, Brave caches the title + content snippets
   - Vendor KB articles and blog posts
2. **Wayback CDX API** for archived pages when Brave shows a useful URL but the live site requires login:
   ```bash
   # Find all archived snapshots of a URL pattern:
   curl -sL "https://web.archive.org/cdx/search/cdx?url=DOMAIN/PATH&matchType=prefix&limit=-1&output=json"
   # Fetch a specific snapshot (use the timestamp from the CDX output):
   curl -sL "https://web.archive.org/web/TIMESTAMP/URL"
   ```
3. **Direct curl to vendor blogs with `User-Agent: Mozilla/5.0 (Mac...)`** bypasses some bot blocks. Combine with `html.parser` to extract article body:
   ```python
   from html.parser import HTMLParser
   class TextExtractor(HTMLParser):
       def __init__(self):
           super().__init__()
           self.text, self.skip = [], False
       def handle_starttag(self, tag, attrs):
           if tag in ('script', 'style', 'noscript'): self.skip = True
       def handle_endtag(self, tag):
           if tag in ('script', 'style', 'noscript'): self.skip = False
       def handle_data(self, data):
           if not self.skip:
               t = data.strip()
               if t and len(t) > 1: self.text.append(t)
   ext = TextExtractor()
   ext.feed(html)
   content = '\n'.join(ext.text)
   ```

**Reddit `.json` endpoint is Cloudflare-walled** from this IP — even with browser user-agent and via Wayback. Quotes must come from Brave snippets or from inside the Reddit thread URL itself (requires login in the browser tool). Acceptable for citation purposes as long as the quote is attributed and dated.

**Wayback CDX gotchas**: empty result `[]` means no archived snapshots exist for that URL pattern. Wayback's actual page render often redirects to its own donation page or a "we didn't archive this" 404 — test with a known-good URL first.

## Via `execute_code`

Import in scripts:
```python
import sys
sys.path.insert(0, '/Users/<user>/.venvs/webtools/lib/python3.9/site-packages')
```

Or use `terminal()` with `~/.venvs/webtools/bin/python3 -c "..."`

## Common Tasks

**Fetch a page and extract all text:**
```python
import requests
from bs4 import BeautifulSoup
r = requests.get(url, timeout=10)
soup = BeautifulSoup(r.text, 'html.parser')
print(soup.get_text(separator='\n', strip=True))
```

**Fetch article main content:**
```python
import trafilatura
downloaded = trafilatura.fetch_url(url)
article = trafilatura.extract(downloaded)
print(article)
```

**Fetch JS page after waiting for content:**
```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(url, timeout=15000)
    page.wait_for_selector('main', timeout=5000)  # wait for content
    print(page.inner_text('main'))
    browser.close()
```

## Troubleshooting

- **Empty result from trafilatura**: Page may not be an article — use `requests + BeautifulSoup` instead
- **LibreSSL SSL warning**: Harmless — requests works fine despite the warning
- **Playwright timeout**: Increase `timeout=30000` or check if the page requires authentication
- **lxml ImportError (`lxml.html.clean` not found)**: Run `pip install lxml_html_clean` — recent lxml versions split html.clean into a separate package
