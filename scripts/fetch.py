#!/usr/bin/env python3
"""Pull competitor newsroom items into data/news.json.

Each company gets a news source chosen in this order:
  1. `rss` from companies.yaml, if set
  2. The newsroom's own RSS/Atom feed (found on the page, or at a common feed path),
     skipped if its newest item is over 120 days old
  3. Google News RSS limited to the company's own site (site:domain + keywords)
  4. A Google News search on `search` (or the company name), for small companies whose
     sites Google News doesn't index, or companies with no website listed

Feed choices are cached in data/feeds.json and re-checked weekly or when a feed breaks.
Feed health per company goes to data/status.json (shown on the dashboard's Sources tab).
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
NEWS_FILE = DATA / "news.json"
FEEDS_FILE = DATA / "feeds.json"
STATUS_FILE = DATA / "status.json"

KEEP_DAYS = 365          # drop items older than this
REDISCOVER_DAYS = 7      # re-check feed choice this often
NEW_DAYS = 3             # items published longer ago than this are never flagged NEW
TIMEOUT = 20
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 CI-newsroom-tracker")
COMMON_PATHS = [
    "/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml",
    "/news/feed", "/news/rss", "/press-releases/rss", "/news-releases/rss",
    "/rss/news-releases.xml", "/rss/pressrelease.aspx",          # GCS / Q4 investor sites
    "/news-releases?format=rss", "/media/press-releases/rss",
    "/en/rss", "/en/news/rss",
]

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "*/*"})

now = datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def get(url: str) -> requests.Response | None:
    try:
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code < 400:
            return r
    except requests.RequestException:
        pass
    return None


def parse_feed(url: str):
    """Return a parsed feed with entries, or None."""
    r = get(url)
    if not r:
        return None
    ctype = r.headers.get("content-type", "")
    body = r.content
    if "html" in ctype and b"<rss" not in body[:2000] and b"<feed" not in body[:2000]:
        return None
    fp = feedparser.parse(body)
    if fp.entries:
        return fp
    return None


def google_news_url(query: str) -> str:
    return ("https://news.google.com/rss/search?q=" + quote_plus(query)
            + "&hl=en-US&gl=US&ceid=US:en")


STALE_DAYS = 120   # an RSS feed whose newest item is older than this is treated as dead


def is_fresh(fp) -> bool:
    times = [t for t in (entry_time(e) for e in fp.entries) if t]
    return bool(times) and max(times) > now - timedelta(days=STALE_DAYS)


def or_terms(words: list[str] | None, limit: int = 12) -> str:
    if not words:
        return ""
    words = ["psychiatry" if w == "psychiatr" else w for w in words[:limit]]
    return " (" + " OR ".join(f'"{w}"' for w in words) + ")"


def name_query(company: dict) -> str:
    if company.get("search"):
        return company["search"]
    base = re.split(r" \(| / ", company["name"])[0].strip()
    return f'"{base}"' + or_terms(company.get("keywords"))


def discover(company: dict) -> dict:
    """Pick the best working news source for a company, in priority order."""
    site = company.get("site")
    if company.get("rss"):
        return {"feed": company["rss"], "method": "rss (configured)"}

    if site:
        parsed = urlparse(site)
        base = f"{parsed.scheme}://{parsed.netloc}"
        pages = [site] if site.rstrip("/") == base else [site, base]
        candidates: list[str] = []
        for page in pages:
            r = get(page)
            if not r:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
                t = (link.get("type") or "").lower()
                if "rss" in t or "atom" in t:
                    candidates.append(urljoin(r.url, link.get("href", "")))
            for a in soup.find_all("a", href=True):
                h = a["href"]
                if re.search(r"(rss|/feed\b|\.xml$|atom)", h, re.I) and "sitemap" not in h.lower():
                    candidates.append(urljoin(r.url, h))
        site_path = site.rstrip("/")
        candidates += [site_path + p for p in ("/feed", "/rss", "/feed/")]
        candidates += [base + p for p in COMMON_PATHS]

        seen = set()
        for c in candidates:
            if c in seen or c.startswith(("mailto:", "javascript:")):
                continue
            seen.add(c)
            if re.search(r"comments/feed|/tag/|/author/|/category/", c):
                continue
            fp = parse_feed(c)
            if fp and is_fresh(fp):          # skip dead feeds (e.g. an old blog feed)
                return {"feed": c, "method": "newsroom rss"}

        domain = parsed.netloc.removeprefix("www.")
        parts = domain.split(".")
        if len(parts) > 2 and parts[0] in {"ir", "investor", "investors", "news", "media"}:
            domain = ".".join(parts[1:])
        gn = google_news_url(f"site:{domain}" + or_terms(company.get("keywords")))
        if parse_feed(gn):
            return {"feed": gn, "method": "newsroom via Google News (site:)"}
        # Small companies' own sites often aren't indexed by Google News: search the
        # company name instead, which picks up their wire releases (GlobeNewswire etc.)
        return {"feed": google_news_url(name_query(company)), "method": "news search (newsroom not indexed)"}

    return {"feed": google_news_url(name_query(company)), "method": "news search (no newsroom found)"}


def clean(text: str, limit: int = 320) -> str:
    text = BeautifulSoup(html.unescape(text or ""), "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def entry_time(e) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(key)
        if t:
            return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
    return None


def matches(keywords: list[str] | None, text: str) -> bool:
    if not keywords:
        return True
    low = text.lower()
    return any(k.lower() in low for k in keywords)


def process(company: dict, cache: dict) -> tuple[list[dict], dict]:
    name = company["name"]
    cached = cache.get(name)
    sig = json.dumps([company.get(k) for k in ("site", "rss", "search", "keywords")])
    fresh = (cached and cached.get("sig") == sig
             and datetime.fromisoformat(cached["checked"].replace("Z", "+00:00"))
             > now - timedelta(days=REDISCOVER_DAYS))
    reused = bool(fresh)
    choice = dict(cached) if fresh else discover(company)

    fp = parse_feed(choice["feed"])
    if fp is None and reused:                # cached feed broke: rediscover once
        choice, reused = discover(company), False
        fp = parse_feed(choice["feed"])

    choice.update({"sig": sig, "checked": choice["checked"] if reused else iso(now)})
    cache[name] = choice

    status = {"company": name, "site": company.get("site"), "feed": choice["feed"],
              "method": choice["method"], "ok": fp is not None, "items": 0,
              "latest": None, "verify": bool(company.get("verify")),
              "checked": iso(now)}
    items = []
    if fp is None:
        status["error"] = "feed unreachable or empty"
        return items, status

    for e in fp.entries[:60]:
        title = clean(e.get("title", ""), 300)
        summary = clean(e.get("summary", "") or e.get("description", ""))
        if not title:
            continue
        # Google News queries already include the keywords; only filter direct RSS feeds
        if "rss" in choice["method"] and not matches(company.get("keywords"), f"{title} {summary}"):
            continue
        link = e.get("link", "")
        published = entry_time(e)
        if published and published < now - timedelta(days=KEEP_DAYS):
            continue
        src = ""
        if isinstance(e.get("source"), dict):
            src = e["source"].get("title", "")
        items.append({
            "id": hashlib.sha1(f"{name}|{title.lower()}".encode()).hexdigest()[:16],
            "company": name,
            "title": title,
            "summary": summary if summary.lower() != title.lower() else "",
            "link": link,
            "published": iso(published) if published else None,
            "outlet": src,
            "method": choice["method"],
        })
    status["items"] = len(items)
    dates = [i["published"] for i in items if i["published"]]
    status["latest"] = max(dates) if dates else None
    return items, status


def main() -> int:
    DATA.mkdir(exist_ok=True)
    companies = yaml.safe_load((ROOT / "companies.yaml").read_text(encoding="utf-8"))["companies"]
    cache = load_json(FEEDS_FILE, {})
    old = load_json(NEWS_FILE, {"items": []})
    existing = {i["id"]: i for i in old.get("items", [])}
    first_run = not existing

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda c: process(c, cache), companies))

    stamp = iso(now)
    new_count = 0
    for items, _ in results:
        for it in items:
            if it["id"] in existing:
                # keep first_seen; refresh link/summary in case they changed
                existing[it["id"]].update({k: it[k] for k in ("link", "summary", "published") if it[k]})
            else:
                # Only recent items count as "new"; a backfill from a newly added
                # or newly fixed source shouldn't flood everyone's unread count.
                recent = not it["published"] or it["published"] >= iso(now - timedelta(days=NEW_DAYS))
                it["first_seen"] = stamp if (recent and not first_run) else None
                existing[it["id"]] = it
                new_count += 1 if it["first_seen"] else 0

    cutoff = iso(now - timedelta(days=KEEP_DAYS))
    known = {c["name"] for c in companies}
    items = [i for i in existing.values()
             if i["company"] in known and (i["published"] or i.get("first_seen") or stamp) >= cutoff]
    items.sort(key=lambda i: i["published"] or i.get("first_seen") or "", reverse=True)

    meta = {c["name"]: {"site": c.get("site"), "lists": c.get("lists", []),
                        "keywords": c.get("keywords")} for c in companies}
    NEWS_FILE.write_text(json.dumps({"updated": stamp, "companies": meta, "items": items},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    FEEDS_FILE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    STATUS_FILE.write_text(json.dumps({"updated": stamp, "sources": [s for _, s in results]},
                                      indent=1), encoding="utf-8")
    ok = sum(1 for _, s in results if s["ok"])
    print(f"{ok}/{len(results)} sources OK, {len(items)} items, {new_count} new")
    return 0


if __name__ == "__main__":
    sys.exit(main())
