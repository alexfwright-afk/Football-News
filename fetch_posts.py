#!/usr/bin/env python3
"""
fetch_posts.py — pull new Fabrizio Romano posts into posts.json

Source: the public web preview of his Telegram channel,
        https://t.me/s/fabrizioromanotg

Romano mirrors every X post to that channel, so the content is the same as the
X API gave us — but the preview is a plain public web page. No account, no
developer programme, no bearer token, no cost.

How it behaves:
  * first run (empty posts.json) walks back BACKFILL_PAGES pages to seed history
  * later runs stop as soon as they hit a post they already have
  * .last-id is a high-water mark, so posts filtered out as junk are never
    re-requested forever
  * posts.json is written atomically — a half-written file is never deployed

Environment (all optional):
    TG_CHANNEL       defaults to fabrizioromanotg
    MAX_POSTS        how many posts to keep in posts.json (default 800)
    BACKFILL_PAGES   pages to walk on a cold start (default 15, ~300 posts)

Exit codes: 0 ok (even when there is nothing new), 1 the source was unreachable.
"""
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
POSTS = os.path.join(HERE, "posts.json")
LASTID = os.path.join(HERE, ".last-id")

CHANNEL = os.environ.get("TG_CHANNEL", "fabrizioromanotg")
BASE = "https://t.me/s/" + CHANNEL
MAX_POSTS = int(os.environ.get("MAX_POSTS", "800"))
BACKFILL_PAGES = int(os.environ.get("BACKFILL_PAGES", "15"))
INCREMENTAL_PAGES = 4          # plenty: a page holds ~20 posts
MIN_LEN = 25                   # skip bare hashtags and one-word replies

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def log(msg):
    print(msg, flush=True)


def die(msg):
    print("fetch_posts: " + msg, file=sys.stderr)
    sys.exit(1)


# ── fetching ───────────────────────────────────────────────────────────────

def get(before=None, attempt=1):
    """GET one page of the channel preview, retrying politely."""
    url = BASE if before is None else "%s?before=%d" % (BASE, before)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "en",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (429, 500, 502, 503, 504) and attempt <= 3:
            wait = min(60, 5 * 2 ** (attempt - 1))
            log("  %d from Telegram, retrying in %ds (attempt %d/3)" % (e.code, wait, attempt))
            time.sleep(wait)
            return get(before, attempt + 1)
        die("HTTP %d from %s" % (e.code, url))
    except urllib.error.URLError as e:
        if attempt <= 3:
            log("  network error (%s), retrying…" % e.reason)
            time.sleep(5 * attempt)
            return get(before, attempt + 1)
        die("network error: %s" % e.reason)


# ── parsing ────────────────────────────────────────────────────────────────

MSG_SPLIT = re.compile(r'(?=<div class="tgme_widget_message[^"]*"[^>]*data-post=)')
POST_ID_RE = re.compile(r'data-post="([^"/]+)/(\d+)"')
TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
DATE_RE = re.compile(r'class="tgme_widget_message_date"[^>]*>\s*<time datetime="([^"]+)"', re.S)
ANY_TIME_RE = re.compile(r'<time[^>]+datetime="([^"]+)"')
TAG_RE = re.compile(r"<[^>]+>")
BR_RE = re.compile(r"<br\s*/?>", re.I)


def to_text(fragment):
    t = BR_RE.sub("\n", fragment)
    t = TAG_RE.sub("", t)
    t = html_mod.unescape(t)
    t = t.replace("​", "").replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", t).strip()


def clean(text):
    """Drop the mirrored-tweet signature and the trailing t.co links, so the
    text matches what the X API used to hand us."""
    text = re.sub(r"[—-]\s*Fabrizio Romano\s*\(@FabrizioRomano\)[\s\S]*$", "", text)
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def iso_z(stamp):
    """2026-06-16T21:18:11+00:00  ->  2026-06-16T21:18:11.000Z"""
    if not stamp:
        return None
    s = stamp.replace("+00:00", "").replace("Z", "")
    return s + ".000Z"


def parse(page_html):
    out = []
    for chunk in MSG_SPLIT.split(page_html):
        m = POST_ID_RE.search(chunk)
        if not m:
            continue
        pid = int(m.group(2))
        tm = TEXT_RE.search(chunk)
        if not tm:
            continue                       # photo-only or service message
        text = clean(to_text(tm.group(1)))
        if not text:
            continue
        dm = DATE_RE.search(chunk) or ANY_TIME_RE.search(chunk)
        out.append({
            "id": str(pid),
            "ts": iso_z(dm.group(1) if dm else None),
            "url": "https://t.me/%s/%d" % (m.group(1), pid),
            "text": text,
        })
    out.sort(key=lambda p: int(p["id"]))
    return out


# ── store ──────────────────────────────────────────────────────────────────

def load_existing():
    if not os.path.exists(POSTS):
        return []
    try:
        data = json.load(open(POSTS, encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        log("warning: posts.json unreadable (%s) — starting fresh" % e)
        return []


def telegram_ids(posts):
    """Telegram message ids are small (5-7 digits). Anything longer is a
    leftover X id from the old pipeline — ignore it for the high-water mark."""
    for p in posts:
        i = str(p.get("id", ""))
        if i.isdigit() and len(i) <= 9:
            yield int(i)


def main():
    existing = load_existing()
    known = {str(p.get("id")) for p in existing}

    since = max(telegram_ids(existing), default=0)
    if os.path.exists(LASTID):
        seen = open(LASTID).read().strip()
        if seen.isdigit():
            since = max(since, int(seen))

    cold = since == 0
    pages = BACKFILL_PAGES if cold else INCREMENTAL_PAGES
    log("no history — backfilling %d pages" % pages if cold
        else "fetching posts newer than %s" % since)

    fresh, seen_any, cursor = [], [], None
    for page in range(pages):
        batch = parse(get(cursor))
        if not batch:
            log("  page %d: nothing returned" % (page + 1))
            break
        seen_any += batch
        new = [p for p in batch if p["id"] not in known and int(p["id"]) > since]
        for p in new:
            known.add(p["id"])
        fresh += new
        log("  page %d: %d posts, %d new" % (page + 1, len(batch), len(new)))
        cursor = min(int(p["id"]) for p in batch)
        if not cold and not new:
            break                          # caught up
        if cursor <= 1:
            break
        time.sleep(0.7)                    # be a polite guest

    if not seen_any:
        die("couldn't read any posts from %s — is the channel reachable?" % BASE)

    kept = [p for p in fresh if p["ts"] and len(p["text"]) >= MIN_LEN]

    if kept:
        merged = sorted(kept + existing, key=lambda p: p.get("ts") or "", reverse=True)[:MAX_POSTS]
        tmp = POSTS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=1)
        os.replace(tmp, POSTS)             # atomic — never a half-written file
        log("added %d new post%s (%d total)"
            % (len(kept), "" if len(kept) == 1 else "s", len(merged)))
        for p in kept[:10]:
            log("  · " + p["text"].replace("\n", " ")[:90])
    else:
        log("no new posts")

    # Advance the high-water mark only once the posts are safely on disk.
    # Doing it earlier would lose posts if the run died in between.
    high = max(int(p["id"]) for p in seen_any)
    if high > since:
        open(LASTID, "w").write(str(high))


if __name__ == "__main__":
    main()
