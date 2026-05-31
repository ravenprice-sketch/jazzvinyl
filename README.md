# Jazz Vinyl Reissue Monitor + Pressings app

Two pieces that work together:

- **monitor.py** — runs on a schedule (GitHub Actions), checks the labels for new
  jazz reissues, emails you when something new appears, AND publishes a
  `data.json` describing the full current catalog (with an AI consensus blurb per
  release).
- **pressings.html** — a static web app that simply *reads* that `data.json` and
  displays it: cover grid, dates, specs, pre-order flags, favorites, "new since
  last visit", and the AI summary per record.

The app does no fetching or scraping itself anymore. All the heavy/fragile work
(feeds, the Rhino scrape, AI blurbs) happens server-side in the monitor, which is
where it's safe to hold an API key. The app is just a viewer.

## Labels tracked
Blue Note Tone Poet, Blue Note Classic Vinyl, Craft OJC, Verve Acoustic Sounds
(all via clean Shopify feeds), plus Rhino High Fidelity **jazz only** (scraped
from Acoustic Sounds; low volume, more fragile).

## Files
- `monitor.py`, `requirements.txt`
- `.github/workflows/monitor.yml`
- `pressings.html`  (the app)
- produced automatically: `data.json`, `blurbs.json`

---

## Setup

### 1. Repo + workflow (same as before)
Put `monitor.py`, `requirements.txt`, `pressings.html` in the repo root and
`monitor.yml` at `.github/workflows/monitor.yml`.

### 2. Secrets  (Settings → Secrets and variables → Actions)
- **Email** (one channel): `SMTP_HOST` (smtp.gmail.com), `SMTP_PORT` (465),
  `SMTP_USER`, `SMTP_PASS` (Gmail app password), `EMAIL_TO`.
- **AI blurbs (optional but that's the point now):** `ANTHROPIC_API_KEY`.
  Create one at console.anthropic.com → API keys. You paste it; it stays in
  Secrets. Without it, everything still works but releases simply have no blurb.
  Cost is a few cents per run, and blurbs are cached per title so each album is
  only paid for once.

### 3. Workflow permissions
Settings → Actions → General → Workflow permissions → **Read and write**.
(The job commits `data.json` back to the repo.)

### 4. Turn on GitHub Pages so the app can read data.json
Settings → Pages → Source: **Deploy from a branch** → Branch: `main`, folder
`/ (root)` → Save. After it builds, your files are served at:
`https://<your-username>.github.io/<repo>/`
So `data.json` will be at:
`https://<your-username>.github.io/<repo>/data.json`
and the app at:
`https://<your-username>.github.io/<repo>/pressings.html`

### 5. Point the app at your data
Open `pressings.html`, find the `DATA_URL` line near the top of the script, and
set it to your data.json URL from step 4. Commit. (Until you do, the app shows a
short "set DATA_URL" message instead of records.)

### 6. Run it
Actions → Jazz Vinyl Monitor → Run workflow. First run records a baseline (no
email), generates blurbs, and publishes `data.json`. Open the app (the Pages URL
for pressings.html) and the grid loads from that file.

## How "new" works
- The **email** alerts on titles newly appearing since the monitor's last run.
- The **app** badges titles new since *your* last visit (stored in the app).
  These are independent on purpose.

## Notes / honest limits
- The app is as fresh as the monitor's last run (daily), not live.
- AI blurbs are the model's synthesized reception summary, labelled "AI summary"
  — not a live scrape of any forum.
- Rhino is the one fragile source; if Acoustic Sounds restyles its pages the
  Rhino line may need the regexes in `fetch_rhino_jazz` updated. The other four
  are unaffected.
