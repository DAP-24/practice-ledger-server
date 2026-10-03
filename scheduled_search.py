"""
Runs one named search profile — called by this package's GitHub Actions
workflows (see .github/workflows/), either automatically on a daily
schedule or manually via "Run workflow" on GitHub's website or mobile
app. This is the server package's own copy, independent from the
desktop app's version of this same file (which instead runs via Windows
Task Scheduler) — see this package's own README, not the desktop app's.

Usage:
    python scheduled_search.py "Audit"
    python scheduled_search.py "General Practice"

Each named profile (saved under settings.json's "search_profiles") gets
its own independent "have I seen this before" tracking, its own
summary-file naming, its own results page (see below), and its own
notification title — so the two never interfere with each other.

Running with no profile name at all reuses whatever's saved under
"last_search_params" instead, for parity with the desktop app's own
fallback behaviour — but every setup described in this package's README
uses a named profile, since that's what the two workflow files expect.

Writes a summary file to the summaries/ folder listing anything new since
the last run, and (if GitHub Pages is enabled per the README) updates a
simple read-only results page under docs/ that you can view from any
browser, including your phone, without needing to open a file directly.

Optional: if a "ntfy_topic" is set, also sends a push notification via
ntfy.sh whenever there's at least one genuinely new match, and (if
"notify_on_zero_results" is on, the default) a second, distinct "checked,
nothing new" push on days with nothing new too.
"""

import html
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import search_core as core

BASE_DIR = Path(__file__).resolve().parent
SUMMARIES_DIR = BASE_DIR / "summaries"
DOCS_DIR = BASE_DIR / "docs"


def generate_results_html(results, profile_name, generated_at):
    """A simple, read-only static HTML page listing results — for GitHub
    Pages, viewable from any browser including your phone, with no
    server or dashboard needed (GitHub just serves this file as-is). Not
    interactive — no live search, no changing filters here; see the
    README for that trade-off and how to change settings instead."""
    sorted_results = sorted(results, key=lambda r: -(r.get("fit_score") or 0))

    def esc(text):
        return html.escape(str(text)) if text is not None else ""

    rows = []
    for r in sorted_results:
        tags = []
        if r.get("is_new"):
            tags.append('<span class="tag new">NEW</span>')
        if r.get("ri_progression_offered"):
            tags.append('<span class="tag">RI training offered</span>')
        tier = r.get("partnership_timeline_tier")
        if tier == "fast":
            tags.append('<span class="tag new">Partner &lt;12mo</span>')
        elif tier == "medium":
            tags.append('<span class="tag">Partner 12-18mo</span>')
        elif tier == "slow":
            tags.append('<span class="tag warn">Partner 2yrs+</span>')

        if r.get("salary_listed"):
            min_s, max_s = r.get("minimum_salary"), r.get("maximum_salary")
            salary = f"£{min_s:,}" if min_s == max_s else f"£{min_s:,}–£{max_s:,}"
        else:
            salary = "Not listed"

        if r.get("remote_or_hybrid"):
            distance = "Remote/hybrid"
        elif r.get("distance_miles") is not None:
            distance = f"{r['distance_miles']} mi"
        else:
            distance = "Unknown"

        job_url = r.get("job_url") or "#"
        rows.append(f"""
        <tr>
          <td data-label="Role"><a href="{esc(job_url)}" target="_blank" rel="noopener">{esc(r.get('job_title'))}</a>
              <div class="tags">{' '.join(tags)}</div></td>
          <td data-label="Employer">{esc(r.get('employer_name') or '—')}</td>
          <td data-label="Location">{esc(r.get('location_name') or '—')}</td>
          <td data-label="Distance">{esc(distance)}</td>
          <td data-label="Salary">{esc(salary)}</td>
          <td data-label="Fit">{esc(r.get('fit_score', 0))}</td>
          <td data-label="Source">{esc(r.get('source'))}</td>
        </tr>""")

    table_body = "\n".join(rows) if rows else '<tr><td colspan="7">No matching roles right now.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Practice Ledger — {esc(profile_name)}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 0; padding: 16px; background: #faf8f3; color: #1f1b16; }}
  h1 {{ font-size: 20px; margin-bottom: 4px; }}
  .meta {{ color: #6b6258; font-size: 13px; margin-bottom: 16px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #e4ddd0; vertical-align: top; }}
  th {{ background: #efe9dd; position: sticky; top: 0; }}
  a {{ color: #8a6d3b; text-decoration: underline; }}
  .tags {{ margin-top: 4px; }}
  .tag {{ display: inline-block; font-size: 11px; border: 1px solid #c9bfa8; border-radius: 3px; padding: 1px 6px; margin-right: 4px; color: #6b6258; }}
  .tag.new {{ background: #2f5d3a; color: #fff; border-color: #2f5d3a; }}
  .tag.warn {{ color: #a33; border-color: #a33; }}
  @media (max-width: 600px) {{
    table, thead, tbody, th, td, tr {{ display: block; }}
    thead {{ display: none; }}
    tr {{ margin-bottom: 14px; border: 1px solid #e4ddd0; border-radius: 6px; padding: 8px; }}
    td {{ border: none; padding: 3px 0; }}
    td:before {{ content: attr(data-label); font-weight: 600; display: block; font-size: 11px; color: #6b6258; }}
  }}
</style>
</head>
<body>
  <h1>Practice Ledger — {esc(profile_name)}</h1>
  <p class="meta">Last updated: {esc(generated_at)} &middot; {len(sorted_results)} matching role(s) &middot;
     read-only (see the repository's README to change search settings)</p>
  <table>
    <thead><tr><th>Role</th><th>Employer</th><th>Location</th><th>Distance</th><th>Salary</th><th>Fit</th><th>Source</th></tr></thead>
    <tbody>{table_body}</tbody>
  </table>
</body>
</html>
"""


def format_summary(results, new_count, site_status, params, profile_name=None):
    lines = []
    now_str = datetime.now().strftime("%A %d %B %Y, %H:%M")
    label = f" ({profile_name})" if profile_name else ""
    lines.append(f"Practice Ledger{label} — scheduled check: {now_str}")
    lines.append(f"Search: \"{params.get('keywords', '')}\"")
    lines.append(f"Total matches: {len(results)}  |  New since last run: {new_count}")
    lines.append("")

    if new_count == 0:
        lines.append("No new matches since the last run.")
    else:
        lines.append("NEW MATCHES:")
        lines.append("-" * 60)
        for r in results:
            if not r.get("is_new"):
                continue
            salary = "Not listed" if not r["salary_listed"] else (
                f"£{r['minimum_salary']:,}" if r["minimum_salary"] == r["maximum_salary"]
                else f"£{r['minimum_salary']:,}–£{r['maximum_salary']:,}"
            )
            if r.get("is_day_rate"):
                salary += " (likely day rate, not annual)"
            commute = ""
            if r.get("commute_minutes"):
                commute = f" | commute ~{r['commute_minutes']} min"
                if r.get("commute_method"):
                    commute += f" via {r['commute_method']}"
            elif r.get("distance_miles") is not None:
                commute = f" | {r['distance_miles']} mi"

            fit = "Independent" if r["firm_classification"] == "independent_signal" else f"fit {r['fit_score']}"

            lines.append(f"* {r['job_title']}")
            lines.append(f"  {r.get('employer_name') or 'employer not disclosed'} — {r['location_name']}{commute}")
            lines.append(f"  {salary} | {fit} | source: {r['source']}")
            lines.append(f"  {r['job_url']}")
            lines.append("")

    lines.append("")
    lines.append("Source status this run:")
    for name, status in site_status.items():
        lines.append(f"  {name}: {status}")

    return "\n".join(lines)


def build_notification_message(results, new_count, profile_name=None):
    """A short, phone-notification-friendly summary — distinct from the
    full text-file summary, which is too long for a push notification."""
    label = f" ({profile_name})" if profile_name else ""
    title = f"Practice Ledger{label}: {new_count} new match{'es' if new_count != 1 else ''}"
    new_results = [r for r in results if r.get("is_new")]
    lines = []
    for r in new_results[:5]:
        salary = "salary not listed" if not r["salary_listed"] else (
            f"£{r['minimum_salary']:,}" if r["minimum_salary"] == r["maximum_salary"]
            else f"£{r['minimum_salary']:,}-£{r['maximum_salary']:,}"
        )
        employer = r.get("employer_name") or "employer not disclosed"
        lines.append(f"{r['job_title']} - {employer} ({salary})")
    if len(new_results) > 5:
        lines.append(f"...and {len(new_results) - 5} more")
    return title, "\n".join(lines)


def main():
    settings = core.load_settings()

    profile_name = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else None

    if profile_name:
        profiles = settings.get("search_profiles") or {}
        # Case-insensitive match, so a Task Scheduler argument that's
        # capitalised slightly differently from how it was saved in the
        # app still works, rather than failing on a technicality.
        matched = next((v for k, v in profiles.items() if k.lower() == profile_name.lower()), None)
        if matched is None:
            print(f"No saved search profile named '{profile_name}'.")
            if profiles:
                print("Profiles that do exist: " + ", ".join(profiles.keys()))
            else:
                print("No profiles have been saved yet — use \"Save as profile\" in the app first.")
            sys.exit(1)
        params = dict(matched)
    else:
        params = dict(settings.get("last_search_params") or {})
        if not params:
            print("No saved search filters found yet. Run at least one search in the")
            print("web app first (python app.py, then search in the browser) — this")
            print("script reuses whatever filters you last used there.")
            sys.exit(1)

    # Belt and braces: the scheduled run always measures from the saved
    # home postcode, never a one-off "search from" location.
    params.pop("search_from_override", None)

    # One-off radius override, only ever present when manually triggered
    # from GitHub's "Run workflow" button with a value typed in — see the
    # workflow file's workflow_dispatch inputs. A scheduled (automatic)
    # run never sets this, so it silently does nothing then, leaving the
    # profile's own saved radius_miles exactly as committed in settings.json.
    radius_override = os.environ.get("RADIUS_OVERRIDE_MILES", "").strip()
    if radius_override:
        try:
            params["radius_miles"] = int(radius_override)
        except ValueError:
            print(f"Ignoring invalid radius override {radius_override!r} (expected a whole number of miles) — using the profile's saved radius instead.")

    if not settings.get("reed_api_key"):
        print("No Reed API key saved. Add one in the app's Settings first.")
        sys.exit(1)

    tracking_path = core.seen_jobs_path_for_profile(profile_name)
    results, site_status, new_count = core.run_search(settings, params, tracking_path=tracking_path)

    core.append_history({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "keywords": params.get("keywords", ""),
        "count": len(results),
        "new_count": new_count,
        "scheduled": True,
        "profile": profile_name,
    })

    SUMMARIES_DIR.mkdir(exist_ok=True)
    file_prefix = f"summary_{core.PROFILE_NAME_SAFE_RE.sub('_', profile_name.strip().lower()).strip('_')}" if profile_name else "summary"
    filename = f"{file_prefix}_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.txt"
    summary_path = SUMMARIES_DIR / filename
    # Extremely unlikely with second-level precision, but never silently
    # overwrite a previous summary if two runs somehow land in the same second.
    counter = 2
    while summary_path.exists():
        filename = f"{file_prefix}_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_{counter}.txt"
        summary_path = SUMMARIES_DIR / filename
        counter += 1
    summary_path.write_text(format_summary(results, new_count, site_status, params, profile_name), encoding="utf-8")

    # Also update this profile's results page, if GitHub Pages is set up
    # (see the README) — overwrites the same file each run, so it always
    # shows the latest results rather than growing forever like summaries/.
    DOCS_DIR.mkdir(exist_ok=True)
    page_name = core.PROFILE_NAME_SAFE_RE.sub("-", profile_name.strip().lower()).strip("-") if profile_name else "results"
    page_path = DOCS_DIR / f"{page_name}.html"
    generated_at = datetime.now().strftime("%A %d %B %Y, %H:%M")
    page_path.write_text(generate_results_html(results, profile_name or "Search", generated_at), encoding="utf-8")

    print(f"Done. {new_count} new match(es) out of {len(results)} total.")
    print(f"Summary written to: {summary_path}")
    print(f"Results page written to: {page_path}")

    topic = settings.get("ntfy_topic")
    if topic and new_count > 0:
        notif_title, notif_body = build_notification_message(results, new_count, profile_name)
        sent = core.send_ntfy_notification(topic, notif_title, notif_body)
        print("Push notification sent." if sent else "Push notification failed to send (summary file was still written normally).")
    elif topic and settings.get("notify_on_zero_results", True):
        # Deliberately sent even with nothing new — this is the only
        # positive confirmation you get that the scheduled check actually
        # ran today, rather than it having silently failed to fire at
        # all (which otherwise looks identical to "ran, found nothing").
        now_str = datetime.now().strftime("%H:%M")
        label = f" ({profile_name})" if profile_name else ""
        sent = core.send_ntfy_notification(
            topic, f"Practice Ledger{label}: checked, nothing new",
            f"Ran at {now_str} — no new matches since last time. {len(results)} role(s) still match your filters overall.",
        )
        print("Push notification sent (no new matches)." if sent else "Push notification failed to send (summary file was still written normally).")


if __name__ == "__main__":
    main()
