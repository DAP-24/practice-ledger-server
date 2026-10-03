# Practice Ledger — Server Package

This is a **separate, independent version** of the Practice Ledger
scheduled search — built to run automatically in the cloud (via GitHub,
for free), so your daily check and phone notifications keep working even
when your laptop is off.

**This is not the same thing as the desktop "Practice Ledger" app.** That
app (the one with the browser interface you search and browse results
in) is untouched and works exactly as before — keep using it for that.
This package only replaces the *automatic daily check* piece, running it
somewhere that's always switched on instead of relying on your laptop and
Windows Task Scheduler.

**The trade-off, stated plainly:** there's no clicky web interface here.
Changing a search's settings later means editing a text file directly on
GitHub's website — not difficult, but different from ticking checkboxes.
See "Changing your search settings later" below for exactly how.

## What you'll end up with

- Two searches — "Audit" and "General Practice" — running automatically
  once a day, without your laptop needing to be on.
- The same phone push notifications you already have, working the same
  way.
- A button in GitHub's website *and* its phone app to run either search
  on demand, any time — this is the part that lets you trigger a check
  from your phone with your laptop off.

## One-time setup

### Step 1: Create a free GitHub account (skip if you already have one)

Go to **github.com** and sign up. It's free.

### Step 2: Create a new, **private** repository

1. Once signed in, click the **+** icon (top right) → **New repository**.
2. Name it something like `practice-ledger-server` (the name doesn't
   matter, but avoid spaces).
3. **Set it to Private.** This matters — it keeps your search results and
   configuration out of public view, even though your actual API keys
   never get stored here regardless (see Step 4).
4. Leave everything else as default, and click **Create repository**.

### Step 3: Upload these files

1. On your new (empty) repository's page, click **uploading an existing
   file** (or **Add file → Upload files**).
2. Drag in **every file and folder from this package**, including the
   hidden `.github` folder with the workflow files inside it. If your
   browser or computer hides folders starting with a dot, you may need to
   upload the `.github/workflows/audit-search.yml` and
   `.github/workflows/general-practice-search.yml` files individually via
   **Add file → Create new file**, typing that exact path (including the
   folders) as the filename — GitHub will create the folders for you.
3. Scroll down and click **Commit changes**.

Check afterward that your repository has this structure:
```
your-repo/
  .github/workflows/audit-search.yml
  .github/workflows/general-practice-search.yml
  scheduled_search.py
  search_core.py
  scrapers.py
  travel_time.py
  requirements.txt
  settings.json
```

### Step 4: Add your API keys as Secrets

Secrets are GitHub's secure, private storage for exactly this kind of
thing — your keys are never visible in the repository itself, never shown
in logs, and not even visible to you again once saved (you'd re-enter
them to change them, not view them).

1. In your repository, click **Settings** (the repository's own Settings
   tab, near the top — not your GitHub account settings).
2. In the left sidebar, click **Secrets and variables → Actions**.
3. Click **New repository secret** for each of the following (you'll do
   this up to five times — skip any you don't use):

| Secret name | Value |
|---|---|
| `REED_API_KEY` | Your Reed developer API key |
| `ADZUNA_APP_ID` | Your Adzuna Application ID |
| `ADZUNA_APP_KEY` | Your Adzuna Application Key |
| `JOOBLE_API_KEY` | Your Jooble API key |
| `NTFY_TOPIC` | Your ntfy.sh topic name |

The names must match **exactly** as shown (capital letters, underscores)
— these are what the workflow files are already set up to look for.

### Step 5: Test it manually

1. Click the **Actions** tab at the top of your repository.
2. You should see **"Audit search"** and **"General Practice search"** in
   the list on the left.
3. Click **"Audit search"**, then click the **"Run workflow"** button
   (top right of the list), then the green **Run workflow** button that
   appears.
4. Wait a minute or two, then refresh the page — you should see a run
   appear with a green tick if it worked. Click into it to see the actual
   log if you want the detail, or just check your phone for the
   notification.
5. Repeat for **"General Practice search"**.

If either shows a red cross instead, click into the run and open the
"Run the ... search" step to see the actual error message — it'll be the
same kind of message you'd see running the script locally (e.g. "No Reed
API key saved" means that Secret wasn't set correctly).

**That's it — once this works, both searches will now also run
automatically every day** (around 7am UK time, adjusting slightly for
daylight saving — see the comments inside the workflow files if you want
to change the time), with no further action needed from you.

## Running a check from your phone

1. Install the free **GitHub Mobile** app (search your phone's app
   store), and sign in with the same account.
2. Open your repository → **Actions**.
3. Tap **"Audit search"** or **"General Practice search"**.
3. Tap **Run workflow**.

That's the whole thing — no laptop involved.

## Changing your search settings later

Since there's no web interface here, changes mean editing `settings.json`
directly:

1. In your repository on GitHub's website, click on `settings.json`.
2. Click the **pencil icon** (Edit this file).
3. Make your change (see the field guide below).
4. Scroll down, click **Commit changes**.

That's it — the next run (scheduled or manual) picks up the change
automatically.

**Field guide** (these live inside `search_profiles` → `Audit` or
`General Practice`):

| Field | What it does | Example |
|---|---|---|
| `keywords` | The search terms | `"audit director"` |
| `radius_miles` | How far from home to search | `30` |
| `min_salary` / `max_salary` | Salary range, or `null` for no limit | `90000` |
| `include_remote_hybrid` | Skip the distance check for remote/hybrid roles | `true` / `false` |
| `require_title_match` | Title must say Director/Partner designate | `true` / `false` |
| `require_audit` | Must mention audit | `true` / `false` |
| `require_general_practice` | Must mention general practice | `true` / `false` |
| `require_ri` | Must mention RI | `true` / `false` |
| `max_age_days` | Ignore postings older than this many days, or `null` | `14` |
| `sort_by` | `"fit"`, `"distance"`, `"salary"`, or `"date"` | `"fit"` |

`home_postcode` (near the top of the file, not inside a profile) is where
both searches measure distance from.

**A note on JSON formatting:** every line except the last one in a group
needs a comma at the end, text values need double quotes around them
(`"audit"`, not `audit`), and `true`/`false`/`null` are typed exactly like
that, with no quotes. If you're not confident editing it directly, copy
the whole file's contents into a message here and ask for a specific
change — that's a perfectly good way to use this.

## Checking on results without a notification

Every run writes a summary file to a `summaries` folder in your
repository, same as the desktop app — click into that folder on GitHub's
website to read any past run's results directly, including the full
reasons.

## If something needs a real fix

Bugs found in the scraping/filtering logic itself will generally need to
be fixed in the original desktop app project first, then the corrected
files copied across here — since the two are kept deliberately separate,
a fix made only here won't reach the desktop app and vice versa. Just ask
for changes to be made to "the server package" specifically to keep the
two distinct.
