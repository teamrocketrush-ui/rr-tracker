"""
RocketRush — Tracker Sync
--------------------------
Two things per sync:
  1. POSTS  — batch actor for all active clients, this month only, 5 posts max per profile
  2. COMMENTS — per-client actor for outgoing comments (comments the client made on others' posts)

Cost per daily sync (14 clients):
  Posts:    ~70 results  × $5/1000 = $0.35
  Comments: ~70 results  × $5/1000 = $0.35
  Total:    ~$0.70/day   → ~$21/month (well within $5 budget at 2-day intervals)

Credentials come from GitHub Actions secrets (GH_PAT + APIFY_TOKEN).
"""

import json, os, sys, base64, urllib.request, urllib.error
from datetime import datetime, date
from pathlib import Path

GITHUB_TOKEN = os.environ.get("GH_PAT", "")
APIFY_TOKEN  = os.environ.get("APIFY_TOKEN", "")

GITHUB_OWNER        = "teamrocketrush-ui"
GITHUB_REPO         = "rr-tracker"
BATCH_POSTS_ACTOR   = "apimaestro/linkedin-batch-profile-posts-scraper"
COMMENTS_ACTOR      = "apimaestro/linkedin-profile-comments"
CLIENTS_PATH        = "data/clients.json"
TEMPLATE_PATH       = "dashboard/tracker_template.html"
DASHBOARD_PATH      = "dashboard/tracker.html"

POSTS_PER_PROFILE    = 5
COMMENTS_PER_PROFILE = 5

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

def username_from_url(url):
    return url.rstrip("/").split("/in/")[-1].split("/")[0]

def latest_post_date(client):
    posts = client.get("months", {}).get(current_month_key(), {}).get("posts", [])
    return posts[0]["full_date"] if posts else None

def latest_comment_date(client):
    comments = client.get("months", {}).get(current_month_key(), {}).get("comments", [])
    return comments[0]["full_date"] if comments else None

def carry_forward_fields(client):
    months  = client.get("months", {})
    mk_now  = current_month_key()
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

# ── APIFY CLIENT ──────────────────────────────────────────────────
def apify_client():
    try:
        from apify_client import ApifyClient
    except ImportError:
        print("ERROR: pip install apify-client")
        sys.exit(1)
    return ApifyClient(APIFY_TOKEN)

def get_dataset_items(ac, run):
    dataset_id = (run["defaultDatasetId"] if isinstance(run, dict)
                  else run.default_dataset_id)
    return list(ac.dataset(dataset_id).iterate_items())

# ── POSTS SYNC ────────────────────────────────────────────────────
def sync_posts(active_clients, dry_run=False):
    """
    One batch Apify call for ALL active clients.
    Returns dict: { username: [parsed_post, ...] }
    """
    if dry_run:
        print("  [DRY RUN] Would call batch posts actor")
        return {}

    usernames = [username_from_url(c.get("linkedinUrl", ""))
                 for c in active_clients if c.get("linkedinUrl")]
    if not usernames:
        return {}

    print(f"  → Batch posts: {len(usernames)} profiles × {POSTS_PER_PROFILE} posts")
    ac = apify_client()
    run = ac.actor(BATCH_POSTS_ACTOR).call(run_input={
        "usernames":       usernames,
        "postsPerProfile": POSTS_PER_PROFILE,
        "postedLimit":     "month",
    })
    items = get_dataset_items(ac, run)
    print(f"  → Got {len(items)} raw post items")

    by_username = {}
    for item in items:
        author = (item.get("authorUsername") or item.get("profileUsername") or
                  item.get("username") or "")
        if not author:
            pu = item.get("profileUrl", "") or item.get("authorUrl", "")
            author = username_from_url(pu)
        if author:
            by_username.setdefault(author, []).append(item)
    return by_username

def parse_post(item):
    if item.get("post_type") == "repost" or item.get("isRepost"):
        return None
    date_str = (item.get("postedAt") or
                (item.get("posted_at", {}).get("date")
                 if isinstance(item.get("posted_at"), dict) else None) or
                item.get("publishedAt") or item.get("date") or "")
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

# ── COMMENTS SYNC ─────────────────────────────────────────────────
def sync_comments_for_client(client, dry_run=False):
    """
    Scrapes OUTGOING comments — comments the client made on other people's posts.
    Uses apimaestro/linkedin-profile-comments (one call per client).
    """
    url = client.get("linkedinUrl", "")
    if not url:
        return 0

    cutoff = latest_comment_date(client)

    if dry_run:
        print(f"    [DRY RUN] Would scrape comments for {client['name']}")
        return 0

    ac = apify_client()
    run = ac.actor(COMMENTS_ACTOR).call(run_input={
        "username":              username_from_url(url),
        "total_comments_to_scrape": COMMENTS_PER_PROFILE,
    })
    items = get_dataset_items(ac, run)

    month_record = client["months"][current_month_key()]
    existing_urls = {c.get("url") for c in month_record.get("comments", [])}
    added = 0

    cutoff_dt = None
    if cutoff:
        try:
            cutoff_dt = datetime.strptime(cutoff, "%Y-%m-%d").date()
        except ValueError:
            pass

    for item in items:
        # Parse comment date
        date_str = (item.get("commentedAt") or item.get("postedAt") or
                    item.get("date") or "")
        date_str = str(date_str)[:19]
        if not date_str:
            continue
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            except ValueError:
                continue

        # Only keep current month
        if dt.strftime("%Y-%m") != current_month_key():
            continue
        # Only keep newer than what we have
        if cutoff_dt and dt.date() <= cutoff_dt:
            continue

        comment_url = item.get("url", item.get("postUrl", ""))
        if comment_url and comment_url in existing_urls:
            continue

        comment_text = (item.get("text") or item.get("commentText") or "").strip()
        hook = comment_text.split("\n")[0][:100] if comment_text else "(comment)"

        comment_record = {
            "date":      f"{dt.strftime('%b')} {dt.day}",
            "full_date": dt.strftime("%Y-%m-%d"),
            "text":      hook,
            "url":       comment_url,
            "likes":     item.get("likes", item.get("reactions", 0)),
        }
        month_record.setdefault("comments", []).append(comment_record)
        if comment_url:
            existing_urls.add(comment_url)
        added += 1

    month_record["comments"].sort(key=lambda x: x["full_date"], reverse=True)
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
    parser.add_argument("--posts-only",    action="store_true")
    parser.add_argument("--comments-only", action="store_true")
    args = parser.parse_args()

    do_posts    = not args.comments_only
    do_comments = not args.posts_only

    print("=== RocketRush Tracker Sync ===")
    print(f"Date: {date.today()}  |  Month: {current_month_key()}")
    print(f"Posts: {'yes' if do_posts else 'skip'}  |  "
          f"Comments: {'yes' if do_comments else 'skip'}")
    if args.dry_run:
        print("⚠  DRY RUN")
    print()

    if not GITHUB_TOKEN:
        print("ERROR: GH_PAT not set"); sys.exit(1)
    if not APIFY_TOKEN and not args.dry_run:
        print("ERROR: APIFY_TOKEN not set"); sys.exit(1)

    # Fetch clients.json
    clients_raw, clients_sha = gh_get(CLIENTS_PATH)
    clients_data = json.loads(clients_raw)

    active  = [c for c in clients_data["clients"] if c.get("status") == "active"]
    skipped = [c for c in clients_data["clients"] if c.get("status") != "active"]

    if args.client:
        active = [c for c in active if c["id"] == args.client]
        if not active:
            print(f"Client '{args.client}' not found."); sys.exit(1)

    # Ensure every client has a current-month record with writer/engager
    for c in clients_data["clients"]:
        if c.get("status") in ("active", "paused"):
            ensure_current_month(c)

    print(f"Active: {len(active)}")
    if skipped:
        labels = ", ".join(f"{c['name']} [{c.get('status')}]" for c in skipped)
        print(f"Skipped: {labels}")
    print()

    posts_results    = {}
    total_posts_added    = 0
    total_comments_added = 0

    # ── STEP 1: POSTS (one batch call) ────────────────────────────
    if do_posts:
        print("── Step 1: Syncing Posts ──")
        posts_results = sync_posts(active, dry_run=args.dry_run)
        print()

        for c in active:
            username = username_from_url(c.get("linkedinUrl", ""))
            raw      = posts_results.get(username, [])
            cutoff   = latest_post_date(c)
            month_r  = c["months"][current_month_key()]
            added    = merge_posts(month_r, raw, cutoff)
            total_posts_added += added
            icon = "✅" if added > 0 else "—"
            print(f"  {icon} {c['name']}: {added} new post(s)  "
                  f"({len(raw)} returned by Apify)")

    # ── STEP 2: COMMENTS (one call per client) ────────────────────
    if do_comments:
        print()
        print("── Step 2: Syncing Outgoing Comments ──")
        for c in active:
            added = sync_comments_for_client(c, dry_run=args.dry_run)
            total_comments_added += added
            if not args.dry_run:
                icon = "✅" if added > 0 else "—"
                print(f"  {icon} {c['name']}: {added} new comment(s)")
            c["lastSyncedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── PUSH & REBUILD ────────────────────────────────────────────
    if not args.dry_run:
        print()
        print("── Pushing to GitHub ──")
        gh_put(CLIENTS_PATH,
               json.dumps(clients_data, indent=2, ensure_ascii=False),
               f"Sync {current_month_key()} — {date.today()}", clients_sha)
        print("  ✅ clients.json pushed")
        rebuild_and_push_dashboard(clients_data)

    # ── SUMMARY ───────────────────────────────────────────────────
    print()
    print("=== Summary ===")
    if do_posts:
        est = len(active) * POSTS_PER_PROFILE * 0.005
        print(f"  Posts added:    {total_posts_added}  (~${est:.2f} Apify cost)")
    if do_comments:
        est = len(active) * COMMENTS_PER_PROFILE * 0.005
        print(f"  Comments added: {total_comments_added}  (~${est:.2f} Apify cost)")
    total_est = 0
    if do_posts:    total_est += len(active) * POSTS_PER_PROFILE * 0.005
    if do_comments: total_est += len(active) * COMMENTS_PER_PROFILE * 0.005
    print(f"  Total Apify cost this run: ~${total_est:.2f}")

if __name__ == "__main__":
    main()
