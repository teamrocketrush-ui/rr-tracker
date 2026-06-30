"""
RocketRush — Apify Output Parser
------------------------------------------------
Takes raw JSON output from the apimaestro/linkedin-profile-posts actor
and converts it into the monthly-snapshot format the dashboard reads.

WHAT THIS DOES:
1. Reads raw post data for one client (from a saved JSON file, or piped
   in directly from the Apify run)
2. Filters out reposts (post_type == "repost") so shared content doesn't
   inflate the client's own posting count
3. Groups posts by month (YYYY-MM)
4. Reshapes each post into the dashboard's expected fields
5. Merges the result into clients.json under that client's monthly record,
   without overwriting other months or other clients

WHAT THIS DOES NOT DO (by design):
- It does not call Apify itself — that happens separately, this just
  parses what Apify already returned
- It does not touch comments-made-by-client data — that's a separate,
  manually-logged data source for now (see earlier discussion)
- It does not push to GitHub — that's a separate step once clients.json
  is updated locally

USAGE:
    python parse_apify_output.py <client_id> <raw_apify_output.json> <clients.json>

Example:
    python parse_apify_output.py c1 apify_test_output.json clients.json
"""

import json
import sys
from datetime import datetime
from pathlib import Path


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def parse_posts(raw_items):
    """
    Filters and reshapes raw Apify post items into the dashboard's format.
    Excludes reposts. Groups by YYYY-MM.
    """
    by_month = {}

    for item in raw_items:
        post_type = item.get("post_type", "regular")
        if post_type == "repost":
            continue  # exclude shared/reposted content from this client's own count

        posted_at = item.get("posted_at", {})
        date_str = posted_at.get("date")
        if not date_str:
            continue  # skip malformed entries rather than crash the whole run

        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

        month_key = dt.strftime("%Y-%m")
        display_date = f"{dt.strftime('%b')} {dt.day}"  # e.g. "Jun 29", "Jun 3" (no leading zero)

        stats = item.get("stats", {})
        text = (item.get("text") or "").strip()
        # Use first line or first ~80 chars as the "hook" for the table view
        hook = text.split("\n")[0][:120] if text else "(no text)"

        post_record = {
            "date": display_date,
            "full_date": dt.strftime("%Y-%m-%d"),
            "title": hook,
            "likes": stats.get("like", stats.get("total_reactions", 0)),
            "comments": stats.get("comments", 0),
            "url": item.get("url", ""),
            "post_type": post_type,
        }

        by_month.setdefault(month_key, []).append(post_record)

    # Sort each month's posts newest-first, matching dashboard expectations
    for month_key in by_month:
        by_month[month_key].sort(key=lambda p: p["full_date"], reverse=True)

    return by_month


def merge_into_clients_json(clients_data, client_id, posts_by_month):
    """
    Merges newly parsed posts into the clients.json structure without
    clobbering other months or other clients' data.
    """
    client = None
    for c in clients_data.get("clients", []):
        if c.get("id") == client_id:
            client = c
            break

    if client is None:
        print(f"WARNING: client_id '{client_id}' not found in clients.json.")
        print("Add the client first via the normal add-client flow before syncing posts.")
        return clients_data

    client.setdefault("months", {})

    for month_key, posts in posts_by_month.items():
        month_record = client["months"].setdefault(month_key, {})
        month_record["posts"] = posts
        month_record["posts_count"] = len(posts)
        # Preserve existing target/writer/engager if already set; don't overwrite
        month_record.setdefault("postsTarget", None)
        month_record.setdefault("commentsTarget", None)
        month_record.setdefault("writer", None)
        month_record.setdefault("engager", None)
        month_record.setdefault("comments", [])  # comments stay manual for now

    client["lastSyncedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return clients_data


def main():
    if len(sys.argv) != 4:
        print("Usage: python parse_apify_output.py <client_id> <raw_apify_output.json> <clients.json>")
        sys.exit(1)

    client_id = sys.argv[1]
    raw_path = sys.argv[2]
    clients_path = sys.argv[3]

    raw_items = load_json(raw_path)
    if not isinstance(raw_items, list):
        print("ERROR: expected raw Apify output to be a JSON list of post items.")
        sys.exit(1)

    posts_by_month = parse_posts(raw_items)

    total_posts = sum(len(v) for v in posts_by_month.values())
    total_excluded = len(raw_items) - total_posts
    print(f"Parsed {len(raw_items)} raw items -> {total_posts} own posts kept, {total_excluded} reposts/invalid excluded.")
    for month, posts in sorted(posts_by_month.items(), reverse=True):
        print(f"  {month}: {len(posts)} posts")

    if Path(clients_path).exists():
        clients_data = load_json(clients_path)
    else:
        print(f"'{clients_path}' not found — creating a new one.")
        clients_data = {"clients": []}

    clients_data = merge_into_clients_json(clients_data, client_id, posts_by_month)
    save_json(clients_path, clients_data)
    print(f"\nSaved to {clients_path}")


if __name__ == "__main__":
    main()
