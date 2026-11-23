#!/usr/bin/env python3
"""Pull competitor newsroom items into data/news.json.

Each company gets a news source chosen in this order:
  1. `rss` from companies.yaml, if set
  2. The newsroom's own RSS/Atom feed (found on the page, or at a common feed path),
     skipped if its newest item is over 120 days old
  3. Google News RSS limited to the company's own site (site:domain + keywords)
  4. A Google News search on `search` (or the company name), for small companies whose
     sites Google News doesn't index, or companies with no website listed
Companies with a `scrape:` block skip all of this: their news page is read directly.

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
        fp = parse_feed(company["rss"])
        if fp:
            return {"feed": company["rss"], "method": "rss (configured)"}
        # configured feed unreachable (moved, or blocks GitHub's servers): try the rest

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
        fp = parse_feed(gn)
        if fp and is_fresh(fp):              # nothing in 4 months = treat as not indexed
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


DATE_FORMATS = ["%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d",
                "%d %B %Y", "%d %b %Y", "%d.%m.%Y", "%m/%d/%Y"]


def parse_date(text: str) -> datetime | None:
    text = re.sub(r"\s+", " ", (text or "").strip())
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(hour=12, tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def scrape_page(url: str, cfg: dict) -> list[dict] | None:
    """For newsrooms with no RSS: read items straight off the news page with CSS selectors.

    cfg: {item: <selector for each news entry>, title: <selector inside item>,
          date: <selector inside item>, link: <optional selector for the <a>>}
    """
    r = get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    entries = []
    for node in soup.select(cfg["item"])[:60]:
        t = node.select_one(cfg["title"]) if cfg.get("title") else node
        d = node.select_one(cfg["date"]) if cfg.get("date") else None
        a = node if node.name == "a" else (node.select_one(cfg.get("link", "a")) or node.find("a"))
        title = t.get_text(" ", strip=True) if t else ""
        if not title or not a or not a.get("href"):
            continue
        pub = parse_date(d.get_text(" ", strip=True)) if d else None
        entries.append({"title": title, "summary": "", "link": urljoin(r.url, a["href"]),
                        "published_dt": pub})
    return entries or None


def process(company: dict, cache: dict) -> tuple[list[dict], dict]:
    name = company["name"]
    if company.get("scrape"):
        entries = scrape_page(company["site"], company["scrape"])
        return build_items(company, entries, {"feed": company["site"], "method": "newsroom page (scraped)"})
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

    entries = None
    if fp is not None:
        entries = [{"title": e.get("title", ""), "summary": e.get("summary", "") or e.get("description", ""),
                    "link": e.get("link", ""), "published_dt": entry_time(e),
                    "outlet": e["source"].get("title", "") if isinstance(e.get("source"), dict) else ""}
                   for e in fp.entries]
    return build_items(company, entries, choice)


def build_items(company: dict, entries: list[dict] | None, choice: dict) -> tuple[list[dict], dict]:
    name = company["name"]
    status = {"company": name, "site": company.get("site"), "feed": choice["feed"],
              "method": choice["method"], "ok": entries is not None, "items": 0,
              "latest": None, "verify": bool(company.get("verify")),
              "checked": iso(now)}
    items = []
    if entries is None:
        status["error"] = "feed unreachable or empty"
        return items, status

    # Google News queries already include the keywords; filter only direct newsroom sources
    direct = "rss" in choice["method"] or "scraped" in choice["method"]
    for e in entries[:60]:
        title = clean(e["title"], 300)
        summary = clean(e.get("summary", ""))
        if not title:
            continue
        if direct and not matches(company.get("keywords"), f"{title} {summary}"):
            continue
        published = e.get("published_dt")
        if published and published < now - timedelta(days=KEEP_DAYS):
            continue
        items.append({
            "id": hashlib.sha1(f"{name}|{title.lower()}".encode()).hexdigest()[:16],
            "company": name,
            "title": title,
            "summary": summary if summary.lower() != title.lower() else "",
            "link": e.get("link", ""),
            "published": iso(published) if published else None,
            "outlet": e.get("outlet", ""),
            "method": choice["method"],
        })
    status["items"] = len(items)
    dates = [i["published"] for i in items if i["published"]]
    status["latest"] = max(dates) if dates else None
    return items, status


# ------------------------------------------------------------------ ClinicalTrials.gov
CTGOV = "https://clinicaltrials.gov/api/v2/studies"
TRIALS_FILE = DATA / "trials.json"
TRIALS_EVERY_HOURS = 6        # trials change slowly; checking every 6 hours is plenty
TRIALS_SINCE = "2024-01-01"   # only trials updated on ClinicalTrials.gov since this date
NEW_TRIAL_DAYS = 14           # a trial first posted within this many days counts as new
CT_FIELDS = ("NCTId,BriefTitle,OverallStatus,Phase,LeadSponsorName,Condition,InterventionName,"
             "StartDate,PrimaryCompletionDate,LastUpdatePostDate,StudyFirstPostDate,EnrollmentCount")


def ct_search(params: dict) -> list[dict] | None:
    """Run one ClinicalTrials.gov search (all pages, up to 1,000 trials). None = request failed."""
    studies, token = [], None
    for _ in range(5):
        p = dict(params)
        p.update({"pageSize": 200, "fields": CT_FIELDS,
                  "filter.advanced": f"AREA[LastUpdatePostDate]RANGE[{TRIALS_SINCE},MAX]"})
        if token:
            p["pageToken"] = token
        try:
            r = session.get(CTGOV, params=p, timeout=30)
            r.raise_for_status()
            d = r.json()
        except Exception:
            return None
        studies += d.get("studies", [])
        token = d.get("nextPageToken")
        if not token:
            break
        time.sleep(0.3)
    return studies


def pretty(status: str) -> str:
    return (status or "").replace("_", " ").capitalize()


def ct_record(study: dict) -> dict:
    ps = study.get("protocolSection", {})
    ident, st = ps.get("identificationModule", {}), ps.get("statusModule", {})
    design = ps.get("designModule", {})
    nct = ident.get("nctId", "")
    return {
        "nct": nct,
        "title": ident.get("briefTitle", ""),
        "status": st.get("overallStatus", ""),
        "phase": ", ".join(p.replace("PHASE", "Ph ").replace("EARLY_Ph 1", "Early Ph 1").replace("NA", "N/A")
                           for p in design.get("phases", [])),
        "sponsor": ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", ""),
        "conditions": ps.get("conditionsModule", {}).get("conditions", [])[:6],
        "interventions": [i.get("name", "") for i in ps.get("armsInterventionsModule", {}).get("interventions", [])][:6],
        "enrollment": design.get("enrollmentInfo", {}).get("count"),
        "start": st.get("startDateStruct", {}).get("date"),
        "primary_completion": st.get("primaryCompletionDateStruct", {}).get("date"),
        "first_posted": st.get("studyFirstPostDateStruct", {}).get("date"),
        "last_update": st.get("lastUpdatePostDateStruct", {}).get("date"),
        "url": f"https://clinicaltrials.gov/study/{nct}",
    }


def update_trials(companies: list[dict]) -> list[dict]:
    """Refresh data/trials.json and return news items for new trials and trial changes."""
    cfg = {c["name"]: c["trials"] for c in companies if c.get("trials")}
    sig = hashlib.sha1(json.dumps([cfg, TRIALS_SINCE], sort_keys=True).encode()).hexdigest()
    prev = load_json(TRIALS_FILE, {"updated": None, "trials": {}})
    if (prev.get("sig") == sig and prev.get("updated")
            and datetime.fromisoformat(prev["updated"].replace("Z", "+00:00"))
            > now - timedelta(hours=TRIALS_EVERY_HOURS)):
        return []                                   # checked recently, nothing to do
    old = prev.get("trials", {})
    first_run = not old
    found: dict[str, dict] = {}
    status, failed = {}, set()

    for name, t in cfg.items():
        queries = []
        for term in t.get("terms", []):
            q = {"query.intr": term}
            if t.get("condition"):
                q["query.cond"] = t["condition"]
            queries.append((term, q))
        if t.get("sponsor"):
            q = {"query.spons": t["sponsor"]}
            if t.get("condition"):
                q["query.cond"] = t["condition"]
            queries.append((f"sponsor: {t['sponsor']}", q))
        ok, count = 0, 0
        for label, q in queries:
            studies = ct_search(q)
            time.sleep(0.3)
            if studies is None:
                continue
            ok += 1
            for s in studies:
                rec = ct_record(s)
                if not rec["nct"]:
                    continue
                cur = found.setdefault(rec["nct"], dict(rec, companies=[], matched=[]))
                if name not in cur["companies"]:
                    cur["companies"].append(name)
                    count += 1
                if label not in cur["matched"]:
                    cur["matched"].append(label)
        if ok < len(queries):
            failed.add(name)
        status[name] = {"queries": len(queries), "ok": ok, "trials": count}

    # If a company's searches failed this time, keep its trials from last time
    for nct, rec in old.items():
        if nct not in found and any(c in failed for c in rec.get("companies", [])):
            found[nct] = rec

    stamp = iso(now)
    events = []
    recent = (now - timedelta(days=NEW_TRIAL_DAYS)).strftime("%Y-%m-%d")
    for nct, rec in found.items():
        before = old.get(nct)
        rec["changes"] = (before or {}).get("changes", [])
        rec["first_seen"] = before.get("first_seen") if before else (None if first_run else stamp)
        if before is None:
            if rec["first_posted"] and rec["first_posted"] >= recent:
                events.append((rec, f"New trial registered: {rec['title']}", rec["first_posted"]))
            continue
        for field, label in (("status", "Status"), ("primary_completion", "Primary completion")):
            if before.get(field) and rec.get(field) and before[field] != rec[field]:
                change = {"date": stamp, "field": label, "from": before[field], "to": rec[field]}
                rec["changes"] = (rec["changes"] + [change])[-10:]
                old_v, new_v = (pretty(before[field]), pretty(rec[field])) if field == "status" else (before[field], rec[field])
                events.append((rec, f"Trial {label.lower()} changed {old_v} \u2192 {new_v}: {rec['title']}", None))

    TRIALS_FILE.write_text(json.dumps({"updated": stamp, "sig": sig, "since": TRIALS_SINCE,
                                       "trials": found, "status": status},
                                      ensure_ascii=False, indent=1), encoding="utf-8")

    items = []
    for rec, title, date in events:
        published = (datetime.fromisoformat(date).replace(hour=12, tzinfo=timezone.utc)
                     if date and len(date) == 10 else now)
        others = rec["companies"][1:]
        detail = " \u00b7 ".join(x for x in [rec["nct"], rec["phase"], pretty(rec["status"]), rec["sponsor"],
                                          ", ".join(rec["conditions"][:3]),
                                          ("also tracked under " + ", ".join(others)) if others else ""] if x)
        company = rec["companies"][0]          # one feed item per trial event
        items.append({
            "id": hashlib.sha1(f"{company}|{rec['nct']}|{title}".encode()).hexdigest()[:16],
            "company": company, "title": title, "summary": detail, "link": rec["url"],
            "published": iso(published), "outlet": "", "method": "ClinicalTrials.gov",
            "new_days": NEW_TRIAL_DAYS,
        })
    print(f"trials: {len(found)} found, {len(events)} new/changed, {len(failed)} companies with failed searches")
    return items


def main() -> int:
    DATA.mkdir(exist_ok=True)
    companies = yaml.safe_load((ROOT / "companies.yaml").read_text(encoding="utf-8"))["companies"]
    cache = load_json(FEEDS_FILE, {})
    old = load_json(NEWS_FILE, {"items": []})
    existing = {i["id"]: i for i in old.get("items", [])}
    first_run = not existing

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda c: process(c, cache), companies))
    try:
        trial_items = update_trials(companies)
    except Exception as exc:                    # never let trials break the news update
        print(f"trials skipped: {exc}")
        trial_items = []
    results.append((trial_items, None))

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
                window = it.pop("new_days", NEW_DAYS)
                recent = not it["published"] or it["published"] >= iso(now - timedelta(days=window))
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
    STATUS_FILE.write_text(json.dumps({"updated": stamp, "sources": [s for _, s in results if s is not None]},
                                      indent=1), encoding="utf-8")
    results = [r for r in results if r[1] is not None]
    ok = sum(1 for _, s in results if s["ok"])
    print(f"{ok}/{len(results)} sources OK, {len(items)} items, {new_count} new")
    return 0


if __name__ == "__main__":
    sys.exit(main())
