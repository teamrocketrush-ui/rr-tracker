"""
RocketRush — Dashboard Data Builder
------------------------------------------------
Converts clients.json (the source of truth, written by sync_tracker.py)
into the exact JS data shape the dashboard HTML expects, then injects
it into the dashboard file so opening the HTML shows real, current data.

This is the final link in the chain:
  Apify -> parse_apify_output.py -> clients.json -> [THIS SCRIPT] -> tracker.html

WHAT THIS DOES:
1. Reads clients.json
2. For each client, picks the CURRENT month's record (or the most recent
   one if the current month has no data yet)
3. Computes derived display fields: days-since-last-post, status color/width,
   posts/comments MTD tallies, comment-day bar chart data
4. Builds the JS array literal in the dashboard's expected format
5. Replaces the clientData = [...] block in tracker.html with this real data

USAGE:
    python build_dashboard_data.py ../data/clients.json tracker_template.html tracker.html
"""

import json
import sys
import re
from datetime import datetime, date
from pathlib import Path

STATUS_GREEN = "green"
STATUS_AMBER = "amber"
STATUS_RED = "red"

INITIAL_COLORS = ["#3A4A40", "#8A6D3B", "#5C7A68", "#6B5B95", "#4A6670", "#7A5C4A"]


def current_month_key():
    return date.today().strftime("%Y-%m")


def days_since(date_str):
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (date.today() - d).days
    except ValueError:
        return None


def status_from_days(days, target_days_between=3):
    """Maps a days-since-activity number to a status tier."""
    if days is None:
        return STATUS_RED, "No activity", 15
    if days <= 1:
        return STATUS_GREEN, "On time", 90
    if days <= target_days_between:
        return STATUS_AMBER, f"{days}d — watch", 55
    return STATUS_RED, f"{days}d overdue", max(10, 30 - days)


def relative_label(days):
    if days is None:
        return "No activity yet"
    if days == 0:
        return "Today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def build_client_view(client):
    months = client.get("months", {})
    month_key = current_month_key()
    month = months.get(month_key)

    if month is None and months:
        # fall back to most recent month with data, so a client isn't blank
        # just because nothing has synced yet for the new month
        month_key = sorted(months.keys())[-1]
        month = months[month_key]
    if month is None:
        month = {}

    posts = month.get("posts", [])
    comments = month.get("comments", [])
    posts_target = month.get("postsTarget") or 0
    comments_target = month.get("commentsTarget") or 0

    last_post_days = days_since(posts[0]["full_date"]) if posts else None
    last_comment_days = days_since(comments[0]["full_date"]) if comments else None

    post_status, post_label, post_width = status_from_days(last_post_days)
    comment_status, comment_label, comment_width = status_from_days(last_comment_days)

    # last 7 days of comment activity for the bar chart
    comment_days = [0] * 7
    today = date.today()
    for c in comments:
        try:
            cdate = datetime.strptime(c["full_date"], "%Y-%m-%d").date()
            offset = (today - cdate).days
            if 0 <= offset < 7:
                comment_days[6 - offset] += 1
        except (ValueError, KeyError):
            continue

    initials = "".join(w[0] for w in client["name"].split()[:2]).upper()
    color_idx = sum(ord(ch) for ch in client["id"]) % len(INITIAL_COLORS)

    flag_type = post_status
    if post_status == STATUS_RED:
        flag_text = f"Flagged — {post_label} (weekends excluded)"
    elif post_status == STATUS_AMBER:
        flag_text = "Watch — approaching threshold"
    else:
        flag_text = f"On track — {relative_label(last_post_days).lower()}"

    return {
        "id": client["id"],
        "name": client["name"],
        "initials": initials,
        "color": INITIAL_COLORS[color_idx],
        "sub": f"{client.get('engagementType','Retainer')} · target {posts_target}/mo",
        "status": client.get("status", "active"),
        "writer": month.get("writer") or "Unassigned",
        "engager": month.get("engager") or "Unassigned",
        "lastPost": relative_label(last_post_days),
        "lastPostDate": posts[0]["date"] if posts else "—",
        "lastComment": relative_label(last_comment_days),
        "lastCommentDate": comments[0]["date"] if comments else "—",
        "postStatus": post_status,
        "postLabel": post_label,
        "postWidth": post_width,
        "commentStatus": comment_status,
        "commentLabel": comment_label,
        "commentWidth": comment_width,
        "postsMTD": f"{len(posts)} / {posts_target}",
        "commentsMTD": f"{len(comments)} / {comments_target}",
        "flag": {"type": flag_type, "text": flag_text},
        "target": f"Target: {posts_target} posts/mo · {comments_target} comments/mo",
        "posts": posts,
        "commentDays": comment_days,
        "commentLog": comments[:5],
    }


def to_js_literal(value):
    """Minimal safe JS literal serializer using JSON (valid JS object syntax)."""
    return json.dumps(value, ensure_ascii=False)


def build_js_array(clients_view):
    entries = []
    for c in clients_view:
        entries.append(to_js_literal(c))
    return "const clientData = [\n" + ",\n".join(entries) + "\n];"


def inject_into_dashboard(dashboard_html, new_data_block):
    pattern = re.compile(r"const clientData = \[.*?\n\];", re.DOTALL)
    if not pattern.search(dashboard_html):
        raise ValueError("Could not find existing clientData block in dashboard HTML to replace.")
    return pattern.sub(new_data_block, dashboard_html, count=1)


def main():
    if len(sys.argv) != 4:
        print("Usage: python build_dashboard_data.py <clients.json> <template.html> <output.html>")
        sys.exit(1)

    clients_path, template_path, output_path = sys.argv[1], sys.argv[2], sys.argv[3]

    clients_data = json.load(open(clients_path))
    active_only_view = []
    for client in clients_data.get("clients", []):
        # Build view for all clients (paused included) so the toggle still
        # shows them; sync logic elsewhere already skips paused for scraping.
        active_only_view.append(build_client_view(client))

    js_block = build_js_array(active_only_view)

    dashboard_html = Path(template_path).read_text()
    updated_html = inject_into_dashboard(dashboard_html, js_block)
    Path(output_path).write_text(updated_html)

    print(f"Built dashboard with {len(active_only_view)} client(s) -> {output_path}")
    for c in active_only_view:
        print(f"  {c['name']} [{c['status']}]: {c['postsMTD']} posts, last post {c['lastPost']}")


if __name__ == "__main__":
    main()
