#!/usr/bin/env python3
"""Release scheduled posts into the per-platform RSS feeds.

Reads queue/queue.json. Any post whose scheduled time has passed and that has
not been released yet is stamped with released_at. Every feed file is then
regenerated from the released posts (newest first, last KEEP items per feed).

dlvr.it reads the feed files and posts new items as it finds them. Standard
library only.

Usage:  python3 scripts/release.py [--now 2026-11-13T03:45:00Z] [--root DIR]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

BASE_URL = "https://coachdjgreer.github.io/feeds/"
PLATFORMS = {
    "bluesky": "Bluesky",
    "x": "X",
    "threads": "Threads",
    "linkedin": "LinkedIn",
    "instagram": "Instagram",
}
KEEP = 20
EPOCH = "1970-01-01T00:00:00Z"
STAMP = "%Y-%m-%dT%H:%M:%SZ"


def parse_utc(value):
    return datetime.strptime(value, STAMP).replace(tzinfo=timezone.utc)


def when_to_utc(post, default_tz):
    """'when' with an offset or Z is absolute; otherwise it is local time in the
    post's (or the queue's) timezone."""
    raw = post["when"].strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(post.get("timezone", default_tz)))
    return dt.astimezone(timezone.utc)


def check_post(post, root, seen):
    """Return an error string, or None if the post is valid."""
    pid = post.get("id")
    if not pid or not isinstance(pid, str):
        return "missing id"
    if pid in seen:
        return "duplicate id"
    seen.add(pid)
    plats = post.get("platforms")
    if not plats or not all(p in PLATFORMS for p in plats):
        return "platforms must be a list drawn from %s" % ", ".join(PLATFORMS)
    if not str(post.get("text", "")).strip():
        return "empty text"
    if not post.get("when"):
        return "missing when"
    img = post.get("image")
    if img and not os.path.isfile(os.path.join(root, img)):
        return "image file not found: %s" % img
    return None


def build_feed(platform, posts, root):
    items = [p for p in posts if platform in p["platforms"] and p.get("released_at")]
    items.sort(key=lambda p: p["released_at"], reverse=True)
    items = items[:KEEP]
    last = items[0]["released_at"] if items else EPOCH
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">',
        "  <channel>",
        "    <title>coachdjgreer %s feed</title>" % PLATFORMS[platform],
        "    <link>%s</link>" % BASE_URL,
        "    <description>Posts for %s from coachdjgreer</description>" % PLATFORMS[platform],
        "    <language>en-ca</language>",
        "    <lastBuildDate>%s</lastBuildDate>" % format_datetime(parse_utc(last), usegmt=True),
    ]
    for p in items:
        text = escape(p["text"])
        out += [
            "    <item>",
            "      <title>%s</title>" % text,
            "      <description>%s</description>" % text,
            '      <guid isPermaLink="false">coachdjgreer-%s-%s</guid>' % (platform, escape(p["id"])),
            "      <pubDate>%s</pubDate>" % format_datetime(parse_utc(p["released_at"]), usegmt=True),
        ]
        img = p.get("image")
        if img:
            url = BASE_URL + img
            size = os.path.getsize(os.path.join(root, img))
            ctype = "image/png" if img.lower().endswith(".png") else "image/jpeg"
            out.append('      <enclosure url="%s" length="%d" type="%s"/>' % (url, size, ctype))
            dims = ""
            if p.get("image_width") and p.get("image_height"):
                dims = ' width="%d" height="%d"' % (p["image_width"], p["image_height"])
            out.append('      <media:content url="%s" type="%s" medium="image"%s/>' % (url, ctype, dims))
        out.append("    </item>")
    out += ["  </channel>", "</rss>", ""]
    return "\n".join(out)


def write_if_changed(path, content):
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    if old == content:
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return True


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help="UTC time like 2026-11-13T03:45:00Z (testing only)")
    ap.add_argument("--root", default=here)
    args = ap.parse_args()

    now = parse_utc(args.now) if args.now else datetime.now(timezone.utc).replace(microsecond=0)
    qpath = os.path.join(args.root, "queue", "queue.json")
    with open(qpath, encoding="utf-8") as f:
        queue = json.load(f)
    default_tz = queue.get("timezone", "America/Vancouver")
    posts = queue.get("posts", [])

    seen, released, problems, bad = set(), [], [], set()
    for n, post in enumerate(posts):
        err = check_post(post, args.root, seen)
        if err:
            problems.append((post.get("id", "?"), err))
            bad.add(n)
            continue
        if post.get("released_at") or post.get("paused"):
            continue
        try:
            due = when_to_utc(post, default_tz)
        except Exception as e:  # bad date text
            problems.append((post["id"], "cannot read 'when': %s" % e))
            bad.add(n)
            continue
        if due <= now:
            post["released_at"] = now.strftime(STAMP)
            released.append(post["id"])

    changed = []
    if released:
        text = json.dumps(queue, indent=2, ensure_ascii=False) + "\n"
        if write_if_changed(qpath, text):
            changed.append("queue/queue.json")

    valid = [p for n, p in enumerate(posts) if n not in bad]
    for platform in PLATFORMS:
        content = build_feed(platform, valid, args.root)
        if write_if_changed(os.path.join(args.root, platform + ".xml"), content):
            changed.append(platform + ".xml")

    print("now (UTC):", now.strftime(STAMP))
    print("released:", ", ".join(released) if released else "nothing due")
    print("files changed:", ", ".join(changed) if changed else "none")
    for pid, err in problems:
        print("PROBLEM with post %s: %s" % (pid, err))
    return 0


if __name__ == "__main__":
    sys.exit(main())
