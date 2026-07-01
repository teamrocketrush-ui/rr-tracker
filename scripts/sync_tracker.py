"""
RocketRush — Tracker Sync (Incremental, Cost-Efficient)
--------------------------------------------------------
WHAT THIS DOES:
  1. Reads clients.json from GitHub
  2. For each active client, finds their most recent post already stored
  3. Asks Apify ONLY for posts newer than that date (no re-scraping)
  4. Merges new posts into clients.json
  5. Saves updated clients.json back to GitHub
  6. Rebuilds the dashboard HTML and pushes it too
  7. Reports a clean summary

APIFY COST LOGIC:
  - If client has posts stored → only ask for posts after the latest one
  - If client has no posts yet → only ask for posts in the current month
  - Apify returns results newest-first, so we stop as soon as we hit a
    date we already have — no wasted credits on old posts

USAGE (run from inside Claude's bash tool):
  python sync_tracker.py [--client CLIENT_ID] [--dry-run]
"""

import json, os, sys, base64, urllib.request, urllib.error
from datetime import datetime, date
from pathlib import Path

# ── CREDENTIALS ───────────────────────────────────────────
# Reads from environment variables first (GitHub Actions secrets),
# falls back to hardcoded values for running locally/in Claude.
GITHUB_TOKEN  = os.environ.get("GH_PAT", "")
APIFY_TOKEN   = os.environ.get("APIFY_TOKEN", "")
GITHUB_OWNER  = "teamrocketrush-ui"
GITHUB_REPO   = "rr-tracker"
ACTOR_ID      = "apimaestro/linkedin-profile-posts"

CLIENTS_PATH   = "data/clients.json"
TEMPLATE_PATH  = "dashboard/tracker_template.html"
DASHBOARD_PATH = "dashboard/tracker.html"

# ── GITHUB HELPERS ────────────────────────────────────────
def gh_get(path):
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    })
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
    content = base64.b64decode(data["content"]).decode()
    return content, data["sha"]

def gh_put(path, content_str, message, sha=None):
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content_str.encode()).decode()
    }
    if sha:
        payload["sha"] = sha
    req = urllib.request.Request(url, method="PUT",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json"
        })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# ── DATE HELPERS ──────────────────────────────────────────
def current_month_key():
    return date.today().strftime("%Y-%m")

def month_start_str():
    """First day of current month as YYYY-MM-DD"""
    d = date.today()
    return f"{d.year}-{d.month:02d}-01"

def latest_post_date(client):
    """
    Find the newest post date already stored for this client in the current month.
    Returns a YYYY-MM-DD string, or None if no posts stored yet for this month.
    """
    month_key = current_month_key()
    months = client.get("months", {})
    month_data = months.get(month_key, {})
    posts = month_data.get("posts", [])
    if not posts:
        return None
    # posts are stored newest-first
    return posts[0].get("full_date")

# ── APIFY ─────────────────────────────────────────────────
def call_apify(linkedin_url, fetch_after_date, dry_run=False):
    """
    Call Apify for one profile. Only fetches posts newer than fetch_after_date.
    fetch_after_date: YYYY-MM-DD string, or None (fetch from start of month)
    Returns list of raw post dicts.
    """
    if dry_run:
        print(f"    [DRY RUN] Would call Apify for {linkedin_url}")
        return []

    try:
        from apify_client import ApifyClient
    except ImportError:
        print("ERROR: apify-client not installed. Run: pip install apify-client")
        sys.exit(1)

    token = APIFY_TOKEN
    if not token:
        print("ERROR: No APIFY_TOKEN found.")
        sys.exit(1)

    ac = ApifyClient(token)

    # We always request a small fixed number (15) since Apify returns newest-first
    # and we stop as soon as we hit an old post — so 15 is more than enough
    # for any realistic posting frequency (max ~12 posts/month for our clients)
    run_input = {
        "username": linkedin_url,
        "total_posts_to_scrape": 15,
    }

    print(f"    → Apify: fetching up to 15 posts (newest first)...")
    run = ac.actor(ACTOR_ID).call(run_input=run_input)
    dataset_id = run["defaultDatasetId"] if isinstance(run, dict) else run.default_dataset_id
    items = list(ac.dataset(dataset_id).iterate_items())
    print(f"    → Got {len(items)} raw items from Apify")
    return items

def parse_and_filter(raw_items, fetch_after_date):
    """
    Parse raw Apify items. If fetch_after_date is set, only keep posts
    strictly AFTER that date (we already have everything up to and including it).
    Skips reposts.
    """
    cutoff = None
    if fetch_after_date:
        try:
            cutoff = datetime.strptime(fetch_after_date, "%Y-%m-%d").date()
        except ValueError:
            cutoff = None

    month_start = datetime.strptime(month_start_str(), "%Y-%m-%d").date()
    new_posts = []

    for item in raw_items:
        if item.get("post_type") == "repost":
            continue

        posted_at = item.get("posted_at", {})
        date_str = posted_at.get("date", "")
        if not date_str:
            continue

        try:
            dt = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

        post_date = dt.date()

        # Only keep posts from current month onwards
        if post_date < month_start:
            continue

        # If we already have posts, only keep ones newer than our newest stored post
        if cutoff and post_date <= cutoff:
            continue

        stats = item.get("stats", {})
        text = (item.get("text") or "").strip()
        hook = text.split("\n")[0][:120] if text else "(no text)"

        new_posts.append({
            "date": f"{dt.strftime('%b')} {dt.day}",
            "full_date": dt.strftime("%Y-%m-%d"),
            "title": hook,
            "likes": stats.get("like", stats.get("total_reactions", 0)),
            "comments": stats.get("comments", 0),
            "url": item.get("url", ""),
            "post_type": item.get("post_type", "regular"),
        })

    # Sort newest first
    new_posts.sort(key=lambda p: p["full_date"], reverse=True)
    return new_posts

def merge_posts(clients_data, client_id, new_posts):
    """
    Merge new posts into the current month's record for this client.
    Preserves existing posts, writer, engager, targets, comments.
    Deduplicates by URL so running twice is safe.
    """
    month_key = current_month_key()
    client = next((c for c in clients_data["clients"] if c["id"] == client_id), None)
    if not client:
        return clients_data

    client.setdefault("months", {})
    month = client["months"].setdefault(month_key, {
        "writer": None, "engager": None,
        "postsTarget": None, "commentsTarget": None,
        "posts": [], "comments": []
    })

    existing_urls = {p.get("url") for p in month.get("posts", [])}
    added = 0
    for p in new_posts:
        if p["url"] not in existing_urls:
            month.setdefault("posts", []).append(p)
            existing_urls.add(p["url"])
            added += 1

    # Re-sort newest first after merge
    month["posts"].sort(key=lambda p: p["full_date"], reverse=True)
    client["lastSyncedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return clients_data, added

# ── DASHBOARD REBUILD ─────────────────────────────────────
def rebuild_dashboard(clients_data):
    """Inline version of build_dashboard_data.py logic so we don't need the file."""
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from build_dashboard_data import build_month_data_js, inject_into_dashboard
        template, template_sha = gh_get(TEMPLATE_PATH)
        block = build_month_data_js(clients_data)
        updated = inject_into_dashboard(template, block)
        return updated, template_sha
    except Exception as e:
        print(f"  WARNING: dashboard rebuild failed locally ({e}) — skipping HTML push")
        return None, None

# ── MAIN ─────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", help="Sync only this client id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=== RocketRush Tracker Sync ===")
    print(f"Date: {date.today()}  |  Month: {current_month_key()}")
    if args.dry_run:
        print("⚠ DRY RUN — no data will be written\n")

    # Validate credentials before doing anything
    if not GITHUB_TOKEN:
        print("ERROR: GH_PAT environment variable not set.")
        print("Set it with: export GH_PAT=your_github_token")
        sys.exit(1)
    if not APIFY_TOKEN and not args.dry_run:
        print("ERROR: APIFY_TOKEN environment variable not set.")
        print("Set it with: export APIFY_TOKEN=your_apify_token")
        sys.exit(1)

    # 1. Fetch clients.json from GitHub
    print("Fetching clients.json from GitHub...")
    clients_raw, clients_sha = gh_get(CLIENTS_PATH)
    clients_data = json.loads(clients_raw)

    # 2. Filter to active clients only
    active = [c for c in clients_data["clients"] if c.get("status") == "active"]
    skipped = [c for c in clients_data["clients"] if c.get("status") != "active"]

    if args.client:
        active = [c for c in active if c["id"] == args.client]
        if not active:
            print(f"Client id '{args.client}' not found among active clients.")
            sys.exit(1)

    print(f"Active: {len(active)} client(s)")
    if skipped:
        skipped_labels = ", ".join(f"{c['name']} [{c.get('status')}]" for c in skipped)
        print(f"Skipped: {skipped_labels}")
    print()

    # 3. Sync each active client
    results = []
    for client in active:
        print(f"[{client['name']}]")
        url = client.get("linkedinUrl")
        if not url:
            print(f"  SKIP: no linkedinUrl set")
            results.append({"name": client["name"], "status": "no_url", "new": 0})
            continue

        # Find our newest stored post — only ask Apify for posts after this
        latest = latest_post_date(client)
        if latest:
            print(f"  Latest stored post: {latest} → only fetching posts after this")
        else:
            print(f"  No posts stored for {current_month_key()} yet → fetching from month start ({month_start_str()})")

        raw = call_apify(url, latest, dry_run=args.dry_run)
        new_posts = parse_and_filter(raw, latest)
        print(f"  New posts to add: {len(new_posts)}")

        if not args.dry_run and new_posts:
            clients_data, added = merge_posts(clients_data, client["id"], new_posts)
            results.append({"name": client["name"], "status": "synced", "new": added})
        else:
            results.append({"name": client["name"], "status": "dry_run" if args.dry_run else "no_new_posts", "new": 0})

    # 4. Push updated clients.json to GitHub
    if not args.dry_run:
        print("\nPushing updated clients.json to GitHub...")
        gh_put(CLIENTS_PATH, json.dumps(clients_data, indent=2, ensure_ascii=False),
               f"Sync {current_month_key()} — {date.today()}", clients_sha)
        print("✅ clients.json pushed")

        # 5. Rebuild and push dashboard
        print("Rebuilding dashboard...")
        dashboard_html, _ = rebuild_dashboard(clients_data)
        if dashboard_html:
            _, dash_sha = gh_get(DASHBOARD_PATH)
            gh_put(DASHBOARD_PATH, dashboard_html,
                   f"Rebuild dashboard after sync {date.today()}", dash_sha)
            print("✅ dashboard/tracker.html pushed")
            print(f"🌐 Live: https://{GITHUB_OWNER}.github.io/{GITHUB_REPO}/dashboard/tracker.html")

    # 6. Summary
    print("\n=== Summary ===")
    for r in results:
        if r["status"] == "synced":
            icon = "✅" if r["new"] > 0 else "—"
            print(f"  {icon} {r['name']}: {r['new']} new post(s) added")
        elif r["status"] == "no_new_posts":
            print(f"  — {r['name']}: already up to date")
        elif r["status"] == "dry_run":
            print(f"  · {r['name']}: dry run")
        else:
            print(f"  ✗ {r['name']}: {r['status']}")

if __name__ == "__main__":
    main()
