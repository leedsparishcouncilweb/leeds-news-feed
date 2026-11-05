#!/usr/bin/env python3
"""
Builds an RSS feed from the Leeds Parish Council news page (Hugo Fox site).

It reads the public news page, picks out each article's title, link, excerpt,
thumbnail and "Posted:" date, removes duplicate notices (keeping the newest),
and writes docs/feed.xml.

Run locally:  python make_feed.py
Test offline: python make_feed.py --html saved-page.html
"""

import argparse
import copy
import re
import sys
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup

# ---------------------------------------------------------------- settings --
NEWS_URL = "https://www.leedsparishcouncil.gov.uk/community/leeds-parish-council-18464/news/"
SITE_PREFIX = "/community/leeds-parish-council-18464/"
FEED_TITLE = "Leeds Parish Council News"
FEED_DESCRIPTION = "News and meeting notices from Leeds Parish Council, Leeds, Maidstone"
OUTPUT = Path(__file__).parent / "docs" / "feed.xml"
LAST_CHECKED = Path(__file__).parent / "docs" / "last-checked.txt"
MAX_ITEMS = 25
USER_AGENT = "LeedsParishNewsFeed/1.0 (+https://github.com)"
# ---------------------------------------------------------------------------

# An article link ends in "-<number>", e.g. .../parish-council-meeting-...-94133
ARTICLE_RE = re.compile(re.escape(SITE_PREFIX) + r"(?:news/)?[a-z0-9\-]+-(\d+)/?$", re.I)
POSTED_RE = re.compile(r"Posted:\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})")
AUTHOR_RE = re.compile(r"Article by:\s*(.+?)\s*$", re.M)
EMAIL_RE = re.compile(r"\S+@\S*|\[email\s*protected\]", re.I)
INLINE_TAGS = ["a", "strong", "b", "em", "i", "span", "small", "u", "font"]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode(resp.headers.get_content_charset() or "utf-8", "replace")


def decode_cf_email(hex_string):
    """Cloudflare hides email addresses on the page; this turns them back into text."""
    try:
        key = int(hex_string[:2], 16)
        return "".join(chr(int(hex_string[i:i + 2], 16) ^ key) for i in range(2, len(hex_string), 2))
    except ValueError:
        return ""


def parse_date(text):
    for fmt in ("%d %b %y", "%d %b %Y", "%d %B %y", "%d %B %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(hour=12, tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def tidy(text):
    return re.sub(r"\s+", " ", text or "").strip()


def find_block(heading):
    """Walk up from an article's heading to the element holding just that one article."""
    block = heading
    while block.parent is not None and block.parent.name not in ("body", "html", "[document]"):
        parent = block.parent
        if len(parent.find_all(["h2", "h3", "h4"])) > 1:
            break
        block = parent
    return block


def parse_articles(page_html):
    soup = BeautifulSoup(page_html, "html.parser")

    # Put hidden email addresses back as plain text.
    for el in soup.select("[data-cfemail]"):
        el.replace_with(decode_cf_email(el["data-cfemail"]))

    articles = {}
    for heading in soup.find_all(["h2", "h3", "h4"]):
        link = heading.find("a", href=True)
        if not link:
            continue
        url = urljoin(NEWS_URL, link["href"])
        match = ARTICLE_RE.search(url.split("?")[0])
        if not match:
            continue
        article_id = int(match.group(1))
        title = tidy(link.get_text())
        if not title:
            continue

        block = find_block(heading)
        # Work on a copy with inline tags flattened so each paragraph reads as one line.
        flat = copy.copy(block)
        for tag in flat.find_all(INLINE_TAGS):
            tag.unwrap()
        flat.smooth()  # join the text pieces left behind by unwrapping
        lines = [tidy(t) for t in flat.get_text("\n").split("\n")]
        lines = [l for l in lines if l]
        text = "\n".join(lines)

        posted = POSTED_RE.search(text)
        author = AUTHOR_RE.search(text)

        # Excerpt: the longest line that isn't the title, author, place, or footer.
        skip = {title, "Leeds, Maidstone, Kent", "Leeds Parish Council", "|"}
        candidates = [
            l for l in lines
            if l not in skip and not l.startswith(("Article by:", "Posted:", "awaiting image"))
        ]
        excerpt = max(candidates, key=len) if candidates else ""
        # Remove email addresses (often cut off mid-way) and tidy the ending.
        excerpt = EMAIL_RE.sub("", excerpt)
        excerpt = tidy(re.sub(r"[\s.]*(\.\.\.|…)\s*$", "", excerpt))
        if len(excerpt) < 20:
            excerpt = ""
        elif candidates and re.search(r"(\.\.\.|…)\s*$", max(candidates, key=len)):
            excerpt += "…"

        # Thumbnail: Hugo Fox serves article pictures through getImage.aspx.
        image = None
        for img in block.find_all("img", src=True):
            if "getimage" in img["src"].lower():
                image = urljoin(NEWS_URL, img["src"])
                break

        articles[article_id] = {
            "id": article_id,
            "title": title,
            "url": url,
            "excerpt": excerpt,
            "author": author.group(1) if author else "",
            "date": parse_date(posted.group(1)) if posted else None,
            "image": image,
        }

    # Newest first (Hugo Fox article numbers go up over time).
    items = sorted(articles.values(), key=lambda a: a["id"], reverse=True)

    # Drop repeated notices with the same title, keeping the newest copy.
    seen, unique = set(), []
    for item in items:
        key = re.sub(r"[^a-z0-9]", "", item["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:MAX_ITEMS]


def build_rss(items):
    now = format_datetime(datetime.now(timezone.utc))
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:media="http://search.yahoo.com/mrss/">',
        "<channel>",
        f"<title>{escape(FEED_TITLE)}</title>",
        f"<link>{escape(NEWS_URL)}</link>",
        f"<description>{escape(FEED_DESCRIPTION)}</description>",
        "<language>en-gb</language>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for item in items:
        out.append("<item>")
        out.append(f"<title>{escape(item['title'])}</title>")
        out.append(f"<link>{escape(item['url'])}</link>")
        out.append(f'<guid isPermaLink="false">leeds-parish-{item["id"]}</guid>')
        if item["date"]:
            out.append(f"<pubDate>{format_datetime(item['date'])}</pubDate>")
        if item["author"]:
            out.append(f"<dc:creator>{escape(item['author'])}</dc:creator>")
        if item["excerpt"]:
            out.append(f"<description>{escape(item['excerpt'])}</description>")
        if item["image"]:
            out.append(f'<media:content url="{escape(item["image"], {chr(34): "&quot;"})}" medium="image"/>')
            out.append(f'<enclosure url="{escape(item["image"], {chr(34): "&quot;"})}" type="image/jpeg" length="0"/>')
        out.append("</item>")
    out += ["</channel>", "</rss>", ""]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", help="read a saved copy of the news page instead of downloading it")
    args = ap.parse_args()

    page = Path(args.html).read_text(encoding="utf-8") if args.html else fetch(NEWS_URL)
    items = parse_articles(page)
    if not items:
        # Don't overwrite a good feed with an empty one if the page layout changes.
        sys.exit("No articles found on the news page - feed left unchanged.")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    new_xml = build_rss(items)
    old_xml = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""

    # Only rewrite when the articles changed (ignore the build timestamp).
    strip = lambda x: re.sub(r"<lastBuildDate>.*?</lastBuildDate>", "", x)
    if strip(new_xml) != strip(old_xml):
        OUTPUT.write_text(new_xml, encoding="utf-8")
        print(f"Feed updated: {len(items)} articles.")
    else:
        print(f"No changes ({len(items)} articles).")

    # One small change per day keeps GitHub's hourly schedule switched on.
    LAST_CHECKED.write_text(datetime.now(timezone.utc).strftime("%Y-%m-%d") + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
