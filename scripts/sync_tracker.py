"""
RocketRush — Tracker Sync (Batch Actor, Cost-Efficient)
--------------------------------------------------------
Uses apimaestro/linkedin-batch-profile-posts-scraper:
  - ONE Apify run for ALL active clients together
  - postedLimit="month" so it only fetches this month's posts
  - postsPerProfile=5 hard cap per client
  - Total cost per sync: ~$0.35 (70 results × $5/1000)
  - Daily sync for a full month: ~$10.50 total

CREDENTIALS come from GitHub Actions secrets:
  GH_PAT       → your GitHub personal access token
  APIFY_TOKEN  → your Apify API token
"""

import json, os, sys, base64, urllib.request, urllib.error
from datetime import datetime, date
from pathlib import Path

GITHUB_TOKEN = os.environ.get("GH_PAT", "")
APIFY_TOKEN  = os.environ.get("APIFY_TOKEN", "")

GITHUB_OWNER   = "teamrocketrush-ui"
GITHUB_REPO    = "rr-tracker"
BATCH_ACTOR_ID = "apimaestro/linkedin-batch-profile-posts-scraper"
CLIENTS_PATH   = "data/clients.json"
TEMPLATE_PATH  = "dashboard/tracker_template.html"
DASHBOARD_PATH = "dashboard/tracker.html"

POSTS_PER_PROFILE = 5   # hard cap per client — controls Apify cost directly

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

def carry_forward_fields(client):
    """Get writer/engager/targets from the most recent month that has them."""
    months = client.get("months", {})
    mk_now = current_month_key()
    for mk in sorted(months.keys(), reverse=True):
        if mk >= mk_now:
            continue
        m = months[mk]
        if m.get("writer") or m.get("engager"):
            return {
                "writer":         m.get("writer"),
                "engager":        m.get("engager"),
                "postsTarget":    m.get("postsTarget"),
                "commentsTarget": m.get("commentsTarget"),
            }
    return {"writer": None, "engager": None,
            "postsTarget": None, "commentsTarget": None}

def ensure_current_month(client):
    """Guarantee a current-month record exists with writer/engager filled in."""
    mk = current_month_key()
    client.setdefault("months", {})
    if mk not in client["months"]:
        fields = carry_forward_fields(client)
        client["months"][mk] = {**fields, "posts": [], "comments": []}
    else:
        m = client["months"][mk]
        if not m.get("writer"):
            fields = carry_forward_fields(client)
            m.setdefault("writer",         fields["writer"])
            m.setdefault("engager",        fields["engager"])
            m.setdefault("postsTarget",    fields["postsTarget"])
            m.setdefault("commentsTarget", fields["commentsTarget"])
    return client["months"][mk]

def latest_post_date(client):
    """Newest full_date already stored for current month, or None."""
    posts = client.get("months", {}).get(current_month_key(), {}).get("posts", [])
    return posts[0]["full_date"] if posts else None

# ── APIFY BATCH CALL ──────────────────────────────────────────────
def call_apify_batch(active_clients, dry_run=False):
    """
    ONE single Apify run for all active clients.
    Returns dict: { linkedin_url: [post, post, ...] }
    """
    if dry_run:
        print(f"  [DRY RUN] Would call batch actor for {len(active_clients)} profiles")
        return {}

    try:
        from apify_client import ApifyClient
    except ImportError:
        print("ERROR: pip install apify-client"); sys.exit(1)

    # Build list of usernames from LinkedIn URLs
    usernames = []
    url_to_client = {}
    for c in active_clients:
        url = c.get("linkedinUrl", "")
        if not url:
            continue
        # Extract username from URL like https://www.linkedin.com/in/username/
        username = url.rstrip("/").split("/in/")[-1].split("/")[0]
        usernames.append(username)
        url_to_client[username] = c

    if not usernames:
        print("  No valid LinkedIn URLs found — skipping Apify call")
        return {}

    print(f"  → Batch actor: {len(usernames)} profiles, "
          f"{POSTS_PER_PROFILE} posts each, this month only")
    print(f"  → Estimated cost: ~${len(usernames) * POSTS_PER_PROFILE * 0.005:.2f}")

    ac = ApifyClient(APIFY_TOKEN)
    run = ac.actor(BATCH_ACTOR_ID).call(run_input={
        "usernames":       usernames,
        "postsPerProfile": POSTS_PER_PROFILE,
        "postedLimit":     "month",   # only posts from this calendar month
    })

    dataset_id = (run["defaultDatasetId"] if isinstance(run, dict)
                  else run.default_dataset_id)
    items = list(ac.dataset(dataset_id).iterate_items())
    print(f"  → Got {len(items)} total results from Apify")

    # Group by username/profile
    by_username = {}
    for item in items:
        # Batch actor returns authorUsername or similar — check common fields
        author = (item.get("authorUsername") or
                  item.get("profileUsername") or
                  item.get("username") or "")
        if not author:
            # fallback: extract from post URL or profile URL in item
            profile_url = item.get("profileUrl", "") or item.get("authorUrl", "")
            author = profile_url.rstrip("/").split("/in/")[-1].split("/")[0]
        if author:
            by_username.setdefault(author, []).append(item)

    return by_username, url_to_client

# ── PARSE POSTS ───────────────────────────────────────────────────
def parse_item(item):
    """Convert one raw Apify item into our dashboard post format."""
    if item.get("post_type") == "repost" or item.get("isRepost"):
        return None

    # Try multiple date field names the batch actor might use
    date_str = (item.get("postedAt") or
                item.get("posted_at", {}).get("date") if isinstance(item.get("posted_at"), dict) else None or
                item.get("publishedAt") or
                item.get("date") or "")

    if isinstance(date_str, dict):
        date_str = date_str.get("date", "")
    date_str = str(date_str)[:19]

    if not date_str:
        return None

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        except ValueError:
            return None

    # Only keep current month
    if dt.strftime("%Y-%m") != current_month_key():
        return None

    stats = item.get("stats", {}) or {}
    text  = (item.get("text") or item.get("content") or "").strip()
    hook  = text.split("\n")[0][:120] if text else "(no text)"

    return {
        "date":      f"{dt.strftime('%b')} {dt.day}",
        "full_date": dt.strftime("%Y-%m-%d"),
        "title":     hook,
        "likes":     stats.get("like", stats.get("total_reactions",
                     item.get("likes", item.get("reactions", 0)))),
        "comments":  stats.get("comments", item.get("comments", 0)),
        "url":       item.get("url", item.get("postUrl", "")),
        "post_type": "regular",
    }

def merge_posts(month_record, new_posts, cutoff_date):
    """Merge new posts, skip ones already stored (dedup by URL and date)."""
    cutoff = None
    if cutoff_date:
        try:
            cutoff = datetime.strptime(cutoff_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    existing_urls = {p.get("url") for p in month_record.get("posts", [])}
    added = 0
    for p in new_posts:
        if not p:
            continue
        # Skip if already stored
        if p["url"] and p["url"] in existing_urls:
            continue
        # Skip if not newer than what we already have
        if cutoff:
            try:
                pd = datetime.strptime(p["full_date"], "%Y-%m-%d").date()
                if pd <= cutoff:
                    continue
            except ValueError:
                pass
        month_record.setdefault("posts", []).append(p)
        if p["url"]:
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
        url = f"https://{GITHUB_OWNER}.github.io/{GITHUB_REPO}/dashboard/tracker.html"
        print(f"✅ Dashboard live → {url}")
    except Exception as e:
        print(f"⚠  Dashboard rebuild failed: {e}")

# ── MAIN ──────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--client",  default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=== RocketRush Tracker Sync ===")
    print(f"Date: {date.today()}  |  Month: {current_month_key()}")
    print(f"Actor: {BATCH_ACTOR_ID}")
    print(f"Posts per profile: {POSTS_PER_PROFILE}  |  Date filter: this month only")
    if args.dry_run:
        print("⚠  DRY RUN — no API calls, no data written")
    print()

    if not GITHUB_TOKEN:
        print("ERROR: GH_PAT env var not set"); sys.exit(1)
    if not APIFY_TOKEN and not args.dry_run:
        print("ERROR: APIFY_TOKEN env var not set"); sys.exit(1)

    # Fetch live clients.json
    clients_raw, clients_sha = gh_get(CLIENTS_PATH)
    clients_data = json.loads(clients_raw)

    all_clients = clients_data["clients"]
    active = [c for c in all_clients if c.get("status") == "active"]
    skipped = [c for c in all_clients if c.get("status") != "active"]

    if args.client:
        active = [c for c in active if c["id"] == args.client]
        if not active:
            print(f"Client '{args.client}' not found among active clients.")
            sys.exit(1)

    # Ensure every active client has a current-month record with writer/engager
    for c in all_clients:
        if c.get("status") in ("active", "paused"):
            ensure_current_month(c)

    print(f"Active clients: {len(active)}")
    if skipped:
        skipped_labels = ", ".join(f"{c['name']} [{c.get('status')}]" for c in skipped)
        print(f"Skipped: {skipped_labels}")
    print()

    if args.dry_run:
        print("Dry run — skipping Apify call.")
        for c in active:
            cutoff = latest_post_date(c)
            print(f"  {c['name']}: would fetch posts"
                  f"{f' after {cutoff}' if cutoff else ' from month start'}")
    else:
        # One batch Apify call for all clients
        result = call_apify_batch(active, dry_run=False)
        by_username, url_to_client = result

        total_added = 0
        print()
        for c in active:
            url = c.get("linkedinUrl", "")
            username = url.rstrip("/").split("/in/")[-1].split("/")[0]
            raw_items = by_username.get(username, [])
            cutoff = latest_post_date(c)
            month_record = c["months"][current_month_key()]
            new_posts = [parse_item(item) for item in raw_items]
            added = merge_posts(month_record, new_posts, cutoff)
            c["lastSyncedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            total_added += added
            icon = "✅" if added > 0 else "—"
            print(f"  {icon} {c['name']}: {added} new post(s) "
                  f"({len(raw_items)} returned by Apify)")

        print()
        print(f"Total new posts added: {total_added}")
        est_cost = len(active) * POSTS_PER_PROFILE * 0.005
        print(f"Estimated Apify cost:  ~${est_cost:.2f}")

        # Push updated clients.json
        print()
        print("Pushing clients.json...")
        gh_put(CLIENTS_PATH,
               json.dumps(clients_data, indent=2, ensure_ascii=False),
               f"Sync {current_month_key()} — {date.today()}", clients_sha)
        print("✅ clients.json pushed")

        # Rebuild and push dashboard
        print("Rebuilding dashboard...")
        rebuild_and_push_dashboard(clients_data)

if __name__ == "__main__":
    main()
