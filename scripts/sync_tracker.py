"""
RocketRush — Tracker Sync (Cost-Efficient Incremental)
-------------------------------------------------------
KEY BEHAVIOURS:
  1. Only asks Apify for posts FROM the start of the current month
     (or after the newest post already stored — whichever is later).
     This means Apify stops scraping as soon as it hits older content.

  2. Requests only 5 posts max per client — more than enough since
     clients post at most 12-15 times a month and we sync frequently.
     This is the main cost control.

  3. When creating a new month record, copies writer/engager/targets
     from the previous month automatically — no "Unassigned" in new tabs.

  4. Every active client appears in the current month tab, even with
     0 posts — so you always see the full picture.

  5. Idempotent — running twice adds 0 extra posts (dedup by URL).

USAGE (GitHub Actions or local):
  export GH_PAT=your_token
  export APIFY_TOKEN=your_token
  python scripts/sync_tracker.py
  python scripts/sync_tracker.py --client c3   # one client only
  python scripts/sync_tracker.py --dry-run     # no API calls
"""

import json, os, sys, base64, urllib.request, urllib.error
from datetime import datetime, date
from pathlib import Path

# ── CREDENTIALS (from env vars — set as GitHub Actions secrets) ──
GITHUB_TOKEN = os.environ.get("GH_PAT", "")
APIFY_TOKEN  = os.environ.get("APIFY_TOKEN", "")

GITHUB_OWNER   = "teamrocketrush-ui"
GITHUB_REPO    = "rr-tracker"
ACTOR_ID       = "apimaestro/linkedin-profile-posts"
CLIENTS_PATH   = "data/clients.json"
TEMPLATE_PATH  = "dashboard/tracker_template.html"
DASHBOARD_PATH = "dashboard/tracker.html"

# ── COST CONTROL: never request more than this many posts per client ──
MAX_POSTS_PER_CLIENT = 5

# ── GITHUB ────────────────────────────────────────────────────────
def gh_get(path):
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    })
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
    return base64.b64decode(data["content"]).decode(), data["sha"]

def gh_put(path, content_str, message, sha=None):
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    payload = {"message": message,
                "content": base64.b64encode(content_str.encode()).decode()}
    if sha:
        payload["sha"] = sha
    req = urllib.request.Request(url, method="PUT",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# ── DATE HELPERS ──────────────────────────────────────────────────
def current_month_key():
    return date.today().strftime("%Y-%m")

def month_start():
    d = date.today()
    return f"{d.year}-{d.month:02d}-01"

def latest_stored_post_date(client):
    """Newest post full_date already in the current month, or None."""
    posts = client.get("months", {}).get(current_month_key(), {}).get("posts", [])
    return posts[0]["full_date"] if posts else None

def prev_month_key():
    d = date.today()
    if d.month == 1:
        return f"{d.year-1}-12"
    return f"{d.year}-{d.month-1:02d}"

def carry_forward_fields(client):
    """
    Returns writer, engager, postsTarget, commentsTarget from the most
    recent month that has them set — so new month tabs are never blank.
    """
    months = client.get("months", {})
    # Walk months newest-first
    for mk in sorted(months.keys(), reverse=True):
        m = months[mk]
        if m.get("writer") or m.get("engager"):
            return {
                "writer":         m.get("writer"),
                "engager":        m.get("engager"),
                "postsTarget":    m.get("postsTarget"),
                "commentsTarget": m.get("commentsTarget"),
            }
    return {"writer": None, "engager": None, "postsTarget": None, "commentsTarget": None}

# ── APIFY ─────────────────────────────────────────────────────────
def call_apify(linkedin_url, dry_run=False):
    if dry_run:
        print(f"    [DRY RUN] Would call Apify for {linkedin_url}")
        return []
    try:
        from apify_client import ApifyClient
    except ImportError:
        print("ERROR: run: pip install apify-client")
        sys.exit(1)

    ac = ApifyClient(APIFY_TOKEN)

    # Request only MAX_POSTS_PER_CLIENT — Apify returns newest-first,
    # so it stops after N posts without scraping the whole history.
    run_input = {
        "username": linkedin_url,
        "total_posts_to_scrape": MAX_POSTS_PER_CLIENT,
    }
    print(f"    → Apify: requesting {MAX_POSTS_PER_CLIENT} posts (newest first)...")
    run = ac.actor(ACTOR_ID).call(run_input=run_input)
    dataset_id = (run["defaultDatasetId"] if isinstance(run, dict)
                  else run.default_dataset_id)
    items = list(ac.dataset(dataset_id).iterate_items())
    print(f"    → Got {len(items)} raw items")
    return items

def parse_and_filter(raw_items, cutoff_date):
    """
    Keep only:
      - Own posts (not reposts)
      - Posted in the current month
      - Strictly after cutoff_date (YYYY-MM-DD) if given
    """
    month_key   = current_month_key()
    cutoff      = None
    if cutoff_date:
        try:
            cutoff = datetime.strptime(cutoff_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    new_posts = []
    for item in raw_items:
        if item.get("post_type") == "repost":
            continue
        posted_at = item.get("posted_at", {})
        date_str  = (posted_at.get("date") or "")[:19]
        if not date_str:
            continue
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

        post_date = dt.date()
        # Must be in current month
        if dt.strftime("%Y-%m") != month_key:
            continue
        # Must be newer than what we already have
        if cutoff and post_date <= cutoff:
            continue

        stats = item.get("stats", {})
        text  = (item.get("text") or "").strip()
        hook  = text.split("\n")[0][:120] if text else "(no text)"

        new_posts.append({
            "date":      f"{dt.strftime('%b')} {dt.day}",
            "full_date": dt.strftime("%Y-%m-%d"),
            "title":     hook,
            "likes":     stats.get("like", stats.get("total_reactions", 0)),
            "comments":  stats.get("comments", 0),
            "url":       item.get("url", ""),
            "post_type": item.get("post_type", "regular"),
        })

    new_posts.sort(key=lambda p: p["full_date"], reverse=True)
    return new_posts

def ensure_current_month(client):
    """
    Guarantee a current-month record exists for this client,
    carrying forward writer/engager/targets from the previous month.
    Returns the (possibly newly created) month record.
    """
    mk = current_month_key()
    client.setdefault("months", {})
    if mk not in client["months"]:
        fields = carry_forward_fields(client)
        client["months"][mk] = {
            **fields,
            "posts":    [],
            "comments": [],
        }
    else:
        # If writer is still None, backfill it now
        m = client["months"][mk]
        if not m.get("writer"):
            fields = carry_forward_fields(client)
            m.setdefault("writer",         fields["writer"])
            m.setdefault("engager",        fields["engager"])
            m.setdefault("postsTarget",    fields["postsTarget"])
            m.setdefault("commentsTarget", fields["commentsTarget"])
    return client["months"][mk]

def merge_new_posts(month_record, new_posts):
    """Merge new posts, dedup by URL. Returns count added."""
    existing_urls = {p.get("url") for p in month_record.get("posts", [])}
    added = 0
    for p in new_posts:
        if p["url"] not in existing_urls:
            month_record.setdefault("posts", []).append(p)
            existing_urls.add(p["url"])
            added += 1
    month_record["posts"].sort(key=lambda p: p["full_date"], reverse=True)
    return added

# ── DASHBOARD REBUILD ─────────────────────────────────────────────
def rebuild_and_push_dashboard(clients_data):
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from build_dashboard_data import build_month_data_js, inject_into_dashboard
        template_raw, _ = gh_get(TEMPLATE_PATH)
        block   = build_month_data_js(clients_data)
        updated = inject_into_dashboard(template_raw, block)
        _, dash_sha = gh_get(DASHBOARD_PATH)
        gh_put(DASHBOARD_PATH, updated,
               f"Rebuild dashboard — sync {date.today()}", dash_sha)
        print(f"✅ Dashboard pushed → https://{GITHUB_OWNER}.github.io/{GITHUB_REPO}/dashboard/tracker.html")
    except Exception as e:
        print(f"⚠  Dashboard rebuild failed: {e}")

# ── MAIN ──────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--client",  default=None, help="Sync only this client id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=== RocketRush Tracker Sync ===")
    print(f"Date: {date.today()}  |  Month: {current_month_key()}")
    print(f"Max posts per client: {MAX_POSTS_PER_CLIENT}")
    if args.dry_run:
        print("⚠  DRY RUN — no data written")
    print()

    if not GITHUB_TOKEN:
        print("ERROR: GH_PAT env var not set"); sys.exit(1)
    if not APIFY_TOKEN and not args.dry_run:
        print("ERROR: APIFY_TOKEN env var not set"); sys.exit(1)

    # Fetch live clients.json
    clients_raw, clients_sha = gh_get(CLIENTS_PATH)
    clients_data = json.loads(clients_raw)

    active  = [c for c in clients_data["clients"] if c.get("status") == "active"]
    skipped = [c for c in clients_data["clients"] if c.get("status") != "active"]

    if args.client:
        active = [c for c in active if c["id"] == args.client]
        if not active:
            print(f"Client '{args.client}' not found among active clients.")
            sys.exit(1)

    print(f"Active clients to sync:  {len(active)}")
    if skipped:
        print(f"Skipped:  {', '.join(f\"{c['name']} [{c.get('status')}]\" for c in skipped)}")
    print()

    results = []

    for client in active:
        print(f"[{client['name']}]")
        url = client.get("linkedinUrl")
        if not url:
            print(f"  SKIP — no linkedinUrl")
            results.append({"name": client["name"], "status": "no_url", "added": 0})
            continue

        # Ensure current month record exists with writer/engager carried forward
        month_record = ensure_current_month(client)

        # Find cutoff — only fetch posts strictly after this date
        cutoff = latest_stored_post_date(client)
        if cutoff:
            print(f"  Latest stored post: {cutoff} → only fetching newer posts")
        else:
            print(f"  No July posts yet → fetching posts from {month_start()} onward")

        raw      = call_apify(url, dry_run=args.dry_run)
        new_posts = parse_and_filter(raw, cutoff)
        print(f"  New posts to add: {len(new_posts)}")

        if not args.dry_run:
            added = merge_new_posts(month_record, new_posts)
            client["lastSyncedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            results.append({"name": client["name"], "status": "synced", "added": added})
        else:
            results.append({"name": client["name"], "status": "dry_run", "added": 0})

    # Push updated clients.json
    if not args.dry_run:
        print("\nPushing clients.json to GitHub...")
        gh_put(CLIENTS_PATH,
               json.dumps(clients_data, indent=2, ensure_ascii=False),
               f"Sync {current_month_key()} — {date.today()}",
               clients_sha)
        print("✅ clients.json pushed")

        print("Rebuilding dashboard...")
        rebuild_and_push_dashboard(clients_data)

    # Summary
    print("\n=== Summary ===")
    total_added = 0
    for r in results:
        if r["status"] == "synced":
            icon = "✅" if r["added"] > 0 else "—"
            print(f"  {icon} {r['name']}: {r['added']} new post(s)")
            total_added += r["added"]
        elif r["status"] == "dry_run":
            print(f"  · {r['name']}: dry run")
        else:
            print(f"  ✗ {r['name']}: {r['status']}")
    if not args.dry_run:
        print(f"\nTotal new posts added: {total_added}")
        print(f"Apify results used:    ~{len(active) * MAX_POSTS_PER_CLIENT} max "
              f"({MAX_POSTS_PER_CLIENT} per client × {len(active)} clients)")

if __name__ == "__main__":
    main()
