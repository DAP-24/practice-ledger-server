"""
Shared search logic for the Practice Ledger server package (the
cloud/GitHub Actions version). This is a deliberately independent copy of
the same logic used by the desktop "Practice Ledger" app — kept separate
so edits to one never silently affect the other. See this package's own
README for what's different here (no web UI, credentials come from
environment variables rather than a Settings screen) and how to set it up.
"""

import html
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus

import requests

import scrapers
import travel_time

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "settings.json"
GEOCODE_CACHE_PATH = BASE_DIR / "geocode_cache.json"
SEEN_JOBS_PATH = BASE_DIR / "seen_jobs.json"
# Separate from SEEN_JOBS_PATH deliberately: seen_jobs.json is used only by
# the scheduled script, to decide what's new enough to send a push
# notification about. This one tracks what's been shown with a "NEW" badge
# in the app itself. Without this split, whichever runs first — a
# background scheduled check, or you opening the app — "uses up" the NEW
# status for the other, so something you got notified about on your phone
# would show as a plain, unhighlighted result if you then opened the app,
# even though you'd never actually looked at it there.
APP_VIEWED_JOBS_PATH = BASE_DIR / "app_viewed_jobs.json"

PROFILE_NAME_SAFE_RE = re.compile(r"[^a-z0-9]+")


def seen_jobs_path_for_profile(profile_name):
    """Each named scheduled profile (e.g. "Audit", "General Practice")
    gets its own seen-jobs tracking file, kept separate for the same
    reason app_viewed_jobs.json is separate from seen_jobs.json: a role
    that appears in BOTH the Audit search and the General Practice search
    should be flagged new independently in each — otherwise whichever
    profile happens to run first each day "uses up" the new status for
    the other one, and the whole point of running both was to make sure
    nothing gets missed by either. No profile name (the plain, original
    scheduled script with no argument) keeps using the original
    seen_jobs.json unchanged, for backward compatibility."""
    if not profile_name:
        return SEEN_JOBS_PATH
    safe = PROFILE_NAME_SAFE_RE.sub("_", profile_name.strip().lower()).strip("_")
    return BASE_DIR / f"seen_jobs__{safe}.json"


OVERRIDES_PATH = BASE_DIR / "overrides.json"
HISTORY_PATH = BASE_DIR / "search_history.json"
SUMMARIES_DIR = BASE_DIR / "summaries"

REED_SEARCH_URL = "https://www.reed.co.uk/api/1.0/search"
# {page} starts at 1; results_per_page capped at 50 by Adzuna's own API.
ADZUNA_SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/gb/search/{page}"
# UK-specific key required — a key from jooble.org (not uk.jooble.org)
# only returns US listings. POST-based, unlike Reed/Adzuna's GET.
JOOBLE_SEARCH_URL = "https://uk.jooble.org/api/{key}"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
POSTCODES_IO_OUTCODE_URL = "https://api.postcodes.io/outcodes/{outcode}"
POSTCODES_IO_POSTCODE_URL = "https://api.postcodes.io/postcodes/{postcode}"

# Matches a full UK postcode with or without the internal space (Reed's
# API returns them squished together, e.g. "S110FQ" instead of "S11 0FQ").
# The inward part (after the space) is always exactly one digit + two
# letters, which is what makes it possible to reliably re-insert the space.
UK_POSTCODE_RE = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})$", re.IGNORECASE)
NOMINATIM_HEADERS = {"User-Agent": "practice-ledger-job-search/1.0 (personal use)"}

STALE_THRESHOLD_DAYS = 21
DAY_RATE_SALARY_CEILING = 2000  # a "salary" this small, on a contract role, is almost certainly a day rate
RI_PROGRESSION_BOOST = 25  # deliberately dominant vs other fit-score components (typically 0-8), so a genuine RI-progression offer always sorts near the top under "Best fit"

# Partnership-timeline scoring tiers, per explicit request: a significant
# boost for a short stated route to partnership, a smaller boost for a
# vague "fast-track" claim with no actual number given (and flagged as
# such, since it's unverified), and a milder down-rate for anything
# explicitly stated as more than 2 years away.
PARTNERSHIP_FAST_BOOST = 25       # explicit route under 12 months
PARTNERSHIP_MEDIUM_BOOST = 15     # explicit route 12-18 months
PARTNERSHIP_VAGUE_BOOST = 8       # "fast-track"-style phrase, no explicit number stated
PARTNERSHIP_SLOW_PENALTY = -10    # explicit route over 2 years — milder than the boosts, per instruction

DEFAULT_SETTINGS = {
    "reed_api_key": "",
    "adzuna_app_id": "",
    "adzuna_app_key": "",
    "jooble_api_key": "",
    "home_postcode": "WD6",
    "title_must_include_any": ["director", "partner designate"],
    "must_mention_any": {
        "audit": ["audit"],
        "general_practice": [
            "general practice", "mixed tax and accounts", "accounts and tax compliance",
            "general accountancy practice", "mixed portfolio", "accounts and tax portfolio",
        ],
        "ri_support": [
            "registered individual", "responsible individual", "audit qualified",
            "audit qualification", "audit registration", "eligible for audit registration",
            "become an ri", "become a registered individual", "icaew audit", " ri ",
        ],
        # Narrower than ri_support above — specifically phrases where the
        # employer is OFFERING to support progression toward RI status,
        # not phrases where RI status is a pre-requisite the candidate
        # must already hold. Used only for the "Best fit" boost, never for
        # the "must mention RI" exclusion filter, since a role requiring
        # someone who's already RI-qualified is a different situation
        # from one offering to develop someone toward it.
        "ri_progression_offered": [
            "support to become an ri", "support to become a registered individual",
            "support towards ri", "support towards becoming ri",
            "route to ri", "route to becoming ri", "route to registered individual",
            "path to ri", "pathway to ri", "clear path to ri",
            "working towards ri", "working towards ri status",
            "working towards responsible individual", "working towards registered individual",
            "progression to ri", "progression towards ri", "progress towards ri",
            "develop towards ri", "developing towards ri", "development towards ri",
            "achieve ri status", "achieving ri status", "help achieve ri",
            "obtain ri status", "obtaining ri status",
            "gain ri status", "gaining ri status",
            "become ri qualified", "becoming ri qualified",
            "ri training", "ri development", "training towards ri",
            "supported to become ri", "supported in becoming ri", "supported to become an ri",
            "help you become ri", "help you become an ri", "help you achieve ri status",
            "assistance becoming ri", "assistance in becoming ri",
            "sponsorship towards ri", "study support towards ri",
            "audit registration support", "support with audit registration",
            "support with your audit registration", "route to audit registration",
            "not yet ri", "if not already ri", "ri status not essential",
        ],
        "equity_route": [
            "equity partner", "route to equity", "equity partnership", "equity stake",
            "profit share", "path to equity", "future equity",
        ],
    },
    # Vague "fast route to partnership" phrasing with no explicit number
    # stated — gets a smaller boost than an explicit short timeframe, and
    # is flagged as unverified rather than treated the same as a role that
    # actually commits to a number of months/years.
    "partnership_fast_track_phrases": [
        "fast-track to partnership", "fast track to partnership",
        "fast-tracked to partner", "fast track to partner",
        "accelerated route to partnership", "accelerated partnership",
        "accelerated path to partner", "rapid route to partnership",
        "swift route to partnership", "quick route to partnership",
        "short route to partnership", "clear and fast route to partnership",
        "expedited route to partnership",
    ],
    "remote_hybrid_keywords": ["fully remote", "remote", "hybrid", "work from home", "wfh"],
    "exclude_title_keywords": [
        "graduate", "trainee", "part qualified", "part-qualified", "apprentice",
        "school leaver", "assistant", "junior",
    ],
    "big4_top10_firms": [
        "pwc", "pricewaterhousecoopers", "price waterhouse coopers", "deloitte", "ey",
        "ernst & young", "ernst and young", "kpmg", "bdo", "grant thornton",
        "evelyn partners", "rsm", "azets", "mazars", "forvis mazars",
    ],
    "independent_signal_phrases": [
        "independent firm", "independent practice", "independent accountancy",
        "owner-managed", "boutique firm", "family-run", "independently owned", "independent, ",
    ],
    "large_firm_signal_phrases": [
        "top 20 firm", "top 30 firm", "top 50 firm", "national firm", "national practice",
        "large regional", "leading regional",
    ],
    "default_radius_miles": 20,
    "enabled_sources": {},
    "traveltime_app_id": "",
    "traveltime_api_key": "",
    "ntfy_topic": "",  # empty = push notifications disabled
    "notify_on_zero_results": True,  # also send a "checked, nothing new" push — useful as confirmation the scheduled run actually happened, not just silence
    "sources": [
        {"name": "LinkedIn", "url_template": "https://www.linkedin.com/jobs/search/?keywords={keywords}&location={location}"},
        {"name": "Indeed", "url_template": "https://www.indeed.co.uk/jobs?q={keywords}&l={location}"},
        {"name": "Glassdoor", "url_template": "https://www.glassdoor.co.uk/Job/jobs.htm?sc.keyword={keywords}&locKeyword={location}"},
        {
            "name": "AJ Chambers (manual — see note in README)",
            "url_template": "https://aj-chambers.com/job-search?q={keywords}",
        },
        {
            "name": "Baker Thornton (manual — candidate-registration model, not a job board)",
            "url_template": "https://bakerthornton.com/Professionals",
        },
        {
            "name": "Public Practice Recruitment Ltd (manual — site returned only a hosting-provider error page, likely a problem on their end)",
            "url_template": "https://publicpracticerecruitment.co.uk/",
        },
        {
            "name": "Kyvano (manual — search results couldn't be reached: keyword search, click, and postback all confirmed working individually, but the results still never render where expected)",
            "url_template": "https://www.kyvano.com/jobs/",
        },
    ],
    # The filters used the last time a search was run from the web UI —
    # scheduled_search.py reuses these so it searches the same thing you
    # last searched for, without needing its own separate configuration.
    "last_search_params": {},
    "search_profiles": {},  # {name: params_dict} — named, saved search configurations for the scheduled script, e.g. one for "Audit", one for "General Practice"
}


def all_known_source_names():
    return ["Reed"] + [site["name"] for site in scrapers.SITE_REGISTRY]


def load_settings():
    if SETTINGS_PATH.exists():
        stored = json.loads(SETTINGS_PATH.read_text())
        merged = {**DEFAULT_SETTINGS, **stored}
    else:
        merged = dict(DEFAULT_SETTINGS)
    enabled = dict(merged.get("enabled_sources") or {})
    for name in all_known_source_names():
        enabled.setdefault(name, True)
    merged["enabled_sources"] = enabled
    apply_env_credential_overrides(merged)
    return merged


# This package's own addition, not present in the local desktop app's copy
# of this file (kept deliberately separate — see the server package's own
# README). settings.json in this repo is committed to git and should
# never contain real credentials, since GitHub repos (even private ones)
# and their history are not the right place for secrets long-term. These
# five fields are instead injected at runtime from GitHub Actions
# "Secrets" — see the workflow file — and simply overwrite whatever
# (blank) placeholder value sits in the committed settings.json. Running
# this script anywhere the environment variables aren't set (e.g. by
# accident, locally) leaves settings.json's own values untouched, so nothing
# breaks — the fields just stay blank/default, same as a fresh install.
ENV_CREDENTIAL_MAP = {
    "REED_API_KEY": "reed_api_key",
    "ADZUNA_APP_ID": "adzuna_app_id",
    "ADZUNA_APP_KEY": "adzuna_app_key",
    "JOOBLE_API_KEY": "jooble_api_key",
    "NTFY_TOPIC": "ntfy_topic",
}


def apply_env_credential_overrides(settings):
    for env_name, settings_key in ENV_CREDENTIAL_MAP.items():
        value = os.environ.get(env_name)
        if value:
            settings[settings_key] = value
    return settings


def save_settings(settings):
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))


def _load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return default
    return default


def _save_json(path, data):
    path.write_text(json.dumps(data, indent=2))


def load_geocode_cache():
    return _load_json(GEOCODE_CACHE_PATH, {})


def save_geocode_cache(cache):
    _save_json(GEOCODE_CACHE_PATH, cache)


_geocode_cache = load_geocode_cache()


def load_seen_jobs():
    return _load_json(SEEN_JOBS_PATH, {})


def save_seen_jobs(seen):
    _save_json(SEEN_JOBS_PATH, seen)


def load_overrides():
    return _load_json(OVERRIDES_PATH, {})


def save_overrides(overrides):
    _save_json(OVERRIDES_PATH, overrides)


def load_history():
    return _load_json(HISTORY_PATH, [])


def append_history(entry):
    history = load_history()
    history.append(entry)
    # Keep the file bounded — last 200 runs is plenty for a trend view.
    history = history[-200:]
    _save_json(HISTORY_PATH, history)


def haversine_miles(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


OUTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$", re.IGNORECASE)


def resolve_home(settings, search_from_override=None):
    """Returns (coords, label) for the point distances are measured from.

    Normally that's the saved home postcode. If a one-off "search from"
    value is given (a city name, a full postcode, or an outcode like M1),
    that's used instead — for this call only. It's never written back to
    settings, so the daily scheduled search keeps using the saved home
    postcode. If the override can't be located, this raises rather than
    silently falling back to home — searching from the wrong place while
    appearing to search from the right one would be worse than an error."""
    override = (search_from_override or "").strip()
    if not override:
        home_label = settings.get("home_postcode", "WD6")
        return geocode_outcode(home_label), home_label
    if OUTCODE_RE.match(override):
        coords = geocode_outcode(override)
    else:
        coords = geocode_place(override)
    if not coords:
        raise ValueError(
            f"Couldn't find the location '{override}' — try a full postcode "
            f"or a well-known town/city name."
        )
    return coords, override


def geocode_outcode(outcode):
    key = f"outcode:{outcode.upper()}"
    if key in _geocode_cache:
        return _geocode_cache[key]
    resp = requests.get(POSTCODES_IO_OUTCODE_URL.format(outcode=outcode.upper()), timeout=10)
    if resp.status_code != 200:
        return None
    data = resp.json().get("result")
    if not data:
        return None
    coords = {"lat": data["latitude"], "lon": data["longitude"]}
    _geocode_cache[key] = coords
    save_geocode_cache(_geocode_cache)
    return coords


def normalize_uk_postcode(text):
    """If text looks like a full UK postcode (with or without the internal
    space), returns it properly spaced, e.g. 'S110FQ' -> 'S11 0FQ'.
    Returns None if it doesn't match the pattern (i.e. it's a place name)."""
    if not text:
        return None
    m = UK_POSTCODE_RE.match(text.strip())
    if m:
        return f"{m.group(1).upper()} {m.group(2).upper()}"
    return None


# Rough bounding box covering all of the UK (Scilly Isles to Shetland,
# Kent to western Ireland-adjacent waters) — used to catch geocoding
# results that are wildly wrong (e.g. from a malformed query silently
# matching some unrelated place) before they ever get cached and treated
# as trustworthy. A result outside this box for a UK postcode/place search
# is essentially certain to be a geocoding error, not a real answer.
UK_BOUNDS = {"lat_min": 49.5, "lat_max": 61.0, "lon_min": -8.7, "lon_max": 2.0}


def looks_like_uk_coords(coords):
    if not coords:
        return False
    return (
        UK_BOUNDS["lat_min"] <= coords["lat"] <= UK_BOUNDS["lat_max"]
        and UK_BOUNDS["lon_min"] <= coords["lon"] <= UK_BOUNDS["lon_max"]
    )


def geocode_postcode(postcode):
    """Geocode a full UK postcode via postcodes.io — much more reliable
    for exact postcodes than a general-purpose geocoder, which was
    confirmed to badly mishandle Reed's squished-together format (one real
    case: 'S110FQ' geocoded to a location ~4,000 miles away instead of
    Sheffield, wrongly excluding a genuinely matching, nearby role).

    Never caches (or returns) a result outside the UK bounding box — a
    previous bug caused one bad result to get cached and then silently
    keep being returned even after the underlying bug was fixed, because
    the cache was trusted unconditionally. This check means a clearly
    wrong result can't poison the cache the same way again, regardless of
    what causes it next time."""
    coords, _reason = geocode_postcode_traced(postcode)
    return coords


def geocode_postcode_traced(postcode):
    """Same as geocode_postcode, but always returns (coords_or_None,
    reason_string) so a failure can be explained rather than just being a
    silent None — used by the director-candidates diagnostic. Catches
    broader exceptions than just network errors, since an unexpected
    response shape (e.g. a changed API field name) would previously have
    crashed silently past a narrower except clause without ever being
    visible anywhere."""
    key = f"postcode:{postcode.upper()}"
    if key in _geocode_cache:
        cached = _geocode_cache[key]
        if looks_like_uk_coords(cached):
            return cached, "used cached value"
        # A previously-cached bad value — don't trust it, re-fetch instead.
    try:
        url = POSTCODES_IO_POSTCODE_URL.format(postcode=quote(postcode))
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None, f"postcodes.io returned HTTP {resp.status_code} for {url}"
        body = resp.json()
        data = body.get("result")
        if not data:
            return None, f"postcodes.io returned 200 but no 'result' field (body: {str(body)[:150]})"
        if "latitude" not in data or "longitude" not in data:
            return None, f"postcodes.io result missing latitude/longitude (got keys: {list(data.keys())})"
        coords = {"lat": data["latitude"], "lon": data["longitude"]}
        if not looks_like_uk_coords(coords):
            return None, f"postcodes.io returned implausible coords {coords}, rejected by UK bounds check"
        _geocode_cache[key] = coords
        save_geocode_cache(_geocode_cache)
        return coords, "ok, freshly fetched"
    except requests.RequestException as e:
        return None, f"postcodes.io request failed: {e}"
    except (ValueError, KeyError, TypeError) as e:
        return None, f"postcodes.io response parsing failed: {e}"


def geocode_place(place_name):
    coords, _reason = geocode_place_traced(place_name)
    return coords


def geocode_place_traced(place_name):
    """Same as geocode_place, but always returns (coords_or_None,
    reason_string) explaining what happened at each stage tried — used by
    the director-candidates diagnostic so a location failure is visible
    and explained rather than just showing up as 'unknown distance'.

    Three tiers, each falling back to the next only if the previous
    genuinely failed:
    1. Exact postcode via postcodes.io (most accurate)
    2. Free-text search via Nominatim, constrained to GB via the API's own
       countrycodes parameter (not just a ", UK" text hint, which one real
       case showed can still be overridden — it matched a location in
       Kentucky, USA, despite the hint)
    3. The postcode's outward code only (e.g. "S11" from "S11 0FQ"), via
       the same outcode lookup already proven reliable for the home
       postcode throughout this app — an approximate (district-centroid)
       location, accurate to roughly a mile or two, rather than nothing.
       Only reached if the exact postcode genuinely isn't in either
       geocoder's database — confirmed to happen for at least one real
       posting (postcodes.io 404, Nominatim's UK-constrained search also
       failing to place it precisely)."""
    if not place_name:
        return None, "no location given"

    postcode = normalize_uk_postcode(place_name)
    query_string = postcode or place_name  # use the properly-spaced version
    # for the fallback too if this looked like a postcode — Nominatim
    # mishandles the squished-together format just as badly as the
    # primary lookup did before it was fixed, so a fallback that still
    # used the raw original string would remain exposed to that same bug.
    if postcode:
        coords, postcode_reason = geocode_postcode_traced(postcode)
        if coords:
            return coords, f"postcode lookup: {postcode_reason}"
        # Fall through to Nominatim below only if postcodes.io itself
        # failed (e.g. a genuinely invalid/unassigned postcode) — better
        # to try the general geocoder than give up entirely.
    else:
        postcode_reason = "not recognised as a postcode"

    key = f"place:{query_string.strip().lower()}"
    if key in _geocode_cache:
        cached = _geocode_cache[key]
        if cached is None or looks_like_uk_coords(cached):
            if cached is not None:
                return cached, f"postcode lookup: {postcode_reason}; place cache: hit"
            # A cached "no result" — still worth trying the outcode
            # fallback below rather than giving up, since that's a
            # different data source that might succeed where this failed.
        else:
            pass  # implausible cached value — don't trust it, re-fetch below
    nominatim_reason = None
    params = {"q": query_string, "format": "json", "limit": 1, "countrycodes": "gb"}
    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers=NOMINATIM_HEADERS, timeout=10)
        time.sleep(1)
        if resp.status_code != 200:
            nominatim_reason = f"Nominatim returned HTTP {resp.status_code}"
        else:
            results = resp.json()
            if not results:
                _geocode_cache[key] = None
                save_geocode_cache(_geocode_cache)
                nominatim_reason = f"Nominatim found no match for {query_string!r}"
            else:
                coords = {"lat": float(results[0]["lat"]), "lon": float(results[0]["lon"])}
                if not looks_like_uk_coords(coords):
                    nominatim_reason = f"Nominatim returned implausible coords {coords}, rejected"
                else:
                    _geocode_cache[key] = coords
                    save_geocode_cache(_geocode_cache)
                    return coords, f"postcode lookup: {postcode_reason}; Nominatim ok, freshly fetched"
    except requests.RequestException as e:
        nominatim_reason = f"Nominatim request failed: {e}"
    except (ValueError, KeyError, TypeError) as e:
        nominatim_reason = f"Nominatim response parsing failed: {e}"

    # Both the exact postcode and free-text search failed. If this was a
    # postcode, fall back to its outward code only — approximate, but a
    # real, reliable data source rather than giving up entirely.
    if postcode:
        outcode = postcode.split()[0]
        outcode_coords = geocode_outcode(outcode)
        if outcode_coords:
            return outcode_coords, (
                f"postcode lookup: {postcode_reason}; {nominatim_reason}; "
                f"fell back to outward code {outcode!r} centroid (approximate)"
            )

    return None, f"postcode lookup: {postcode_reason}; {nominatim_reason}"


OFFICE_FREQUENCY_PATTERNS = [
    re.compile(r"\d+\s*-?\s*\d*\s*days?\s*(a|per)\s*month", re.I),
    re.compile(r"\d+\s*-?\s*\d*\s*days?\s*(a|per)\s*week", re.I),
    re.compile(r"fully\s*remote", re.I),
]


def find_office_frequency_hint(text):
    if not text:
        return None
    for pattern in OFFICE_FREQUENCY_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0)
    return None


# Matches a sentence-ish chunk containing "partner" or "partnership" — used
# to search for a stated timeframe near that specific mention, rather than
# picking up an unrelated duration elsewhere in the description (e.g.
# "5 years' post-qualification experience required").
PARTNERSHIP_MENTION_RE = re.compile(r"([^.]*\bpartner(?:ship)?\b[^.]*)", re.IGNORECASE)

# Matches a number (or a range, e.g. "12-18") followed by a month/year unit.
PARTNERSHIP_DURATION_RE = re.compile(r"(\d+)\s*(?:-\s*(\d+)\s*)?\s*(months?|yrs?|years?)\b", re.IGNORECASE)


def find_partnership_timeline_signal(text, fast_track_phrases):
    """Looks for a stated partnership timeframe near an actual mention of
    "partner"/"partnership" (not just any number in the text), and
    classifies it into a tier:
    - 'fast': explicit route under 12 months
    - 'medium': explicit route 12-18 months
    - 'neutral_explicit': explicit route 19-24 months (stated, but doesn't
      qualify for a boost or the penalty)
    - 'slow': explicit route over 24 months (2 years)
    - 'vague': a "fast-track"-style phrase with no actual number given
    - None: no partnership-timeline signal found at all

    For a stated range (e.g. "12-18 months"), uses the upper bound, since
    that's the honest ceiling of what's being promised.

    Returns a dict {tier, months, matched_text} or None."""
    if not text:
        return None

    for sentence in PARTNERSHIP_MENTION_RE.findall(text):
        dur_match = PARTNERSHIP_DURATION_RE.search(sentence)
        if not dur_match:
            continue
        low = int(dur_match.group(1))
        high = int(dur_match.group(2)) if dur_match.group(2) else None
        unit = dur_match.group(3).lower()
        value = high if high is not None else low
        months = value * 12 if unit.startswith("y") else value

        if months <= 12:
            tier = "fast"
        elif months <= 18:
            tier = "medium"
        elif months <= 24:
            tier = "neutral_explicit"
        else:
            tier = "slow"
        return {"tier": tier, "months": months, "matched_text": dur_match.group(0).strip()}

    # No explicit number found near a partner/partnership mention — check
    # for vague fast-track phrasing as a weaker, flagged-as-unverified signal.
    lowered = text.lower()
    for phrase in fast_track_phrases:
        if phrase in lowered:
            return {"tier": "vague", "months": None, "matched_text": phrase}

    return None


def partnership_timeline_score(signal):
    if not signal:
        return 0
    return {
        "fast": PARTNERSHIP_FAST_BOOST,
        "medium": PARTNERSHIP_MEDIUM_BOOST,
        "vague": PARTNERSHIP_VAGUE_BOOST,
        "slow": PARTNERSHIP_SLOW_PENALTY,
    }.get(signal["tier"], 0)


# Reed's job description text sometimes contains raw HTML — actual tags
# (e.g. <p>, <br>, <li>) and/or HTML entities (e.g. "&amp;" for "&"). Left
# undecoded, these show up literally in the UI (a real case: "Audit &amp;
# Accounts Director" instead of "Audit & Accounts Director") because the
# frontend's own HTML-escaping doubles up on top of Reed's existing
# entities. Cleaned once here, right when the text is read, so every
# downstream use (snippets, keyword matching, everything) sees plain text.
HTML_TAG_RE = re.compile(r"<[^>]+>")


def clean_html_text(text):
    if not text:
        return text
    without_tags = HTML_TAG_RE.sub(" ", text)
    decoded = html.unescape(without_tags)
    return re.sub(r"\s+", " ", decoded).strip()


def text_contains_any(text, phrases):
    if not text:
        return []
    lowered = text.lower()
    return [p for p in phrases if p.lower() in lowered]


# Standalone "RI" (the abbreviation for Registered/Responsible Individual)
# needs a word-boundary check, not a plain substring — real postings very
# commonly write it as "(RI)", "RI status", "RI," etc., where there's no
# space on both sides. A literal " ri " substring check misses all of
# these. \b matches on the transition between a word character and a
# non-word one (including punctuation, parentheses, and string start/end),
# so this correctly catches "(RI)" while still not matching "RI" hidden
# inside an unrelated word like "PRIORITY".
RI_STANDALONE_RE = re.compile(r"\bri\b", re.IGNORECASE)


def find_ri_hits(text, ri_phrases):
    hits = text_contains_any(text, ri_phrases)
    if text and RI_STANDALONE_RE.search(text) and "RI" not in hits:
        hits.append("RI")
    return hits


def is_remote_or_hybrid(text, keywords):
    if not text:
        return False
    lowered = text.lower()
    return any(k.lower() in lowered for k in keywords)


def classify_firm(text, settings):
    """Returns 'big4_top10', 'independent_signal', 'large_firm_signal', or 'unclassified'."""
    if not text:
        return "unclassified"
    lowered = text.lower()
    if any(name in lowered for name in settings.get("big4_top10_firms", [])):
        return "big4_top10"
    if any(phrase in lowered for phrase in settings.get("independent_signal_phrases", [])):
        return "independent_signal"
    if any(phrase in lowered for phrase in settings.get("large_firm_signal_phrases", [])):
        return "large_firm_signal"
    return "unclassified"


def compute_fit_score(audit_hits, ri_hits, equity_hits, firm_classification, ri_progression_hits=None, partnership_signal=None):
    score = len(audit_hits) + len(ri_hits) + len(equity_hits)
    if firm_classification == "independent_signal":
        score += 3
    if ri_progression_hits:
        score += RI_PROGRESSION_BOOST
    score += partnership_timeline_score(partnership_signal)
    return score


def normalize_key(title, location):
    raw = f"{title or ''}|{location or ''}".lower()
    return re.sub(r"[^a-z0-9|]", "", raw)


def is_stale(date_posted):
    if not date_posted:
        return False
    try:
        posted_dt = datetime.fromisoformat(date_posted.replace("Z", "+00:00"))
        age_days = (datetime.now(posted_dt.tzinfo) - posted_dt).days
        return age_days > STALE_THRESHOLD_DAYS
    except ValueError:
        return False


def detect_day_rate(description, job_type_hint, min_salary):
    """Best-effort: flags a salary figure that's probably a day rate, not
    annual, so it doesn't get wrongly compared against an annual min/max
    salary filter."""
    text = (description or "").lower()
    mentions_day_rate = any(p in text for p in ["day rate", "per day", "daily rate", "/day"])
    looks_like_contract = job_type_hint == "contract" or "contract" in text
    small_number = bool(min_salary) and min_salary < DAY_RATE_SALARY_CEILING
    return mentions_day_rate or (looks_like_contract and small_number)


def diagnose_exclusion_reason(title, description, settings, require_title_match, require_audit,
                               require_ri, home, radius_miles, include_remote_hybrid, location_name,
                               require_general_practice=False):
    """Same filter logic as apply_filters, but returns a plain-English
    reason for why a candidate was excluded (or 'PASSES' if it wasn't) —
    used only for the title-matching-candidates diagnostic, never in the
    real search path, so it can't affect real results."""
    combined_text = f"{title} {description}"

    excl = text_contains_any(title, settings.get("exclude_title_keywords", []))
    if excl:
        return f"excluded by title keyword: {excl}"

    title_matches = text_contains_any(title, settings["title_must_include_any"])
    if require_title_match and not title_matches:
        return "title does not contain Director/Partner designate"

    # RI and general practice are both audit-specific/audit-implying
    # signals — either counts as evidence of audit involvement even
    # without the literal word "audit" (matches apply_filters exactly).
    ri_hits = find_ri_hits(combined_text, settings["must_mention_any"]["ri_support"])
    general_practice_hits = text_contains_any(combined_text, settings["must_mention_any"].get("general_practice", []))
    audit_hits = text_contains_any(combined_text, settings["must_mention_any"]["audit"])
    audit_satisfied = bool(audit_hits) or bool(ri_hits) or bool(general_practice_hits)
    if require_audit and not audit_satisfied:
        return "no audit mention found (and no RI or general practice mention, either of which would also count)"

    if require_ri and not ri_hits:
        return "no RI mention found"
    if require_general_practice and not general_practice_hits:
        return "no general practice mention found"

    firm_classification = classify_firm(combined_text, settings)
    if firm_classification == "big4_top10":
        return "excluded — description mentions a Big 4/Top 10 firm"

    remote_hybrid = is_remote_or_hybrid(combined_text, settings["remote_hybrid_keywords"])
    if home and location_name:
        job_coords, geocode_reason = geocode_place_traced(location_name)
    else:
        job_coords, geocode_reason = None, "no home postcode configured" if not home else "no location given"
    distance = None
    if job_coords:
        distance = round(haversine_miles(home["lat"], home["lon"], job_coords["lat"], job_coords["lon"]), 1)
    within_radius = distance is not None and distance <= radius_miles
    passes_location = within_radius or (include_remote_hybrid and remote_hybrid)
    if not passes_location:
        if distance is not None:
            dist_str = f"{distance} mi"
        else:
            dist_str = f"unknown distance ({geocode_reason})"
        return f"excluded by location — {dist_str} from home, not flagged remote/hybrid (radius {radius_miles} mi)"

    return "PASSES all filters"


def diagnose_director_candidates(settings, params):
    """Runs the real Reed + scraper pipeline, but reports every candidate
    whose TITLE contains 'director' or 'partner designate' — regardless of
    whether it passed the other filters — along with the exact reason it
    was kept or excluded. Deliberately narrow (title-matching only) so the
    output stays small and safe to paste, since a full unfiltered dump
    previously grew large enough to break pasting into chat."""
    keywords = params.get("keywords", "accounting")
    radius_miles = params.get("radius_miles", settings.get("default_radius_miles", 20))
    include_remote_hybrid = params.get("include_remote_hybrid", True)
    require_audit = params.get("require_audit", True)
    require_ri = params.get("require_ri", True)
    require_title_match = params.get("require_title_match", True)
    require_general_practice = params.get("require_general_practice", False)

    home, home_label = resolve_home(settings, params.get("search_from_override"))
    title_filter_words = ["director", "partner designate"]

    def is_director_titled(title):
        lowered = (title or "").lower()
        return any(w in lowered for w in title_filter_words)

    results = []

    if settings.get("enabled_sources", {}).get("Reed", True) and settings.get("reed_api_key"):
        resp = requests.get(
            REED_SEARCH_URL, params={"keywords": keywords, "resultsToTake": 100},
            auth=(settings["reed_api_key"], ""), timeout=20,
        )
        if resp.status_code == 200:
            for job in resp.json().get("results", []):
                title = job.get("jobTitle", "")
                if not is_director_titled(title):
                    continue
                reason = diagnose_exclusion_reason(
                    title, clean_html_text(job.get("jobDescription", "")), settings, require_title_match,
                    require_audit, require_ri, home, radius_miles, include_remote_hybrid,
                    job.get("locationName", ""), require_general_practice,
                )
                results.append({
                    "source": "Reed", "job_title": title, "location": job.get("locationName"),
                    "job_url": job.get("jobUrl"), "reason": reason,
                })

    enabled_sources = settings.get("enabled_sources", {})
    sites_to_scrape = [s for s in scrapers.SITE_REGISTRY if enabled_sources.get(s["name"], True)]
    scraped = scrapers.scrape_all_sites(sites=sites_to_scrape)
    for site_name, outcome in scraped.items():
        for cand in outcome["candidates"]:
            title = cand["job_title"]
            if not is_director_titled(title):
                continue
            reason = diagnose_exclusion_reason(
                title, cand.get("description_snippet", ""), settings, require_title_match,
                require_audit, require_ri, home, radius_miles, include_remote_hybrid,
                cand.get("location_name"), require_general_practice,
            )
            results.append({
                "source": site_name, "job_title": title, "location": cand.get("location_name"),
                "job_url": cand.get("job_url"), "reason": reason,
            })

    return results


def apply_filters(title, description, settings, require_title_match, require_audit, require_ri, require_general_practice=False):
    """Returns a dict with 'passes' plus all the derived fields, or
    passes=False with safe defaults for the rest."""
    empty = dict(
        passes=False, title_matches=[], audit_hits=[], ri_hits=[], equity_hits=[],
        remote_hybrid=False, office_hint=None, firm_classification="unclassified", fit_score=0,
        ri_progression_hits=[], partnership_signal=None, general_practice_hits=[],
    )
    combined_text = f"{title} {description}"

    if text_contains_any(title, settings.get("exclude_title_keywords", [])):
        return empty

    title_matches = text_contains_any(title, settings["title_must_include_any"])
    if require_title_match and not title_matches:
        return {**empty, "title_matches": title_matches}

    # RI is an audit-specific qualification — a posting mentioning it is
    # genuine evidence of audit involvement even if the word "audit"
    # itself never appears (e.g. some General Practice postings describe
    # the audit work without using that literal word). Computed before
    # the audit check so that inference can count, regardless of whether
    # "must mention RI" is separately switched on.
    # General practice is also computed up front, same reasoning as RI —
    # a "general practice" mention is a genuine signal of audit exposure
    # (general practice work typically includes it) even when the literal
    # word "audit" never appears, so it counts toward "must mention audit"
    # too. Without this, ticking both Audit and General practice required
    # both literal phrase-sets to appear together in the same posting,
    # which real general-practice roles rarely phrase that way — so
    # ticking both nearly always produced nil results. With this fix,
    # ticking both no longer needs the literal word "audit" specifically;
    # a genuine general-practice mention satisfies that side on its own.
    ri_hits = find_ri_hits(combined_text, settings["must_mention_any"]["ri_support"])
    general_practice_hits = text_contains_any(combined_text, settings["must_mention_any"].get("general_practice", []))
    audit_hits = text_contains_any(combined_text, settings["must_mention_any"]["audit"])
    audit_satisfied = bool(audit_hits) or bool(ri_hits) or bool(general_practice_hits)
    if require_audit and not audit_satisfied:
        return {**empty, "title_matches": title_matches, "audit_hits": audit_hits, "ri_hits": ri_hits,
                "general_practice_hits": general_practice_hits}
    if not audit_hits:
        # Reflect whichever inference is what actually let this through,
        # so it's clear why this passed "must mention audit" despite no
        # literal audit keyword.
        if ri_hits:
            audit_hits = audit_hits + ["RI (implies audit)"]
        elif general_practice_hits:
            audit_hits = audit_hits + ["general practice (implies audit)"]

    if require_ri and not ri_hits:
        return {**empty, "title_matches": title_matches, "audit_hits": audit_hits, "ri_hits": ri_hits,
                "general_practice_hits": general_practice_hits}

    # The General practice checkbox itself is unchanged by the inference
    # above — it still only passes on a literal general-practice mention,
    # since ticking it specifically means "show me roles that frame
    # themselves as general practice," not "show me anything audit-like."
    if require_general_practice and not general_practice_hits:
        return {**empty, "title_matches": title_matches, "audit_hits": audit_hits, "ri_hits": ri_hits,
                "general_practice_hits": general_practice_hits}

    firm_classification = classify_firm(combined_text, settings)
    if firm_classification == "big4_top10":
        return {**empty, "title_matches": title_matches, "audit_hits": audit_hits, "ri_hits": ri_hits,
                "general_practice_hits": general_practice_hits, "firm_classification": firm_classification}

    equity_hits = text_contains_any(combined_text, settings["must_mention_any"].get("equity_route", []))
    ri_progression_hits = text_contains_any(combined_text, settings["must_mention_any"].get("ri_progression_offered", []))
    partnership_signal = find_partnership_timeline_signal(
        combined_text, settings.get("partnership_fast_track_phrases", [])
    )
    remote_hybrid = is_remote_or_hybrid(combined_text, settings["remote_hybrid_keywords"])
    office_hint = find_office_frequency_hint(description)
    fit_score = compute_fit_score(audit_hits, ri_hits, equity_hits, firm_classification, ri_progression_hits, partnership_signal)

    return dict(
        passes=True, title_matches=title_matches, audit_hits=audit_hits, ri_hits=ri_hits,
        equity_hits=equity_hits, remote_hybrid=remote_hybrid, office_hint=office_hint,
        firm_classification=firm_classification, fit_score=fit_score,
        ri_progression_hits=ri_progression_hits, partnership_signal=partnership_signal,
        general_practice_hits=general_practice_hits,
    )


def resolve_distance(home, location_name, radius_miles, include_remote_hybrid, remote_hybrid):
    distance = None
    if home and location_name:
        job_coords = geocode_place(location_name)
        if job_coords:
            distance = round(haversine_miles(home["lat"], home["lon"], job_coords["lat"], job_coords["lon"]), 1)
    within_radius = distance is not None and distance <= radius_miles
    passes_location = within_radius or (include_remote_hybrid and remote_hybrid)
    return distance, passes_location


def parse_salary_text(salary_text):
    if not salary_text:
        return None, None
    numbers = [int(n.replace(",", "")) for n in re.findall(r"[\d,]{4,7}", salary_text)]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return numbers[0], numbers[1]


def run_search(settings, params, tracking_path=None):
    """The whole pipeline: Reed + scraped sites, filtered, deduped, sorted,
    with new-since-last-search flags, manual overrides, stale/day-rate
    flags, and (if configured) real commute times applied. Returns
    (results, site_status).

    tracking_path: which "seen" file to check/update for the "is_new" flag.
    Defaults to SEEN_JOBS_PATH (the scheduled script's own notification
    tracking) if not given. The web app passes APP_VIEWED_JOBS_PATH
    instead, so a role you were notified about on your phone still shows
    as genuinely new the first time you actually see it in the app, and
    vice versa — each channel tracks its own "have I surfaced this yet"
    independently."""
    tracking_path = tracking_path or SEEN_JOBS_PATH
    keywords = params.get("keywords", "accounting")
    min_salary = params.get("min_salary")
    max_salary = params.get("max_salary")
    radius_miles = params.get("radius_miles", settings.get("default_radius_miles", 20))
    include_remote_hybrid = params.get("include_remote_hybrid", True)
    require_audit = params.get("require_audit", True)
    require_ri = params.get("require_ri", True)
    require_title_match = params.get("require_title_match", True)
    require_general_practice = params.get("require_general_practice", False)
    job_type = params.get("job_type", "any")
    employment_type = params.get("employment_type", "any")  # any | full_time | part_time
    max_age_days = params.get("max_age_days")
    sort_by = params.get("sort_by", "fit")
    mark_new = params.get("mark_new", True)

    enabled_sources = settings.get("enabled_sources", {})
    reed_enabled = enabled_sources.get("Reed", True)
    overrides = load_overrides()

    home, home_label = resolve_home(settings, params.get("search_from_override"))

    results = []
    seen_keys = set()
    site_status = {}

    if reed_enabled and settings.get("reed_api_key"):
        reed_params = {"keywords": keywords, "resultsToTake": 100}
        if min_salary:
            reed_params["minimumSalary"] = min_salary
        if max_salary:
            reed_params["maximumSalary"] = max_salary
        if job_type == "permanent":
            reed_params["permanent"] = "true"
        elif job_type == "contract":
            reed_params["contract"] = "true"
        elif job_type == "temp":
            reed_params["temp"] = "true"
        if employment_type == "full_time":
            reed_params["fullTime"] = "true"
        elif employment_type == "part_time":
            reed_params["partTime"] = "true"

        resp = requests.get(REED_SEARCH_URL, params=reed_params, auth=(settings["reed_api_key"], ""), timeout=20)
        if resp.status_code != 200:
            site_status["Reed"] = f"error: HTTP {resp.status_code}"
        else:
            site_status["Reed"] = "ok"
            for job in resp.json().get("results", []):
                title = job.get("jobTitle", "")
                description = clean_html_text(job.get("jobDescription", ""))
                f = apply_filters(title, description, settings, require_title_match, require_audit, require_ri, require_general_practice)
                if not f["passes"]:
                    continue

                date_posted = job.get("date")
                if max_age_days and date_posted:
                    try:
                        posted_dt = datetime.fromisoformat(date_posted.replace("Z", "+00:00"))
                        if (datetime.now(posted_dt.tzinfo) - posted_dt).days > max_age_days:
                            continue
                    except ValueError:
                        pass

                location_name = job.get("locationName", "")
                key = normalize_key(title, location_name)

                remote_hybrid = f["remote_hybrid"]
                remote_manually_set = False
                if key in overrides and "remote_override" in overrides[key]:
                    remote_hybrid = overrides[key]["remote_override"]
                    remote_manually_set = True

                distance, passes_location = resolve_distance(
                    home, location_name, radius_miles, include_remote_hybrid, remote_hybrid
                )
                if not passes_location:
                    continue

                seen_keys.add(key)
                min_sal = job.get("minimumSalary")
                max_sal = job.get("maximumSalary")
                is_day_rate = detect_day_rate(description, job_type, min_sal)

                results.append({
                    "job_title": title,
                    "employer_name": job.get("employerName"),
                    "location_name": location_name,
                    "distance_miles": distance,
                    "minimum_salary": min_sal,
                    "maximum_salary": max_sal,
                    "salary_listed": bool(min_sal or max_sal),
                    "is_day_rate": is_day_rate,
                    "date_posted": date_posted,
                    "is_stale": is_stale(date_posted),
                    "job_url": job.get("jobUrl"),
                    "remote_or_hybrid": remote_hybrid,
                    "remote_manually_set": remote_manually_set,
                    "office_frequency_hint": f["office_hint"],
                    "matched_title_terms": f["title_matches"],
                    "matched_audit_terms": f["audit_hits"],
                    "matched_ri_terms": f["ri_hits"],
                    "matched_equity_terms": f["equity_hits"],
                    "matched_general_practice_terms": f.get("general_practice_hits", []),
                    "firm_classification": f["firm_classification"],
                    "fit_score": f["fit_score"],
                    "ri_progression_offered": bool(f.get("ri_progression_hits")),
                    "partnership_timeline_tier": (f.get("partnership_signal") or {}).get("tier"),
                    "partnership_timeline_text": (f.get("partnership_signal") or {}).get("matched_text"),
                    "description_snippet": (description[:280] + "…") if len(description) > 280 else description,
                    "source": "Reed",
                    "job_key": key,
                })

    # Adzuna — a genuine, legitimate UK job aggregator with a free
    # self-serve API (unlike Indeed, which has no self-service option and
    # explicitly prohibits scraping in its own terms). Structured the same
    # way as Reed above, but does check against Reed's own keys first
    # (Reed's own loop doesn't self-check, matching its existing design),
    # since these are the two sources most likely to genuinely double-post
    # the exact same real role.
    adzuna_enabled = enabled_sources.get("Adzuna", True)
    if adzuna_enabled and settings.get("adzuna_app_id") and settings.get("adzuna_app_key"):
        adzuna_params = {
            "app_id": settings["adzuna_app_id"], "app_key": settings["adzuna_app_key"],
            "what": keywords, "results_per_page": 50, "content-type": "application/json",
        }
        if min_salary:
            adzuna_params["salary_min"] = min_salary
        if max_salary:
            adzuna_params["salary_max"] = max_salary
        if job_type == "permanent":
            adzuna_params["permanent"] = 1
        elif job_type == "contract":
            adzuna_params["contract"] = 1
        if employment_type == "full_time":
            adzuna_params["full_time"] = 1
        elif employment_type == "part_time":
            adzuna_params["part_time"] = 1
        if max_age_days:
            adzuna_params["max_days_old"] = max_age_days

        try:
            resp = requests.get(ADZUNA_SEARCH_URL.format(page=1), params=adzuna_params, timeout=20)
        except requests.RequestException as e:
            site_status["Adzuna"] = f"error: {e}"
            resp = None

        if resp is not None:
            if resp.status_code != 200:
                site_status["Adzuna"] = f"error: HTTP {resp.status_code}"
            else:
                site_status["Adzuna"] = "ok"
                for job in resp.json().get("results", []):
                    title = job.get("title", "")
                    description = clean_html_text(job.get("description", ""))
                    f = apply_filters(title, description, settings, require_title_match, require_audit, require_ri, require_general_practice)
                    if not f["passes"]:
                        continue

                    date_posted = job.get("created")
                    if max_age_days and date_posted:
                        try:
                            posted_dt = datetime.fromisoformat(date_posted.replace("Z", "+00:00"))
                            if (datetime.now(posted_dt.tzinfo) - posted_dt).days > max_age_days:
                                continue
                        except ValueError:
                            pass

                    location_name = (job.get("location") or {}).get("display_name", "")
                    key = normalize_key(title, location_name)
                    if key in seen_keys:
                        continue  # already have this exact role from Reed

                    remote_hybrid = f["remote_hybrid"]
                    remote_manually_set = False
                    if key in overrides and "remote_override" in overrides[key]:
                        remote_hybrid = overrides[key]["remote_override"]
                        remote_manually_set = True

                    distance, passes_location = resolve_distance(
                        home, location_name, radius_miles, include_remote_hybrid, remote_hybrid
                    )
                    if not passes_location:
                        continue

                    seen_keys.add(key)
                    min_sal = job.get("salary_min")
                    max_sal = job.get("salary_max")
                    is_day_rate = detect_day_rate(description, job_type, min_sal)

                    results.append({
                        "job_title": title,
                        "employer_name": (job.get("company") or {}).get("display_name"),
                        "location_name": location_name,
                        "distance_miles": distance,
                        "minimum_salary": min_sal,
                        "maximum_salary": max_sal,
                        "salary_listed": bool(min_sal or max_sal),
                        "is_day_rate": is_day_rate,
                        "date_posted": date_posted,
                        "is_stale": is_stale(date_posted),
                        "job_url": job.get("redirect_url"),
                        "remote_or_hybrid": remote_hybrid,
                        "remote_manually_set": remote_manually_set,
                        "office_frequency_hint": f["office_hint"],
                        "matched_title_terms": f["title_matches"],
                        "matched_audit_terms": f["audit_hits"],
                        "matched_ri_terms": f["ri_hits"],
                        "matched_equity_terms": f["equity_hits"],
                        "matched_general_practice_terms": f.get("general_practice_hits", []),
                        "firm_classification": f["firm_classification"],
                        "fit_score": f["fit_score"],
                        "ri_progression_offered": bool(f.get("ri_progression_hits")),
                        "partnership_timeline_tier": (f.get("partnership_signal") or {}).get("tier"),
                        "partnership_timeline_text": (f.get("partnership_signal") or {}).get("matched_text"),
                        "description_snippet": (description[:280] + "…") if len(description) > 280 else description,
                        "source": "Adzuna",
                        "job_key": key,
                    })

    # Jooble — another legitimate free aggregator API, POST-based unlike
    # Reed/Adzuna. Its salary field is free text (not structured min/max),
    # so it's run through the same parse_salary_text used for scraped
    # sites. Response field names are based on Jooble's documented
    # examples rather than a live-tested call, since a real key wasn't
    # available while building this — defensively coded with .get()
    # throughout, and the diagnostic endpoint will show clearly if any
    # field name needs correcting once tested against a real key.
    jooble_enabled = enabled_sources.get("Jooble", True)
    if jooble_enabled and settings.get("jooble_api_key"):
        jooble_body = {"keywords": keywords, "page": "1"}
        try:
            resp = requests.post(
                JOOBLE_SEARCH_URL.format(key=settings["jooble_api_key"]),
                json=jooble_body, timeout=20,
            )
        except requests.RequestException as e:
            site_status["Jooble"] = f"error: {e}"
            resp = None

        if resp is not None:
            if resp.status_code != 200:
                site_status["Jooble"] = f"error: HTTP {resp.status_code}"
            else:
                site_status["Jooble"] = "ok"
                for job in resp.json().get("jobs", []):
                    title = job.get("title", "")
                    description = clean_html_text(job.get("snippet", ""))
                    f = apply_filters(title, description, settings, require_title_match, require_audit, require_ri, require_general_practice)
                    if not f["passes"]:
                        continue

                    date_posted = job.get("updated")
                    if max_age_days and date_posted:
                        try:
                            posted_dt = datetime.fromisoformat(date_posted.replace("Z", "+00:00"))
                            if (datetime.now(posted_dt.tzinfo) - posted_dt).days > max_age_days:
                                continue
                        except ValueError:
                            pass

                    location_name = job.get("location", "")
                    key_norm = normalize_key(title, location_name)
                    if key_norm in seen_keys:
                        continue  # already have this from Reed or Adzuna

                    remote_hybrid = f["remote_hybrid"]
                    remote_manually_set = False
                    if key_norm in overrides and "remote_override" in overrides[key_norm]:
                        remote_hybrid = overrides[key_norm]["remote_override"]
                        remote_manually_set = True

                    distance, passes_location = resolve_distance(
                        home, location_name, radius_miles, include_remote_hybrid, remote_hybrid
                    )
                    if not passes_location:
                        continue

                    min_sal, max_sal = parse_salary_text(job.get("salary"))
                    if min_salary and (max_sal or 0) < min_salary:
                        continue
                    if max_salary and (min_sal or 10**9) > max_salary:
                        continue

                    seen_keys.add(key_norm)
                    is_day_rate = detect_day_rate(description, job_type, min_sal)

                    results.append({
                        "job_title": title,
                        "employer_name": job.get("company"),
                        "location_name": location_name,
                        "distance_miles": distance,
                        "minimum_salary": min_sal,
                        "maximum_salary": max_sal,
                        "salary_listed": bool(min_sal or max_sal),
                        "is_day_rate": is_day_rate,
                        "date_posted": date_posted,
                        "is_stale": is_stale(date_posted),
                        "job_url": job.get("link"),
                        "remote_or_hybrid": remote_hybrid,
                        "remote_manually_set": remote_manually_set,
                        "office_frequency_hint": f["office_hint"],
                        "matched_title_terms": f["title_matches"],
                        "matched_audit_terms": f["audit_hits"],
                        "matched_ri_terms": f["ri_hits"],
                        "matched_equity_terms": f["equity_hits"],
                        "matched_general_practice_terms": f.get("general_practice_hits", []),
                        "firm_classification": f["firm_classification"],
                        "fit_score": f["fit_score"],
                        "ri_progression_offered": bool(f.get("ri_progression_hits")),
                        "partnership_timeline_tier": (f.get("partnership_signal") or {}).get("tier"),
                        "partnership_timeline_text": (f.get("partnership_signal") or {}).get("matched_text"),
                        "description_snippet": (description[:280] + "…") if len(description) > 280 else description,
                        "source": "Jooble",
                        "job_key": key_norm,
                    })

    sites_to_scrape = [s for s in scrapers.SITE_REGISTRY if enabled_sources.get(s["name"], True)]
    if sites_to_scrape:
        scraped = scrapers.scrape_all_sites(sites=sites_to_scrape)
        for site_name, outcome in scraped.items():
            site_status[site_name] = outcome["status"]
            for cand in outcome["candidates"]:
                title = cand["job_title"]
                description = cand["description_snippet"]
                f = apply_filters(title, description, settings, require_title_match, require_audit, require_ri, require_general_practice)
                if not f["passes"]:
                    continue

                location_name = cand.get("location_name")
                key = normalize_key(title, location_name)
                if key in seen_keys:
                    continue

                remote_hybrid = f["remote_hybrid"]
                remote_manually_set = False
                if key in overrides and "remote_override" in overrides[key]:
                    remote_hybrid = overrides[key]["remote_override"]
                    remote_manually_set = True

                distance, passes_location = resolve_distance(
                    home, location_name, radius_miles, include_remote_hybrid, remote_hybrid
                )
                if not passes_location:
                    continue

                min_sal, max_sal = parse_salary_text(cand.get("salary_text"))
                is_day_rate = detect_day_rate(description, job_type, min_sal)
                if not is_day_rate:
                    if min_salary and (max_sal or 0) < min_salary:
                        continue
                    if max_salary and (min_sal or 10**9) > max_salary:
                        continue

                seen_keys.add(key)
                results.append({
                    "job_title": title,
                    "employer_name": None,
                    "location_name": location_name,
                    "distance_miles": distance,
                    "minimum_salary": min_sal,
                    "maximum_salary": max_sal,
                    "salary_listed": bool(min_sal or max_sal),
                    "is_day_rate": is_day_rate,
                    "date_posted": None,
                    "is_stale": False,  # no reliable date from these sources
                    "job_url": cand["job_url"],
                    "remote_or_hybrid": remote_hybrid,
                    "remote_manually_set": remote_manually_set,
                    "office_frequency_hint": f["office_hint"],
                    "matched_title_terms": f["title_matches"],
                    "matched_audit_terms": f["audit_hits"],
                    "matched_ri_terms": f["ri_hits"],
                    "matched_equity_terms": f["equity_hits"],
                    "matched_general_practice_terms": f.get("general_practice_hits", []),
                    "firm_classification": f["firm_classification"],
                    "fit_score": f["fit_score"],
                    "ri_progression_offered": bool(f.get("ri_progression_hits")),
                    "partnership_timeline_tier": (f.get("partnership_signal") or {}).get("tier"),
                    "partnership_timeline_text": (f.get("partnership_signal") or {}).get("matched_text"),
                    "description_snippet": description[:280] + ("…" if len(description) > 280 else ""),
                    "source": site_name,
                    "job_key": key,
                })

    # ---- New-since-last-search flagging ----
    seen_store = _load_json(tracking_path, {})
    now_iso = datetime.now(timezone.utc).isoformat()
    new_count = 0
    for r in results:
        if r["job_key"] not in seen_store:
            r["is_new"] = True
            new_count += 1
            seen_store[r["job_key"]] = {"first_seen": now_iso, "job_title": r["job_title"]}
        else:
            r["is_new"] = False
    if mark_new:
        _save_json(tracking_path, seen_store)

    # ---- Real commute time (optional — needs TravelTime credentials) ----
    if settings.get("traveltime_app_id") and settings.get("traveltime_api_key") and home:
        for r in results:
            if r["distance_miles"] is None:
                continue
            job_coords = geocode_place(r["location_name"])
            if not job_coords:
                continue
            commute = travel_time.get_commute(
                home, job_coords, settings["traveltime_app_id"], settings["traveltime_api_key"]
            )
            if commute:
                r["commute_minutes"] = commute.get("minutes")
                r["commute_method"] = commute.get("method_summary")

    # ---- Sort ----
    if sort_by == "distance":
        results.sort(key=lambda r: (r["distance_miles"] is None, r["distance_miles"] or 0))
    elif sort_by == "salary":
        results.sort(key=lambda r: (not r["salary_listed"], -(r["maximum_salary"] or r["minimum_salary"] or 0)))
    elif sort_by == "date":
        results.sort(key=lambda r: (r["date_posted"] is None, r["date_posted"] or ""), reverse=True)
    else:
        results.sort(key=lambda r: (-r["fit_score"], r["distance_miles"] is None, r["distance_miles"] or 0))

    return results, site_status, new_count


NTFY_BASE_URL = "https://ntfy.sh"


def send_ntfy_notification(topic, title, message):
    """Sends a push notification via ntfy.sh (https://ntfy.sh) — a free,
    no-account service. Returns True on success, False on any failure
    (never raises), since a failed notification shouldn't stop the
    scheduled search itself from completing and writing its summary file.

    Honesty note: ntfy topics are public by name — anyone who knows or
    guesses the topic string can subscribe and see these notifications.
    There's no login involved. Use a long, hard-to-guess topic name if
    you want this to stay effectively private, not something obvious."""
    if not topic:
        return False
    try:
        resp = requests.post(
            f"{NTFY_BASE_URL}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "default"},
            timeout=10,
        )
        return resp.status_code == 200
    except requests.RequestException:
        return False


def build_link_sources(settings, keywords, location_override=None):
    location_for_links = (location_override or "").strip() or settings.get("home_postcode", "WD6")
    link_sources = []
    for src in settings.get("sources", []):
        url = src["url_template"].format(keywords=quote_plus(keywords), location=quote_plus(location_for_links))
        link_sources.append({"name": src["name"], "url": url})
    return link_sources
