"""
RocketRush — Tracker Sync Orchestrator
------------------------------------------------
This is the script that runs when "update the tracker" is triggered.
It's the layer between the manager's request and the actual scraping.

WHAT THIS DOES:
1. Reads clients.json
2. Filters to status == "active" only (paused clients are skipped entirely,
   so no Apify credits are spent on them)
3. For each active client, calculates the sync gap: how many days since
   lastSyncedAt (or "since start of month" if never synced)
4. Calls the Apify actor for that client's LinkedIn URL
5. Hands the raw result to parse_apify_output.py's functions to merge
   into clients.json
6. Reports a summary: who was synced, how many new posts, who was skipped
   and why, and flags anyone with an unusually large gap (10+ days)

WHAT THIS DOES NOT DO:
- Touch comments data (still manual, see earlier discussion)
- Regenerate the dashboard HTML (separate step, see build_dashboard_data.py)
- Push to GitHub (separate step, outside Python)

REQUIRES:
    pip install apify-client
    APIFY_TOKEN environment variable set

USAGE:
    python sync_tracker.py clients.json
    python sync_tracker.py clients.json --client c1   # sync just one client
    python sync_tracker.py clients.json --dry-run     # show what WOULD happen, no API calls
"""

import json
import os
import sys
import argparse
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parse_apify_output import parse_posts, merge_into_clients_json, load_json, save_json

ACTOR_ID = "apimaestro/linkedin-profile-posts"
LARGE_GAP_THRESHOLD_DAYS = 10


def days_since(date_str):
    """date_str format: 'YYYY-MM-DD HH:MM:SS' or None"""
    if not date_str:
        return None
    try:
        last = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").date()
        return (date.today() - last).days
    except ValueError:
        return None


def posts_to_scrape_for_gap(gap_days):
    """
    Scale how many posts to request based on the sync gap.
    Small gap = small request (cheap). Large/first-time gap = bigger pull.
    This is the cost-control logic discussed earlier.
    """
    if gap_days is None:
        return 50  # never synced before -> full backfill for the month
    if gap_days <= 3:
        return 10
    if gap_days <= 10:
        return 25
    return 50  # long gap -> pull more to make sure nothing is missed


def get_active_clients(clients_data):
    return [c for c in clients_data.get("clients", []) if c.get("status") == "active"]


def get_skipped_clients(clients_data):
    return [c for c in clients_data.get("clients", []) if c.get("status") != "active"]


def call_apify_for_client(client, limit, dry_run=False):
    """
    Calls the Apify actor for one client's profile URL.
    Returns the raw list of post items, or None on failure.
    """
    url = client.get("linkedinUrl")
    if not url:
        print(f"  SKIP: {client['name']} has no linkedinUrl set.")
        return None

    if dry_run:
        print(f"  [DRY RUN] Would call Apify for {client['name']} ({url}), limit={limit}")
        return []

    try:
        from apify_client import ApifyClient
    except ImportError:
        print("ERROR: apify-client not installed. Run: pip install apify-client")
        sys.exit(1)

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print("ERROR: APIFY_TOKEN environment variable not set.")
        sys.exit(1)

    client_api = ApifyClient(token)
    print(f"  Calling Apify for {client['name']} (limit={limit})...")

    run = client_api.actor(ACTOR_ID).call(run_input={
        "username": url,
        "total_posts_to_scrape": limit,
    })

    dataset_id = run["defaultDatasetId"] if isinstance(run, dict) else run.default_dataset_id
    items = list(client_api.dataset(dataset_id).iterate_items())
    return items


def sync_one_client(client, clients_data, dry_run=False):
    gap = days_since(client.get("lastSyncedAt"))
    limit = posts_to_scrape_for_gap(gap)

    gap_label = f"{gap} day(s) since last sync" if gap is not None else "never synced (first run)"
    flag = " *** LARGE GAP ***" if (gap is not None and gap >= LARGE_GAP_THRESHOLD_DAYS) else ""
    print(f"\n{client['name']} — {gap_label}{flag}")

    raw_items = call_apify_for_client(client, limit, dry_run=dry_run)
    if raw_items is None:
        return clients_data, {"client": client["name"], "status": "error", "new_posts": 0}
    if dry_run:
        return clients_data, {"client": client["name"], "status": "dry_run", "new_posts": 0}

    posts_by_month = parse_posts(raw_items)
    clients_data = merge_into_clients_json(clients_data, client["id"], posts_by_month)

    total_new = sum(len(v) for v in posts_by_month.values())
    return clients_data, {"client": client["name"], "status": "synced", "new_posts": total_new, "gap_days": gap}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("clients_json", help="Path to clients.json")
    parser.add_argument("--client", help="Sync only this client id", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without calling Apify")
    args = parser.parse_args()

    clients_data = load_json(args.clients_json)
    active = get_active_clients(clients_data)
    skipped = get_skipped_clients(clients_data)

    if args.client:
        active = [c for c in active if c["id"] == args.client]
        if not active:
            print(f"Client id '{args.client}' not found among active clients.")
            sys.exit(1)

    print(f"=== RocketRush Tracker Sync {'(DRY RUN)' if args.dry_run else ''} ===")
    print(f"Active clients to sync: {len(active)}")
    if skipped:
        skipped_labels = ", ".join(f"{c['name']} [{c.get('status')}]" for c in skipped)
        print(f"Skipped (not active): {skipped_labels}")

    results = []
    for client in active:
        clients_data, result = sync_one_client(client, clients_data, dry_run=args.dry_run)
        results.append(result)

    if not args.dry_run:
        save_json(args.clients_json, clients_data)

    print("\n=== Summary ===")
    for r in results:
        if r["status"] == "synced":
            gap_note = f" (gap: {r['gap_days']}d)" if r.get("gap_days") is not None else " (first sync)"
            print(f"  ✓ {r['client']}: {r['new_posts']} posts found{gap_note}")
        elif r["status"] == "dry_run":
            print(f"  - {r['client']}: dry run, no changes made")
        else:
            print(f"  ✗ {r['client']}: {r['status']}")

    large_gaps = [r for r in results if r.get("gap_days", 0) and r["gap_days"] >= LARGE_GAP_THRESHOLD_DAYS]
    if large_gaps:
        print(f"\n⚠ {len(large_gaps)} client(s) had a gap of {LARGE_GAP_THRESHOLD_DAYS}+ days — worth a manual check.")


if __name__ == "__main__":
    main()
