# RocketRush Tracker — Repo Structure

This is the intended layout inside the `rr-agency-data` GitHub repo (or a new
dedicated repo, e.g. `rr-tracker`, if you'd rather keep it separate from the
existing analytics pipeline repo).

```
rr-tracker/
├── data/
│   └── clients.json          ← source of truth, read/written by every script
├── scripts/
│   ├── sync_tracker.py       ← orchestrator: filters active clients, calls Apify, merges
│   ├── parse_apify_output.py ← reshapes raw Apify JSON into clients.json format
│   └── build_dashboard_data.py ← injects clients.json into the dashboard template
├── dashboard/
│   ├── tracker_template.html ← the dashboard with placeholder clientData
│   └── tracker.html          ← GENERATED — the live published dashboard (GitHub Pages serves this)
├── docs/
│   └── CLAUDE_PROJECT_INSTRUCTIONS.md ← system prompt for the Claude Project
└── README.md
```

## Why this layout

- `data/clients.json` is the only file that needs to persist meaningfully between
  syncs. Everything else is either a script (static) or generated output
  (`dashboard/tracker.html`, rebuilt fresh every sync).
- Keeping `tracker_template.html` separate from the generated `tracker.html` means
  `build_dashboard_data.py` always has a clean source to inject into — it never
  risks injecting into an already-injected file and corrupting the regex match.
- GitHub Pages should be configured to serve from `dashboard/tracker.html` (or the
  `dashboard/` folder as the Pages root), same pattern as your existing
  `rr-agency-data` proposal pages.

## Suggested .gitignore

```
__pycache__/
*.pyc
.env
apify_test_output.json
```

(Keep `clients.json` tracked — it's the actual data, not a secret. The Apify
token itself should live in GitHub Actions secrets if you automate the trigger,
never committed to the repo.)

## Typical workflow once live

1. Manager triggers sync (via Claude Project chat, or eventually a GitHub Action
   if you want a button instead of a chat message)
2. `sync_tracker.py` runs, updates `data/clients.json`
3. `build_dashboard_data.py` runs, regenerates `dashboard/tracker.html` from the
   template + updated data
4. Both files get committed and pushed
5. GitHub Pages auto-publishes the updated `tracker.html` within a minute or two
6. Manager refreshes the dashboard URL and sees current numbers

## First-time setup checklist

- [ ] Create the repo (or a `tracker/` subfolder in `rr-agency-data`)
- [ ] Push the `scripts/`, `dashboard/`, `docs/` folders as built here
- [ ] Seed `data/clients.json` with real clients (from the filled Excel template)
- [ ] Set `APIFY_TOKEN` as a GitHub Actions secret (only if automating the trigger
      beyond manual chat-initiated runs)
- [ ] Enable GitHub Pages, point it at the `dashboard/` folder
- [ ] Paste `CLAUDE_PROJECT_INSTRUCTIONS.md` into a new Claude Project's custom
      instructions, and connect that Project to this repo
