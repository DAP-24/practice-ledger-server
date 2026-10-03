"""
Real commute time via the TravelTime API (traveltime.com), instead of the
straight-line mileage the app falls back to otherwise.

IMPORTANT — read before relying on this:
This requires a free TravelTime account (traveltime.com) and its two
credentials (Application ID + API Key), entered in Settings. Without
them, the app just uses straight-line distance as before — nothing
breaks, you just don't get commute times.

The exact response schema for the /v4/routes endpoint (which mode-by-mode
legs look like) wasn't verified against a live response while building
this — only the documented parameters were. Parsing is defensive: if the
response doesn't match what's expected, this returns None and the app
silently falls back to distance-only for that listing, rather than
erroring. If commute times aren't appearing, check a raw response from
the API (see README) and adjust parse_route_response() below.

TravelTime has no timetable data outside a ~2-week window, so the
departure time used here is always computed fresh (next weekday morning)
rather than hardcoded to any fixed date.
"""

from datetime import datetime, timedelta, timezone

import requests

ROUTES_URL = "https://api.traveltimeapp.com/v4/routes"


def next_weekday_morning(hour=9):
    """Returns an ISO8601 timestamp for the next weekday at the given hour
    — always within TravelTime's ±2-week data window, and a reasonable
    proxy for 'normal commute time'."""
    now = datetime.now(timezone.utc)
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # Sat=5, Sun=6
        candidate += timedelta(days=1)
    return candidate.isoformat()


def parse_route_response(data):
    """Defensive parsing — TravelTime's routes response nests a 'parts'
    list per result with a 'mode' per part. If the shape doesn't match,
    return None rather than guessing."""
    try:
        result = data["results"][0]
        if result.get("unreachable") or not result.get("locations"):
            return None
        properties = result["locations"][0]["properties"][0]
        travel_time_seconds = properties.get("travel_time")
        if travel_time_seconds is None:
            return None
        minutes = round(travel_time_seconds / 60)

        modes = []
        route_parts = properties.get("route", {}).get("parts", [])
        for part in route_parts:
            mode = part.get("mode")
            if mode and (not modes or modes[-1] != mode):
                modes.append(mode)
        method_summary = " → ".join(m.replace("_", " ").title() for m in modes) if modes else None

        return {"minutes": minutes, "method_summary": method_summary}
    except (KeyError, IndexError, TypeError):
        return None


def get_commute(home_coords, job_coords, app_id, api_key, timeout=10):
    """Returns {'minutes': int, 'method_summary': str or None} or None on
    any failure (missing credentials, API error, unparseable response,
    location too far from public transport, etc.)."""
    if not (app_id and api_key and home_coords and job_coords):
        return None

    params = {
        "type": "public_transport",
        "origin_lat": home_coords["lat"],
        "origin_lng": home_coords["lon"],
        "destination_lat": job_coords["lat"],
        "destination_lng": job_coords["lon"],
        "arrival_time": next_weekday_morning(hour=9),
    }
    headers = {"X-Application-Id": app_id, "X-Api-Key": api_key}

    try:
        resp = requests.get(ROUTES_URL, params=params, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return None
        return parse_route_response(resp.json())
    except requests.RequestException:
        return None
