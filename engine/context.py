"""Context layer: what newsletters, research houses and the trade press say about each bottleneck.

Reads the free feeds in config/context_sources.json plus one Google News query per theme, keeps
items that look supply-chain related, and asks an LLM (GLM via the shared worker, flat-rate plan)
to tag each one: which themes, whether it points to tightening or easing, the fact type, a one-line
summary in our own words, and any bottleneck terms that none of our themes covers.

Stored: title, link, source, date and the tags (data/context/items.jsonl). Article text is never
stored or republished. This layer is context only: no tested call reads it.

    python engine/context.py [--days 7] [--max-per-query 25] [--workers 3]
"""
import argparse
import calendar
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import feedparser
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
CFG = json.load(open("config/context_sources.json"))
THEMES = {t["slug"]: t["name"] for t in json.load(open("config/themes.json"))}
ITEMS = "data/context/items.jsonl"
SEEN = "data/context/seen.txt"  # ids already judged (kept or not), so the LLM never sees an item twice
GLM = "/home/nik/projects/multisite-audit/tools/glm.py"
UA = "Mozilla/5.0 (compatible; LeadtimeBot/0.1; +https://github.com/NiklavsD/bottleneck-signals)"
BATCH = 15

KEYWORDS = sorted({k.lower() for ks in CFG["themes"].values() for k in ks} |
                  {k.lower() for k in CFG["prefilter_extra"]} | {n.lower() for n in THEMES.values()})
KW_RE = re.compile("|".join(r"\b" + re.escape(k) for k in KEYWORDS), re.I)


def iid(url):
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def clean(s, n=600):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()[:n]


def published(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return dt.datetime.fromtimestamp(calendar.timegm(t), dt.timezone.utc).date().isoformat() if t else None


def read_feed(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=45)
    r.raise_for_status()
    return feedparser.parse(r.content).entries


def gather(days, max_per_query):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = {}

    def add(e, src, name, kind, theme_hint=None, force=False):
        url = e.get("link")
        d = published(e)
        if not url or not d or d < since:
            return
        title = clean(e.get("title"), 300)
        # Google News titles end with " - Publisher"
        publisher = None
        if (kind == "news" or "news.google.com" in url) and " - " in title:
            title, publisher = title.rsplit(" - ", 1)
        text = title + " " + clean(e.get("summary"))
        if not force and not KW_RE.search(text):
            return
        k = iid(url)
        it = out.setdefault(k, {"id": k, "url": url, "title": title, "published": d, "source": src,
                                "source_name": publisher or name, "kind": kind, "snippet": clean(e.get("summary")),
                                "hints": []})
        if theme_hint and theme_hint not in it["hints"]:
            it["hints"].append(theme_hint)

    def feed_job(f):
        url = f.get("url") or "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": f"site:{f['site']} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        try:
            return f, read_feed(url)
        except Exception as ex:  # a dead feed must not stop the run
            print(f"  feed {f['id']}: {ex}", file=sys.stderr)
            return f, []

    def news_job(slug):
        terms = " OR ".join(f'"{k}"' for k in CFG["themes"][slug])
        q = f"({terms}) {CFG['google_news_qualifier']} when:{days}d"
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        try:
            return slug, read_feed(url)[:max_per_query]
        except Exception as ex:
            print(f"  news {slug}: {ex}", file=sys.stderr)
            return slug, []

    with ThreadPoolExecutor(6) as ex:
        for f, entries in ex.map(feed_job, CFG["feeds"]):
            for e in entries:
                add(e, f["id"], f["name"], f["kind"], force=f.get("all", False))
        for slug, entries in ex.map(news_job, list(CFG["themes"])):
            for e in entries:
                add(e, "gnews", "Google News", "news", theme_hint=slug, force=True)
    return list(out.values())


PROMPT = """You tag news items for a research site that tracks physical supply bottlenecks behind AI, power and robotics.
Themes (slug: name):
{themes}

For EACH item in the JSON array on stdin, return one object:
{{"id": <same id>, "relevant": true|false, "themes": [slugs, max 3, only from the list],
  "signal": "tightening"|"easing"|"neutral",
  "fact": "price"|"lead_time"|"capacity"|"demand"|"shortage"|"policy"|"other",
  "claim": "<one sentence, max 25 words, in your own words, stating the concrete supply/demand fact>",
  "new_terms": [short names of specific supply bottlenecks mentioned that NONE of the themes covers, else []]}}

Rules:
- relevant = the item says something about supply, demand, prices, capacity, lead times or shortages for one of the themes. Product reviews, games, stock tips, generic AI model news: relevant=false.
- tightening = demand outrunning supply (shortage, price up, longer lead times, sold out, allocation). easing = the opposite (glut, price cuts, shorter lead times, capacity catching up). Otherwise neutral.
- Use ONLY the title and snippet given. Do not add numbers or facts that are not there. If no concrete fact, state the headline's point plainly.
- "hints" are the search topics that found the item; they are not proof it is relevant.
Output ONLY the JSON array, one object per input item, nothing else."""


def tag_batch(batch):
    payload = json.dumps([{"id": b["id"], "title": b["title"], "snippet": b["snippet"][:500], "source": b["source_name"],
                           "hints": b["hints"]} for b in batch], ensure_ascii=False)
    prompt = PROMPT.format(themes="\n".join(f"{k}: {v}" for k, v in THEMES.items()))
    for attempt in range(3):
        try:
            r = subprocess.run([GLM, "--model", "glm-5.3", "-", prompt], input=payload, capture_output=True,
                               text=True, timeout=900)
            m = re.search(r"\[\s*\{.*\}\s*\]", r.stdout, re.S)
            if m:
                return json.loads(m.group(0))
            print(f"  tag: no JSON (attempt {attempt + 1}): {r.stdout[-200:]} {r.stderr[-200:]}", file=sys.stderr)
        except Exception as ex:
            print(f"  tag error (attempt {attempt + 1}): {ex}", file=sys.stderr)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--max-per-query", type=int, default=25)
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    os.makedirs("data/context", exist_ok=True)
    seen = set(open(SEEN).read().split()) if os.path.exists(SEEN) else set()
    fresh = [i for i in gather(a.days, a.max_per_query) if i["id"] not in seen]
    print(f"candidates {len(fresh)} new")
    batches = [fresh[i:i + BATCH] for i in range(0, len(fresh), BATCH)]
    kept = failed = 0
    with ThreadPoolExecutor(a.workers) as ex:
        for batch, tags in zip(batches, ex.map(tag_batch, batches)):
            if tags is None:
                failed += len(batch)
                continue  # not marked seen: retried next run
            by_id = {t.get("id"): t for t in tags if isinstance(t, dict)}
            with open(ITEMS, "a") as f_items, open(SEEN, "a") as f_seen:
                for it in batch:
                    t = by_id.get(it["id"])
                    if t is None:
                        continue  # model skipped it: retry next run
                    f_seen.write(it["id"] + "\n")
                    themes = [s for s in t.get("themes", []) if s in THEMES]
                    if not t.get("relevant") or not themes:
                        continue
                    rec = {k: it[k] for k in ("id", "url", "title", "published", "source", "source_name", "kind")}
                    rec.update(themes=themes, signal=t.get("signal") if t.get("signal") in ("tightening", "easing") else "neutral",
                               fact=t.get("fact", "other"), claim=clean(t.get("claim"), 240),
                               new_terms=[clean(x, 60) for x in t.get("new_terms", [])][:5],
                               tagged_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), tagger="glm-5.3")
                    f_items.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    kept += 1
    total = sum(1 for _ in open(ITEMS)) if os.path.exists(ITEMS) else 0
    print(f"kept {kept} relevant, {failed} to retry, total stored {total}")


if __name__ == "__main__":
    main()
