# bridge — opportunities bot

Finds work experience, internships and volunteering for UK students in Years 11-13
and writes them into Supabase. Runs itself every 6 hours via GitHub Actions.

## Setup

Two repository secrets are required (Settings -> Secrets and variables -> Actions):

| Secret | Where to find it |
|---|---|
| `SUPABASE_URL` | Supabase -> Project Settings -> API Keys -> Project URL |
| `SUPABASE_SERVICE_KEY` | Supabase -> Project Settings -> API Keys -> secret key |

The secret key bypasses row-level security. It belongs in GitHub Secrets and
nowhere else — never in the frontend, never pasted into a chat.

## Running it

Actions tab -> scrape -> Run workflow. After the first manual run it goes on the
6-hourly schedule by itself.

## Layout

    run.py                      entrypoint - add scrapers to the SCRAPERS list
    scrapers/uptree.py          Uptree events + placements
    pipeline/sync.py            normalise, categorise, upsert, expire stale rows
    sql/rls.sql                 database setup (already run in Supabase)
    .github/workflows/scrape.yml  the 6-hourly schedule

## Notes

- A run returning fewer than 5 rows raises instead of syncing. Silent zero is how
  aggregators quietly empty themselves while the cron stays green.
- Nothing is ever deleted. Listings that vanish get `is_live = false` after 14 days.
- Set your real contact address in the `HEADERS` User-Agent in `scrapers/uptree.py`
  before running this at any volume.
