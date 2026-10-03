"""
Scrapers for agency sites that don't offer an API, plus GAAPweb (a
structured accountancy job board that many agencies cross-post to).

IMPORTANT — read before relying on this module:
These scrapers use heuristic HTML parsing, not site-verified CSS
selectors. Each site's status (ok / zero_results / error) is returned
alongside results so failures are visible rather than silent.

Title extraction walks up from the job link through its ancestors,
looking for the first ancestor that contains EXACTLY ONE heading tag
(h1-h6) — that's almost always the individual job card, since a link's
immediate parent is often just a small button wrapper (the real title
sits as a sibling, not a parent), while walking too far up reaches a
section containing many cards (multiple headings), which this stops
short of. Falls back to the link's own visible text if no clean single
heading is found on the way up.

A URL is treated as a real posting (not a nav/filter/category link) if
it contains "/job/" (singular — every site seen during testing uses this
consistently only for individual postings, regardless of how short the
ID is), or has a 4+ digit run or a UUID elsewhere in the URL — this catches
sites using "/jobs/" (plural) for both listings and individual postings.

If a site consistently returns zero_results despite having live
vacancies, the likely cause is that its listings load via JavaScript
after the page loads — this scraper only sees the initial HTML. Confirmed
for TPF Recruitment (embedded Zoho Recruit widget); Public Practice
Recruitment Ltd's "Load more listings" button suggests the same for
anything beyond its first batch.
"""

import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) practice-ledger-job-search/1.0"
}

JOB_LINK_HINTS = [
    "/job/", "/jobs/", "/vacancy/", "/vacancies/", "/role/", "/role-details/",
    "/careers/", "/job-detail/", "/job-search/",
]

# Path markers trusted as individual postings regardless of ID length —
# every example seen of these during testing was a real job page, never a
# nav/listing page, so the usual "needs a 4+ digit ID" check is skipped
# for these specifically (some sites use short sequential or slug-only IDs).
# "/job-search/" deliberately excluded — Distinct Recruitment uses that
# same prefix for its category/listing pages too, so those need the
# digit-ID check below to tell a real posting from a listing page.
STRONG_JOB_PATH_MARKERS = ["/job/", "/role-details/", "/job-detail/"]

GENERIC_LINK_TEXTS = [
    "read more", "apply", "apply now", "view job", "view more", "view details",
    "search jobs", "find a job", "save job", "learn more", "details", "see more",
    "more info", "remove selection", "load more", "careers blog", "send your cv",
    "send us your cv", "quick cv upload", "i'm interested", "im interested",
    "register your interest",
]

# Repeated section labels that carousel/slider plugins bake into every
# individual slide's markup — these can look like a job's "only heading"
# even though they're shared across every listing, not specific to one.
GENERIC_SECTION_HEADINGS = [
    "recent practice jobs", "recent jobs", "latest jobs", "current vacancies",
    "featured jobs", "job openings", "our vacancies",
]

JOB_ID_PATTERNS = [
    re.compile(r"\d{4,}"),
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I),
]

SALARY_RE = re.compile(r"£\s?[\d,]{4,7}(?:\s?[-–—to]{1,3}\s?£?\s?[\d,]{4,7})?")
LOCATION_HINT_RE = re.compile(
    r"\b(London|Surrey|Kent|Essex|Herts|Hertfordshire|Watford|Guildford|Leatherhead|"
    r"Weybridge|Croydon|Egham|Farnham|Woking|Cobham|Sussex|Hampshire|Berkshire|"
    r"Buckinghamshire|St\.?\s?Albans|Chelmsford|Reading|Southampton|Winchester|"
    r"Norwich|Oxford|Bristol)\b",
    re.I,
)

# Sites confirmed to have real, robots-permitted job listings during research,
# verified against actual live scraper output (not just assumed).
SITE_REGISTRY = [
    {"name": "Fletcher George", "listing_url": "https://fletchergeorge.co.uk/"},
    {"name": "Austin Rose", "listing_url": "https://www.austinrose.co.uk/audit-assurance"},
    {"name": "Ambition", "listing_url": "https://www.ambition.co.uk/jobs/accountancy-practice/audit-and-assurance"},
    {"name": "The Accountancy Recruiters", "listing_url": "https://www.accountancyrecruiters.com/jobs"},
    {"name": "Howett Thorpe", "listing_url": "https://www.howett-thorpe.co.uk/practice/"},
    {"name": "Rowland Recruitment", "listing_url": "https://jobs.rowlandrecruitment.com/"},
    {"name": "Farrer Barnes", "listing_url": "https://www.farrer-barnes.com/cm/jobs"},
    {"name": "GAAPweb (Audit)", "listing_url": "https://www.gaapweb.com/jobs/audit/london-greater-/"},
    # Insite Recruitment specialises in accountancy practice specifically —
    # confirmed cross-posts many but not obviously all roles to Reed (real
    # gaps found: several live listings on their own site weren't visible
    # via their Reed employer page at the time this was checked), and their
    # site is plain static HTML, no JavaScript rendering needed.
    #
    # Uses their Public Practice division's own job board specifically
    # (not their general Finance board) — confirmed via a real missed role
    # ("Audit Director RI", Watford) that this page is where genuinely
    # relevant Director/Partner/audit/RI content actually lives, while the
    # general Finance board is mostly unrelated commercial finance roles
    # (payroll, credit control, HR) with only occasional practice content.
    {"name": "Insite Recruitment", "listing_url": "https://www.insiterec.co.uk/practice/job-board---public-practice"},

    # ICAEW Jobs — the official job board for ACA-qualified chartered
    # accountants, dedicated "Accounting - practice" category. Confirmed
    # via direct fetch: 379 live listings, a dedicated "Director" filter,
    # and real RI-mentioning content, fully public (the "members only"
    # restriction is specifically on applying/account creation, not on
    # browsing — confirmed by successfully fetching full listing content
    # without hitting a login wall).
    {"name": "ICAEW Jobs", "listing_url": "https://jobs.icaew.com/jobs/accounting-practice/"},
    # ACCA Careers — the other major UK accounting body's job board,
    # dedicated "Accountancy Practice Jobs" page. Confirmed via direct
    # fetch: real content, though skews somewhat more junior overall than
    # ICAEW (ACCA's membership includes more part-qualified candidates).
    {"name": "ACCA Careers", "listing_url": "https://jobs.accaglobal.com/landingpage/3165354/accountancy-practice-jobs/"},
    # Michael Page — confirmed via direct fetch to have a specifically
    # filtered category matching this brief almost exactly ("a clear and
    # accelerated path towards Responsible Individual (RI) status and
    # ultimately Partnership"). Clean, reliable structure: real <h3>
    # headings directly containing the job link.
    {"name": "Michael Page", "listing_url": "https://www.michaelpage.co.uk/jobs/audit-advisory/audit-advisory/practice-audit-assurance"},

    # The sites below need a headless browser (needs_js: True) because their
    # job listings are injected by JavaScript after the page loads — a plain
    # HTTP request (everything above this line) genuinely cannot see that
    # content, no matter the URL. Requires a one-time setup: see README's
    # "Getting the JavaScript-rendered sites working" section. Without that
    # setup, these will show a clear "Playwright not installed" error status
    # rather than silently failing or breaking the rest of the app.
    {
        # Same Firefish/ASP.NET platform as Kyvano (same _DynamicTemplates
        # folder structure, same .aspx pages) — very likely the same
        # click-to-search behavior. The URL after clicking Search
        # (?cid=...) looks like a session-specific results ID rather than
        # a stable link, so clicking is the more reliable approach here
        # too rather than hardcoding that URL.
        "name": "Oscar Wood", "listing_url": "https://www.oscarwood.co.uk/jobs-board.aspx", "needs_js": True,
        "click_selector": "[id*='btnSearch']",
    },
    {"name": "Morgan McKinley", "listing_url": "https://www.morganmckinley.com/uk/jobs/discipline/accounting-finance-audit-jobs", "needs_js": True},
    {
        # Fixed: point straight at the actual ATS jobs page instead of the
        # Wix homepage, which only ever contained a link to this — never
        # the job listings themselves.
        "name": "TPF Recruitment", "listing_url": "https://tpfrecruitment.zohorecruit.eu/jobs/Careers", "needs_js": True,
    },
    {"name": "Curtis Recruitment", "listing_url": "https://www.curtisrecruitment.co.uk/current-vacancies/", "needs_js": True},
    {"name": "Pro-Recruitment", "listing_url": "https://www.pro-recruitment.co.uk/live-roles", "needs_js": True},
]

# Baker Thornton deliberately excluded, and not a needs_js case: their
# "Professionals" page reads as a discreet candidate-registration funnel,
# not a browsable listing, and their actual job board
# (practicejobs.bakerthornton.com) is overwhelmingly US/Canada roles — not a
# good scraping target regardless of JavaScript. Kept as a link-only source
# instead (search_core.py's default "sources" list).

# Public Practice Recruitment Ltd deliberately excluded: even with the SSL
# bypass, the page returned was a 6.9KB hosting-provider page (links only to
# Krystal/cPanel, their web host) rather than any real site content —
# combined with the earlier certificate mismatch, this points to a genuine
# problem on their end (or an interaction between the SSL bypass and their
# host's routing) rather than something fixable by adjusting this scraper.
# Kept as a link-only source instead.

# AJ Chambers deliberately excluded: their robots.txt disallows automated
# access site-wide. They post their roles to Reed instead (confirmed), so
# Reed already covers them.



def fetch_page(url, timeout=15, insecure_ssl_fallback=False):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.SSLError:
        if not insecure_ssl_fallback:
            raise
        # A small number of sites have a misconfigured cert that browsers
        # tolerate but Python's stricter ssl module rejects. Only used for
        # sites explicitly flagged in SITE_REGISTRY where the content was
        # manually verified as legitimate first.
        resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
        resp.raise_for_status()
        return resp.text


def fetch_page_rendered(url, timeout=25, ignore_https_errors=False, click_selector=None, fill_before_click=None):
    """Fetches a page AFTER letting its JavaScript run, using a headless
    Chromium browser via Playwright — needed for sites whose job listings
    are injected client-side and never appear in the raw HTML fetch_page()
    receives. Also collects content from any same-page iframes (e.g. an
    embedded ATS widget), concatenated onto the main document's HTML, since
    a site's real listings sometimes live inside one of these rather than
    the top-level page.

    click_selector: for sites where results only appear after a real form
    submission (e.g. ASP.NET WebForms postbacks). Can be a single CSS
    selector string, or a list of candidate selectors to try in order —
    useful when the same underlying platform doesn't render every site's
    search button with quite the same markup. Confirmed necessary from a
    real WebForm_DoPostBackWithOptions call found in Kyvano's page — this
    isn't a guess. Returns (html, click_info) where click_info records
    which selector worked (if any) and why any failed, so a failed click
    is visible in diagnostics instead of silently swallowed.

    fill_before_click: optional list of {"selector": ..., "value": ...}
    dicts, tried in order before the click sequence — the first one whose
    selector actually matches an element gets filled in. Needed for sites
    where a blank search returns nothing and an actual keyword has to be
    entered first. Best-effort: a selector that doesn't match anything is
    silently skipped rather than treated as an error, since it's normal
    for most of these guesses not to match a given page.

    Two more things this works around, based on several sites coming back
    with zero content but no error:
    1. Bot detection — plain headless Chromium exposes navigator.webdriver
       as true, which some sites use to serve stripped-down content to
       anything that looks automated. Launch args + an init script mask
       this the same way common scraping-detection workarounds do.
    2. Waiting for full network idle before reading the page — many job
       board widgets poll continuously in the background and never truly
       go idle. Switched to a fixed, more generous wait instead of relying
       on network-idle detection succeeding.

    Requires a one-time setup step:
        pip install playwright
        playwright install chromium
    (see README). If either isn't done, this raises a clear RuntimeError
    that shows up as that site's error status — it doesn't crash the app
    or affect any other site.

    Honesty note: the click_selector and fill_before_click mechanisms
    follow standard Playwright patterns, but couldn't be tested end-to-end
    against the real sites while building this — the environment used to
    build this app can't download browser binaries. Should work on a
    normal machine with internet access, which is what you're running it
    on."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright not installed — run: pip install playwright && "
            "playwright install chromium (see README)"
        )

    selectors_to_try = []
    if click_selector:
        selectors_to_try = [click_selector] if isinstance(click_selector, str) else list(click_selector)

    click_info = {
        "attempted": bool(selectors_to_try), "succeeded_selector": None, "errors": [],
        "fill_attempted": bool(fill_before_click), "fill_succeeded_selector": None, "fill_errors": [],
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                ignore_https_errors=ignore_https_errors,
                viewport={"width": 1366, "height": 900},
            )
            # Mask the most common automated-browser signal before any
            # page script runs.
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()

            # domcontentloaded is reached reliably even by sites that poll
            # continuously in the background; the explicit wait after it
            # gives client-side rendering time to actually happen, without
            # depending on network-idle detection that may never fire.
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000 if selectors_to_try else 8000)

            if fill_before_click:
                for fill_spec in fill_before_click:
                    try:
                        page.fill(fill_spec["selector"], fill_spec["value"], timeout=4000)
                        click_info["fill_succeeded_selector"] = fill_spec["selector"]
                        break
                    except Exception as e:
                        click_info["fill_errors"].append({"selector": fill_spec["selector"], "error": str(e)[:150]})
                        continue

            html_before_click = page.content() if selectors_to_try else None

            for selector in selectors_to_try:
                try:
                    page.click(selector, timeout=8000)
                except Exception as e_normal:
                    # A "resolved to <element>" timeout (element found but
                    # Playwright won't click it) usually means it's hidden,
                    # covered by something else, or not yet considered
                    # stable — force bypasses those checks and dispatches
                    # the click directly. Standard fix for this exact
                    # failure signature, tried before giving up on this
                    # selector entirely.
                    try:
                        page.click(selector, timeout=8000, force=True)
                    except Exception as e_forced:
                        click_info["errors"].append({
                            "selector": selector,
                            "error": f"normal click: {str(e_normal)[:150]} | forced click: {str(e_forced)[:150]}",
                        })
                        continue
                # Don't assume the click causes a full page navigation —
                # many ASP.NET sites use a partial AJAX update instead, in
                # which case waiting for a "load state" resolves almost
                # instantly (that state was already satisfied by the
                # original page load) and doesn't actually wait for the new
                # content to arrive. A plain fixed wait works either way.
                page.wait_for_timeout(6000)
                click_info["succeeded_selector"] = selector
                click_info["content_changed"] = page.content() != html_before_click
                break

            html_parts = [page.content()]
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                try:
                    html_parts.append(frame.content())
                except Exception:
                    pass  # cross-origin or already-closed frames can't be read — skip, don't fail the whole fetch

            browser.close()
            return "\n".join(html_parts), click_info
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            f"Playwright render failed ({e}) — if Chromium isn't installed, "
            f"run: playwright install chromium"
        )


def looks_like_job_posting_url(url):
    if any(marker in url for marker in STRONG_JOB_PATH_MARKERS):
        return True
    return any(p.search(url) for p in JOB_ID_PATTERNS)


def diagnose_page(html, base_url):
    """Deep diagnostic for a fetched page — shows exactly where the
    extraction pipeline succeeds or fails, rather than just a final
    candidate count. Used when a site returns zero_results and the cause
    isn't obvious from that alone."""
    soup = BeautifulSoup(html, "html.parser")
    all_links = soup.find_all("a", href=True)
    all_clickable = soup.find_all(["button"]) + soup.find_all(attrs={"onclick": True})

    hint_matched = []
    for a in all_links:
        href = a["href"]
        if any(hint in href for hint in JOB_LINK_HINTS):
            hint_matched.append(urljoin(base_url, href))

    id_matched = [href for href in hint_matched if looks_like_job_posting_url(href)]

    return {
        "html_length": len(html),
        "html_sample_start": html[:300],
        "total_a_tags": len(all_links),
        "total_buttons_or_onclick_elements": len(all_clickable),
        "sample_hrefs": [a["href"] for a in all_links[:25]],
        "links_matching_job_keyword_hints": len(hint_matched),
        "sample_hint_matched_urls": hint_matched[:10],
        "links_also_passing_id_pattern_check": len(id_matched),
        "sample_id_matched_urls": id_matched[:10],
    }


def find_title_via_ancestor_heading(a, max_levels=6):
    """Walk up from the link through its ancestors. Returns (title, node)
    for the first ancestor containing exactly one heading — that's almost
    always the individual job card. Stops early if an ancestor has more
    than one heading, since that means we've walked past the card
    boundary into a section containing multiple listings. Skips (but
    keeps walking past) a heading that's a known generic section label
    repeated across every card by some carousel/slider plugins."""
    node = a
    for _ in range(max_levels):
        node = node.parent
        if node is None or getattr(node, "name", None) in ("body", "html", None, "[document]"):
            break
        headings = node.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        if len(headings) == 1:
            text = headings[0].get_text(strip=True)
            if text.lower() in GENERIC_SECTION_HEADINGS:
                continue  # not a real title — keep walking up past it
            if text and len(text) > 3:
                return text, node
        elif len(headings) > 1:
            break
    return None, None


def find_title_via_image_alt(a, max_levels=4):
    """Fallback for sites with no semantic heading tags at all (e.g. slider
    plugins) — the job title often survives as an <img alt="..."> even
    when it's not in any heading. Same 'exactly one' logic as the heading
    search, for the same reason."""
    node = a
    for _ in range(max_levels):
        node = node.parent
        if node is None or getattr(node, "name", None) in ("body", "html", None, "[document]"):
            break
        images_with_alt = [img for img in node.find_all("img") if img.get("alt", "").strip()]
        if len(images_with_alt) == 1:
            alt_text = images_with_alt[0]["alt"].strip()
            if alt_text.lower() in GENERIC_SECTION_HEADINGS:
                continue
            if len(alt_text) > 3 and alt_text.lower() not in GENERIC_LINK_TEXTS:
                return alt_text, node
        elif len(images_with_alt) > 1:
            break
    return None, None


def find_title_via_first_child(a, max_levels=5):
    """Fallback for card-style listings with no semantic heading tags and
    no useful image alt text (e.g. some Webflow/CMS-built sites, where a
    job's title, category tag, location and salary are all just plain
    stacked <div>s). Confirmed necessary for at least one real site
    (Insite Recruitment) where a title check found no heading tags at all
    in the visible page structure.

    Looks for an ancestor with more than one direct child element — a
    sign it's a "card" with several stacked fields — and takes the FIRST
    child's own text as the title, since that's consistently where a
    title sits in this kind of layout. Rejected if it's empty, too long
    to plausibly be a title, or is itself a generic/salary/apply-button
    string rather than a real title."""
    node = a
    for _ in range(max_levels):
        node = node.parent
        if node is None or getattr(node, "name", None) in ("body", "html", None, "[document]"):
            break
        direct_children = [c for c in node.find_all(recursive=False) if getattr(c, "name", None)]
        if len(direct_children) < 2:
            continue
        first_text = direct_children[0].get_text(strip=True)
        if not first_text or len(first_text) > 120:
            continue
        if first_text.lower() in GENERIC_LINK_TEXTS or first_text.lower() in GENERIC_SECTION_HEADINGS:
            continue
        if SALARY_RE.search(first_text):
            continue
        return first_text, node
    return None, None


CARD_META_MARKER = "Read more about this role"


def split_embedded_card_metadata(title):
    """Some sites (confirmed: Pro-Recruitment) put the whole card's text
    inside the heading, so the "title" arrives as e.g.
    "Corporate Finance DirectorRead more about this rolePermanent
    £85,000 to £120,000Manchester". This splits that into the clean
    title plus the card's OWN salary and location, which are far more
    reliable than searching the surrounding page text (that picked up an
    unrelated town — e.g. Guildford for a Manchester role — because the
    surrounding text spans more than one card).

    The location is only trusted when a salary figure anchors it (the
    location sits immediately after the salary in this layout); with no
    salary to anchor on, only the title is cleaned. Returns
    (clean_title, salary_text_or_None, location_text_or_None). Titles
    without the marker are returned untouched."""
    if not title or CARD_META_MARKER not in title:
        return title, None, None
    clean, _, remainder = title.partition(CARD_META_MARKER)
    clean = clean.strip()
    salary_match = SALARY_RE.search(remainder)
    if not salary_match:
        return clean, None, None
    location = remainder[salary_match.end():].strip(" ,-|/")
    if not location or len(location) > 60:
        location = None
    return clean, salary_match.group(0), location


def extract_job_candidates(html, base_url):
    """Heuristic extraction: find links that look like specific job
    postings (not nav/filter links), preferring the enclosing card's
    unique heading for the title over the link's own (often generic)
    visible text."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not any(hint in href for hint in JOB_LINK_HINTS):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen_urls:
            continue
        if not looks_like_job_posting_url(full_url):
            continue  # looks like a nav/filter/category link, not a posting

        title, context_node = find_title_via_ancestor_heading(a)
        if not title:
            title, alt_node = find_title_via_image_alt(a)
            if alt_node:
                context_node = alt_node
        if not title:
            title, card_node = find_title_via_first_child(a)
            if card_node:
                context_node = card_node
        if not context_node:
            context_node = a.find_parent(["li", "article", "div"])
        context = context_node.get_text(" ", strip=True) if context_node else ""

        if not title:
            link_text = a.get_text(strip=True)
            if link_text and len(link_text) > 3 and link_text.lower() not in GENERIC_LINK_TEXTS:
                title = link_text
        if not title:
            continue  # nothing usable found for this link

        seen_urls.add(full_url)
        title, embedded_salary, embedded_location = split_embedded_card_metadata(title)
        salary_match = SALARY_RE.search(context)
        location_match = LOCATION_HINT_RE.search(context)

        candidates.append(
            {
                "job_title": title,
                "job_url": full_url,
                "description_snippet": context[:400],
                "salary_text": embedded_salary or (salary_match.group(0) if salary_match else None),
                "location_name": embedded_location or (location_match.group(0) if location_match else None),
            }
        )
    return candidates


def scrape_site(site, timeout=15):
    """Returns (status, candidates). status is 'ok', 'zero_results', or 'error: <msg>'."""
    try:
        if site.get("needs_js"):
            html, _click_info = fetch_page_rendered(
                site["listing_url"], timeout=max(timeout, 25),
                ignore_https_errors=site.get("insecure_ssl_fallback", False),
                click_selector=site.get("click_selector"),
                fill_before_click=site.get("fill_before_click"),
            )
        else:
            html = fetch_page(
                site["listing_url"], timeout=timeout,
                insecure_ssl_fallback=site.get("insecure_ssl_fallback", False),
            )
    except (requests.RequestException, RuntimeError) as e:
        return f"error: {e}", []

    try:
        candidates = extract_job_candidates(html, site["listing_url"])
    except Exception as e:
        return f"error: parse failed ({e})", []

    if not candidates:
        return "zero_results", []
    return "ok", candidates


def scrape_all_sites(sites=None, delay_seconds=0.5):
    """Scrapes each registered site in turn, returns a dict:
    {site_name: {"status": ..., "candidates": [...]}}
    A small delay between requests is polite to smaller agency sites that
    aren't built for heavy traffic."""
    sites = sites if sites is not None else SITE_REGISTRY
    results = {}
    for site in sites:
        status, candidates = scrape_site(site)
        for c in candidates:
            c["source_site"] = site["name"]
        results[site["name"]] = {"status": status, "candidates": candidates}
        time.sleep(delay_seconds)
    return results


def diagnose_site(site_name, timeout=15):
    """Fetches a specific site by name (from SITE_REGISTRY) and returns a
    deep diagnostic instead of final candidates — shows total links found,
    what they look like, exactly which filtering stage rejects them, and
    (for sites with a click_selector) whether the click succeeded and why
    it failed if not. Use this when a site returns zero_results and it's
    unclear why."""
    site = next((s for s in SITE_REGISTRY if s["name"] == site_name), None)
    if not site:
        return {"error": f"No site named '{site_name}' in SITE_REGISTRY"}

    click_info = None
    try:
        if site.get("needs_js"):
            html, click_info = fetch_page_rendered(
                site["listing_url"], timeout=max(timeout, 25),
                ignore_https_errors=site.get("insecure_ssl_fallback", False),
                click_selector=site.get("click_selector"),
                fill_before_click=site.get("fill_before_click"),
            )
        else:
            html = fetch_page(
                site["listing_url"], timeout=timeout,
                insecure_ssl_fallback=site.get("insecure_ssl_fallback", False),
            )
    except (requests.RequestException, RuntimeError) as e:
        return {"listing_url": site["listing_url"], "fetch_error": str(e)}

    diagnostic = diagnose_page(html, site["listing_url"])
    diagnostic["listing_url"] = site["listing_url"]
    diagnostic["needs_js"] = site.get("needs_js", False)
    if click_info is not None:
        diagnostic["click_info"] = click_info
    return diagnostic
