"""
Run this on a schedule (e.g. via Windows Task Scheduler, once a day) to
check for new matches automatically, without opening the app.

Two ways to run it:

1. No argument — reuses whatever filters you last ran a search with in
   the web UI (stored in settings.json under "last_search_params"). This
   is the original, simplest behaviour: set your filters up in the app,
   run one search there, and this script keeps using the same ones.

       python scheduled_search.py

2. With a profile name — reuses a named, SAVED search configuration
   instead (stored under settings.json's "search_profiles", saved from
   the app via the "Save as profile" control). Use this to run more than
   one distinct search daily without them overwriting each other — e.g.
   one profile with "Audit" ticked, another with "General practice"
   ticked, so a role that only matches one of them is never missed by
   running just a single combined search.

       python scheduled_search.py "Audit"
       python scheduled_search.py "General Practice"

   Each named profile gets its own independent "have I seen this before"
   tracking, its own summary-file naming, and its own notification
   title — so the two runs never interfere with each other, and you can
   always tell which one found what.

Writes a summary file to the summaries/ folder listing anything new since
the last run. If nothing's new, it still writes a short file saying so,
so you can tell the script actually ran.

Optional: if a "ntfy_topic" is set in Settings, also sends a push
notification to your phone via ntfy.sh (free, no account needed) whenever
there's at least one genuinely new match. If "notify_on_zero_results" is
also on (the default), a second, distinct "checked, nothing new" push is
sent on days with nothing new too — see the README for both.

Setting this up in Windows Task Scheduler, for a single (unnamed) daily
search — the easy way:
    1. Open Task Scheduler (search for it in the Start menu)
    2. Create Basic Task -> name it "Practice Ledger daily check"
    3. Trigger: Daily, pick a time (e.g. 7:00 AM)
    4. Action: "Start a program"
       - Program/script: the full path to "Run Scheduled Search.bat" in
         this same folder, e.g.
         C:\\Users\\you\\Desktop\\jobsearch\\Run Scheduled Search.bat
       - Leave "Add arguments" and "Start in" blank — the .bat file
         handles finding its own folder itself, so there's nothing else
         to configure.
    5. Finish. It'll now run automatically at that time each day, and
       write to scheduled_search_log.txt in this folder if you ever want
       to check it actually ran (Task Scheduler runs it invisibly).

For running TWO NAMED PROFILES daily (e.g. "Audit" and "General
Practice"), see the README section "Running more than one daily search"
— it uses two ready-made launcher files instead, one per profile, so
Task Scheduler still only ever needs one field filled in per task.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import search_core as core

BASE_DIR = Path(__file__).resolve().parent
SUMMARIES_DIR = BASE_DIR / "summaries"


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

    print(f"Done. {new_count} new match(es) out of {len(results)} total.")
    print(f"Summary written to: {summary_path}")

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
