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


def build_client_view(client, month_key, month, is_current_month):
    posts = month.get("posts", [])
    comments = month.get("comments", [])
    posts_target = month.get("postsTarget") or 0
    comments_target = month.get("commentsTarget") or 0

    last_post_days = days_since(posts[0]["full_date"]) if posts else None
    last_comment_days = days_since(comments[0]["full_date"]) if comments else None

    if is_current_month:
        post_status, post_label, post_width = status_from_days(last_post_days)
        comment_status, comment_label, comment_width = status_from_days(last_comment_days)
    else:
        # Past months are frozen historical records — no live overdue flag,
        # just a neutral display so old months don't show misleading red/amber.
        post_status, post_label, post_width = "neutral", "Past month", 100 if posts else 0
        comment_status, comment_label, comment_width = "neutral", "Past month", 100 if comments else 0

    # last 7 days of comment activity for the bar chart (only meaningful for
    # the current month; past months show the full month's daily distribution
    # instead of a rolling 7-day window)
    comment_days = [0] * 7
    today = date.today()
    for c in comments:
        try:
            cdate = datetime.strptime(c["full_date"], "%Y-%m-%d").date()
            offset = (today - cdate).days
            if is_current_month and 0 <= offset < 7:
                comment_days[6 - offset] += 1
        except (ValueError, KeyError):
            continue

    initials = "".join(w[0] for w in client["name"].split()[:2]).upper()
    color_idx = sum(ord(ch) for ch in client["id"]) % len(INITIAL_COLORS)

    flag_type = post_status
    if not is_current_month:
        flag_text = f"Historical record — {len(posts)} post(s) logged"
    elif post_status == STATUS_RED:
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
        "lastPost": relative_label(last_post_days) if is_current_month else (posts[0]["date"] if posts else "—"),
        "lastPostDate": posts[0]["date"] if posts else "—",
        "lastComment": relative_label(last_comment_days) if is_current_month else (comments[0]["date"] if comments else "—"),
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


def month_label(month_key):
    """'2026-06' -> 'June 2026'"""
    try:
        d = datetime.strptime(month_key, "%Y-%m")
        return d.strftime("%B %Y")
    except ValueError:
        return month_key


def build_month_data_js(clients_data):
    """
    Builds the monthData JS object covering EVERY month that appears across
    ALL clients (a union of months), not just the current one. Each client
    that has no record for a given month is simply omitted from that month's
    array (so e.g. a brand-new client doesn't show up in past months).

    Output shape:
        const monthData = {
          "2026-05": [ {client view}, {client view}, ... ],
          "2026-06": [ {client view}, ... ]
        };
        const monthLabels = { "2026-05": "May 2026", "2026-06": "June 2026" };
    """
    today_key = current_month_key()
    all_month_keys = set()
    for client in clients_data.get("clients", []):
        all_month_keys.update(client.get("months", {}).keys())
    # Always include the current month even if nobody has synced yet,
    # so the tab exists and the dashboard isn't blank on the 1st of a new month.
    all_month_keys.add(today_key)

    sorted_keys = sorted(all_month_keys)  # chronological, oldest first

    month_data = {}
    for month_key in sorted_keys:
        is_current = (month_key == today_key)
        views = []
        for client in clients_data.get("clients", []):
            months = client.get("months", {})
            month = months.get(month_key)

            if client.get("status") == "removed":
                # Removed clients stay visible in months where they genuinely
                # had data (so the historical record isn't erased), but never
                # appear in the current month or any month going forward —
                # even if a stray record exists there, we suppress it.
                if month is None or is_current:
                    continue

            if month is None:
                continue  # this client has no record for this month — skip
            views.append(build_client_view(client, month_key, month, is_current))
        month_data[month_key] = views

    labels = {k: month_label(k) for k in sorted_keys}

    month_data_json = json.dumps(month_data, ensure_ascii=False, indent=2)
    labels_json = json.dumps(labels, ensure_ascii=False, indent=2)

    return (
        f"const monthData = {month_data_json};\n"
        f"const monthLabels = {labels_json};\n"
        f"let activeMonth = \"{today_key}\";"
    )


def inject_into_dashboard(dashboard_html, new_data_block):
    """
    Replaces BOTH the old static clientData array AND the placeholder
    monthData/monthLabels/activeMonth block (added by the dashboard template)
    with the freshly generated monthData block. Order matters: clientData
    is replaced first (collapsed to an empty array, since monthData is now
    the single source of truth), then the monthData/monthLabels/activeMonth
    trio is replaced with real generated data.
    """
    # 1. Neutralise the old static clientData array (kept only as a shape
    #    reference in the template; the dashboard no longer reads it once
    #    monthData below is populated).
    client_data_pattern = re.compile(r"const clientData = \[.*?\n\];", re.DOTALL)
    if not client_data_pattern.search(dashboard_html):
        raise ValueError("Could not find clientData block in dashboard HTML.")
    dashboard_html = client_data_pattern.sub("const clientData = [];", dashboard_html, count=1)

    # 2. Replace the monthData/monthLabels/activeMonth placeholder trio.
    month_block_pattern = re.compile(
        r'const monthData = \{[^;]*?"__CURRENT__":\s*clientData\s*\};\s*'
        r'const monthLabels = \{[^;]*?\};\s*'
        r'let activeMonth = Object\.keys\(monthData\)\[0\];',
        re.DOTALL,
    )
    if not month_block_pattern.search(dashboard_html):
        raise ValueError(
            "Could not find monthData placeholder block in dashboard HTML. "
            "Make sure dashboard/tracker_template.html has the month-tabs update applied."
        )
    dashboard_html = month_block_pattern.sub(new_data_block, dashboard_html, count=1)

    return dashboard_html


def main():
    if len(sys.argv) != 4:
        print("Usage: python build_dashboard_data.py <clients.json> <template.html> <output.html>")
        sys.exit(1)

    clients_path, template_path, output_path = sys.argv[1], sys.argv[2], sys.argv[3]

    clients_data = json.load(open(clients_path))

    month_data_block = build_month_data_js(clients_data)

    dashboard_html = Path(template_path).read_text()
    updated_html = inject_into_dashboard(dashboard_html, month_data_block)
    Path(output_path).write_text(updated_html)

    # Summary print
    today_key = current_month_key()
    all_month_keys = set()
    for client in clients_data.get("clients", []):
        all_month_keys.update(client.get("months", {}).keys())
    all_month_keys.add(today_key)
    sorted_keys = sorted(all_month_keys)

    print(f"Built dashboard with {len(sorted_keys)} month tab(s) -> {output_path}")
    for mk in sorted_keys:
        is_current = (mk == today_key)
        n_clients = 0
        for c in clients_data.get("clients", []):
            has_month = mk in c.get("months", {})
            if not has_month:
                continue
            if c.get("status") == "removed" and is_current:
                continue  # matches build_month_data_js suppression logic
            n_clients += 1
        marker = " (current)" if is_current else ""
        print(f"  {month_label(mk)}{marker}: {n_clients} client(s) with data")


if __name__ == "__main__":
    main()
