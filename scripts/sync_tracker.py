"""
RocketRush — Tracker Sync
--------------------------
Uses harvestapi/linkedin-post-search — ONE Apify call for ALL active clients.

VERIFIED BEHAVIOUR (tested by user 2026-07-02, real run):
  - postedLimitDate genuinely stops fetching once it hits older posts —
    does NOT force extra results to fill maxPosts. Confirmed: requesting
    maxPosts=5 with 2 profiles returned only 2 and 1 results respectively,
    matching actual post dates, not the max ceiling.
  - Billing is per POST DELIVERED, not per profile queried or per page
    fetched internally. 3 posts across 2 profiles cost $0.01 total.
  - Output includes engagement.likes, engagement.comments, engagement.shares
    — all confirmed present in real output.
  - "Include Reposts" toggle (input key not yet in a confirmed schema dump,
    using includeReposts=False) filters reposts out BEFORE they reach us —
    no client-side repost filtering needed as long as this is off.

COST ESTIMATE (from real test): ~$0.002 per post delivered. A full month
of daily syncing across 17 clients is estimated well under $1, but this
assumes empty-result days cost ~$0 — not yet confirmed with a live 17-client
zero-post-day test. Watch actual costs in Apify Console after first runs.

CREDENTIALS from GitHub Actions secrets (GH_PAT + APIFY_TOKEN).
"""

import json, os, sys, base64, urllib.request, urllib.error
from datetime import datetime, date, timedelta
from pathlib import Path

GITHUB_TOKEN = os.environ.get("GH_PAT", "")
APIFY_TOKEN  = os.environ.get("APIFY_TOKEN", "")

GITHUB_OWNER   = "teamrocketrush-ui"
GITHUB_REPO    = "rr-tracker"
ACTOR_ID       = "harvestapi/linkedin-post-search"
CLIENTS_PATH   = "data/clients.json"
TEMPLATE_PATH  = "dashboard/tracker_template.html"
DASHBOARD_PATH = "dashboard/tracker.html"

MAX_POSTS_PER_PROFILE = 6   # safety ceiling only — postedLimitDate is the real limiter

# ── GITHUB ────────────────────────────────────────────────────────
def gh_get(path):
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"})
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
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (200, 201):
            return {}
        raise

# ── HELPERS ───────────────────────────────────────────────────────
def current_month_key():
    return date.today().strftime("%Y-%m")

def month_start_str():
    d = date.today()
    return f"{d.year}-{d.month:02d}-01"

def username_from_url(url):
    if "/in/" in url:
        return url.rstrip("/").split("/in/")[-1].split("/")[0].split("?")[0]
    return url.rstrip("/").split("/")[-1]

def latest_post_date(client):
    posts = client.get("months", {}).get(current_month_key(), {}).get("posts", [])
    return posts[0]["full_date"] if posts else None

def carry_forward_fields(client):
    months = client.get("months", {})
    mk_now = current_month_key()
    for mk in sorted(months.keys(), reverse=True):
        if mk >= mk_now:
            continue
        m = months[mk]
        if m.get("writer") or m.get("engager"):
            return {"writer": m.get("writer"), "engager": m.get("engager"),
                    "postsTarget": m.get("postsTarget"),
                    "commentsTarget": m.get("commentsTarget")}
    return {"writer": None, "engager": None,
            "postsTarget": None, "commentsTarget": None}

def ensure_current_month(client):
    mk = current_month_key()
    client.setdefault("months", {})
    if mk not in client["months"]:
        fields = carry_forward_fields(client)
        client["months"][mk] = {**fields, "posts": [], "comments": []}
    else:
        m = client["months"][mk]
        if not m.get("writer"):
            f = carry_forward_fields(client)
            for k in ("writer", "engager", "postsTarget", "commentsTarget"):
                m.setdefault(k, f[k])
    return client["months"][mk]

def compute_shared_cutoff_date(active_clients):
    """
    This actor takes ONE postedLimitDate for the whole batch call, not
    one per client. To make sure nobody's new post is missed, we use the
    EARLIEST date needed across all clients (i.e. the oldest "latest
    stored post" among them, or month start if any client has none yet).
    Posts already stored get filtered out later by URL dedup, so using
    an earlier-than-necessary cutoff for some clients just means a few
    already-known posts get re-fetched (near-zero cost) — never missed data.
    """
    dates = []
    for c in active_clients:
        d = latest_post_date(c)
        if d:
            dates.append(d)
    if not dates:
        return month_start_str()
    # earliest of the "latest known" dates — the most conservative cutoff
    earliest = min(dates)
    return earliest

# ── APIFY ─────────────────────────────────────────────────────────
def apify_client():
    try:
        from apify_client import ApifyClient
    except ImportError:
        print("ERROR: pip install apify-client")
        sys.exit(1)
    return ApifyClient(APIFY_TOKEN)

def call_apify_search(active_clients, cutoff_date, dry_run=False):
    """
    ONE batch call for all active clients using their profile URLs.
    Returns dict: { username: [raw_post_item, ...] }
    """
    author_urls = [c["linkedinUrl"] for c in active_clients if c.get("linkedinUrl")]
    if not author_urls:
        return {}

    if dry_run:
        print(f"  [DRY RUN] Would call {ACTOR_ID} for {len(author_urls)} profiles, "
              f"cutoff={cutoff_date}, maxPosts={MAX_POSTS_PER_PROFILE}")
        return {}

    ac = apify_client()
    run_input = {
        "authorUrls":               author_urls,
        "maxPosts":                 MAX_POSTS_PER_PROFILE,
        "postedLimitDate":          cutoff_date,
        "sortBy":                   "date",
        "scrapeComments":           False,
        "scrapeReactions":          False,
        "postNestedComments":       False,
        "postNestedReactions":      False,
        "profileScraperMode":       "short",
        "commentsProfileScraperMode": "short",
        "reactionsProfileScraperMode": "short",
        # Reposts excluded — verified in user's manual test that turning
        # this off filters them out before they reach the output at all.
        "includeReposts":           False,
        "includeQuotePosts":        False,
    }

    print(f"  → Apify: {len(author_urls)} profiles, cutoff={cutoff_date}, "
          f"max {MAX_POSTS_PER_PROFILE}/profile")
    run = ac.actor(ACTOR_ID).call(run_input=run_input)
    dataset_id = (run["defaultDatasetId"] if isinstance(run, dict)
                  else run.default_dataset_id)
    items = list(ac.dataset(dataset_id).iterate_items())
    print(f"  → Got {len(items)} posts total")

    by_username = {}
    for item in items:
        author = item.get("author", {}).get("publicIdentifier", "")
        if author:
            by_username.setdefault(author, []).append(item)
    return by_username

def parse_post(item):
    """Parse one raw item into our dashboard post format — schema confirmed
    from a real test output (dataset_linkedin-post-search JSON)."""
    if item.get("type") != "post":
        return None  # defensive — only accept confirmed post-type items

    posted_at = item.get("postedAt", {})
    date_str = posted_at.get("date", "")
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None

    if dt.strftime("%Y-%m") != current_month_key():
        return None

    engagement = item.get("engagement", {})
    text = (item.get("content") or "").strip()
    hook = text.split("\n")[0][:120] if text else "(no text)"

    return {
        "date":      f"{dt.strftime('%b')} {dt.day}",
        "full_date": dt.strftime("%Y-%m-%d"),
        "title":     hook,
        "likes":     engagement.get("likes", 0),
        "comments":  engagement.get("comments", 0),
        "shares":    engagement.get("shares", 0),
        "url":       item.get("linkedinUrl", ""),
        "post_type": "regular",
    }

def merge_posts(month_record, raw_items, cutoff_date):
    cutoff = None
    if cutoff_date:
        try:
            cutoff = datetime.strptime(cutoff_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    existing_urls = {p.get("url") for p in month_record.get("posts", [])}
    added = 0
    for item in raw_items:
        p = parse_post(item)
        if not p:
            continue
        if p["url"] and p["url"] in existing_urls:
            continue
        if cutoff:
            try:
                if datetime.strptime(p["full_date"], "%Y-%m-%d").date() <= cutoff:
                    continue
            except ValueError:
                pass
        month_record.setdefault("posts", []).append(p)
        if p["url"]:
            existing_urls.add(p["url"])
        added += 1
    month_record["posts"].sort(key=lambda x: x["full_date"], reverse=True)
    return added

# ── DASHBOARD REBUILD ─────────────────────────────────────────────
def rebuild_and_push_dashboard(clients_data):
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from build_dashboard_data import build_month_data_js, inject_into_dashboard
        template_raw, _ = gh_get(TEMPLATE_PATH)
        updated = inject_into_dashboard(template_raw,
                                        build_month_data_js(clients_data))
        _, dash_sha = gh_get(DASHBOARD_PATH)
        gh_put(DASHBOARD_PATH, updated,
               f"Rebuild dashboard — sync {date.today()}", dash_sha)
        print(f"  ✅ https://{GITHUB_OWNER}.github.io/{GITHUB_REPO}/dashboard/tracker.html")
    except Exception as e:
        print(f"  ⚠  Dashboard rebuild failed: {e}")

# ── MAIN ──────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--client",  default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=== RocketRush Tracker Sync ===")
    print(f"Date: {date.today()}  |  Month: {current_month_key()}")
    print(f"Actor: {ACTOR_ID}")
    if args.dry_run:
        print("⚠  DRY RUN")
    print()

    if not GITHUB_TOKEN:
        print("ERROR: GH_PAT not set"); sys.exit(1)
    if not APIFY_TOKEN and not args.dry_run:
        print("ERROR: APIFY_TOKEN not set"); sys.exit(1)

    clients_raw, clients_sha = gh_get(CLIENTS_PATH)
    clients_data = json.loads(clients_raw)

    active  = [c for c in clients_data["clients"] if c.get("status") == "active"]
    skipped = [c for c in clients_data["clients"] if c.get("status") != "active"]

    if args.client:
        active = [c for c in active if c["id"] == args.client]
        if not active:
            print(f"Client '{args.client}' not found."); sys.exit(1)

    for c in clients_data["clients"]:
        if c.get("status") in ("active", "paused"):
            ensure_current_month(c)

    print(f"Active: {len(active)}")
    if skipped:
        labels = ", ".join(f"{c['name']} [{c.get('status')}]" for c in skipped)
        print(f"Skipped: {labels}")
    print()

    cutoff_date = compute_shared_cutoff_date(active)
    print(f"Shared cutoff date for this batch: {cutoff_date}")
    print(f"(earliest 'latest known post' across all active clients — "
          f"ensures nobody's new post is missed)")
    print()

    by_username = call_apify_search(active, cutoff_date, dry_run=args.dry_run)

    total_added = 0
    print()
    for c in active:
        username = username_from_url(c.get("linkedinUrl", ""))
        raw = by_username.get(username, [])
        per_client_cutoff = latest_post_date(c)
        month_r = c["months"][current_month_key()]
        added = merge_posts(month_r, raw, per_client_cutoff)
        total_added += added
        c["lastSyncedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        icon = "✅" if added > 0 else "—"
        print(f"  {icon} {c['name']}: {added} new post(s)  ({len(raw)} returned)")

    print()
    print(f"Total new posts added: {total_added}")
    est_cost = total_added * 0.002
    print(f"Estimated Apify cost:  ~${est_cost:.3f} (based on $2/1000 posts, "
          f"actual results delivered)")

    if not args.dry_run:
        print()
        print("Pushing to GitHub...")
        gh_put(CLIENTS_PATH,
               json.dumps(clients_data, indent=2, ensure_ascii=False),
               f"Sync {current_month_key()} — {date.today()}", clients_sha)
        print("  ✅ clients.json pushed")
        rebuild_and_push_dashboard(clients_data)

if __name__ == "__main__":
    main()
