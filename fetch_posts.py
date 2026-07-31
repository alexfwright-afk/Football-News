#!/usr/bin/env python3
"""
fetch_posts.py — pull new posts from one or more public Telegram channels
                 into posts.json

Every post carries a `source` field, so the app can label where it came from
and you can filter by it. Post ids are namespaced (`romano-63801`) so two
channels can never collide.

Adding a source is one line in SOURCES below, or the TG_SOURCES environment
variable — no other file needs changing.

How it behaves:
  * each source is fetched independently; one being down doesn't stop the rest
  * first run for a source walks back BACKFILL_PAGES pages to seed history
  * later runs stop as soon as they hit a post they already have
  * the high-water mark per source is derived from posts.json itself, so
    nothing depends on a cache file surviving between runs
  * posts.json is written atomically — a half-written file is never deployed

Environment (all optional):
    TG_SOURCES       "key:channel:Label, key:channel:Label"
    MAX_POSTS        how many posts to keep in total (default 800)
    BACKFILL_PAGES   pages to walk on a cold start (default 15, ~300 posts)

Exit codes: 0 ok (even when there is nothing new), 1 no source was reachable.
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

# key, channel, display name. The key is what ends up in each post and in the
# app's source filter — keep it short and don't change it once it's in use.
DEFAULT_SOURCES = [
    ("romano",   "fabrizioromanotg", "Fabrizio Romano"),
    ("ornstein", "David_Ornstein",   "David Ornstein"),
]

MAX_POSTS = int(os.environ.get("MAX_POSTS", "800"))
BACKFILL_PAGES = int(os.environ.get("BACKFILL_PAGES", "15"))
INCREMENTAL_PAGES = 4          # plenty: a page holds ~20 posts
MIN_LEN = 25                   # skip bare hashtags and one-word replies

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def sources():
    raw = os.environ.get("TG_SOURCES", "").strip()
    if not raw:
        return DEFAULT_SOURCES
    out = []
    for part in raw.split(","):
        bits = [b.strip() for b in part.split(":")]
        if len(bits) >= 2 and bits[0] and bits[1]:
            out.append((bits[0], bits[1], bits[2] if len(bits) > 2 else bits[1]))
    return out or DEFAULT_SOURCES


def log(msg):
    print(msg, flush=True)


def die(msg):
    print("fetch_posts: " + msg, file=sys.stderr)
    sys.exit(1)


# ── fetching ───────────────────────────────────────────────────────────────

def get(channel, before=None, attempt=1):
    """GET one page of a channel preview, retrying politely."""
    url = "https://t.me/s/" + channel
    if before is not None:
        url += "?before=%d" % before
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
            log("    %d from Telegram, retrying in %ds (attempt %d/3)" % (e.code, wait, attempt))
            time.sleep(wait)
            return get(channel, before, attempt + 1)
        log("    HTTP %d from %s" % (e.code, url))
        return None
    except urllib.error.URLError as e:
        if attempt <= 3:
            log("    network error (%s), retrying…" % e.reason)
            time.sleep(5 * attempt)
            return get(channel, before, attempt + 1)
        log("    network error: %s" % e.reason)
        return None


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
    """Strip the mirrored-tweet signature some channels append, and any bare
    links — including the self-link Telegram puts at the top of some posts."""
    text = re.sub(r"[—-]\s*Fabrizio Romano\s*\(@FabrizioRomano\)[\s\S]*$", "", text)
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def iso_z(stamp):
    """2026-06-16T21:18:11+00:00  ->  2026-06-16T21:18:11.000Z"""
    if not stamp:
        return None
    return stamp.replace("+00:00", "").replace("Z", "") + ".000Z"


def parse(page_html, key):
    out = []
    for chunk in MSG_SPLIT.split(page_html):
        m = POST_ID_RE.search(chunk)
        if not m:
            continue
        num = int(m.group(2))
        tm = TEXT_RE.search(chunk)
        if not tm:
            continue                       # photo-only or service message
        text = clean(to_text(tm.group(1)))
        if not text:
            continue
        dm = DATE_RE.search(chunk) or ANY_TIME_RE.search(chunk)
        out.append({
            "id": "%s-%d" % (key, num),
            "num": num,                    # dropped before saving
            "source": key,
            "ts": iso_z(dm.group(1) if dm else None),
            "url": "https://t.me/%s/%d" % (m.group(1), num),
            "text": text,
        })
    out.sort(key=lambda p: p["num"])
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


def migrate(posts, first_key):
    """Posts saved before multi-source support have a bare numeric id and no
    source. Give them both, once, so everything downstream can assume the new
    shape. Legacy X ids (19 digits) are dropped — that pipeline is long gone."""
    out, moved, dropped = [], 0, 0
    for p in posts:
        pid = str(p.get("id", ""))
        if p.get("source"):
            out.append(p)
            continue
        if pid.isdigit():
            if len(pid) > 9:               # an X id from the original build
                dropped += 1
                continue
            p["id"] = "%s-%s" % (first_key, pid)
            p["source"] = first_key
            moved += 1
        out.append(p)
    if moved or dropped:
        log("migrated %d existing posts to '%s'%s"
            % (moved, first_key, ", dropped %d legacy X posts" % dropped if dropped else ""))
    return out


def high_water(posts, key):
    """Newest message number we already hold for this source."""
    best = 0
    prefix = key + "-"
    for p in posts:
        pid = str(p.get("id", ""))
        if pid.startswith(prefix):
            tail = pid[len(prefix):]
            if tail.isdigit():
                best = max(best, int(tail))
    return best


# ── one source ─────────────────────────────────────────────────────────────

def harvest(key, channel, label, since):
    cold = since == 0
    pages = BACKFILL_PAGES if cold else INCREMENTAL_PAGES
    log("%s (@%s): %s" % (label, channel,
                          "no history — backfilling %d pages" % pages if cold
                          else "looking for posts newer than %d" % since))

    fresh, seen_any, cursor = [], False, None
    for page in range(pages):
        page_html = get(channel, cursor)
        if page_html is None:
            break
        batch = parse(page_html, key)
        if not batch:
            log("    page %d: nothing returned" % (page + 1))
            break
        seen_any = True
        new = [p for p in batch if p["num"] > since]
        fresh += new
        log("    page %d: %d posts, %d new" % (page + 1, len(batch), len(new)))
        cursor = min(p["num"] for p in batch)
        if not cold and not new:
            break                          # caught up
        if cursor <= 1:
            break
        time.sleep(0.7)                    # be a polite guest

    return fresh, seen_any


def main():
    srcs = sources()
    existing = migrate(load_existing(), srcs[0][0])
    known = {str(p.get("id")) for p in existing}

    all_fresh, reachable = [], 0
    for key, channel, label in srcs:
        fresh, ok = harvest(key, channel, label, high_water(existing, key))
        reachable += 1 if ok else 0
        all_fresh += [p for p in fresh if p["id"] not in known]

    if not reachable:
        die("no source was reachable — is Telegram up?")

    kept = [p for p in all_fresh if p["ts"] and len(p["text"]) >= MIN_LEN]
    for p in kept:
        p.pop("num", None)

    if not kept:
        log("no new posts")
        return

    merged = sorted(kept + existing, key=lambda p: p.get("ts") or "", reverse=True)[:MAX_POSTS]

    tmp = POSTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)
    os.replace(tmp, POSTS)                 # atomic — never a half-written file

    by_source = {}
    for p in kept:
        by_source[p["source"]] = by_source.get(p["source"], 0) + 1
    log("added %d new post%s (%s) — %d total"
        % (len(kept), "" if len(kept) == 1 else "s",
           ", ".join("%s: %d" % kv for kv in sorted(by_source.items())), len(merged)))
    for p in kept[:10]:
        log("  · [%s] %s" % (p["source"], p["text"].replace("\n", " ")[:80]))


if __name__ == "__main__":
    main()
