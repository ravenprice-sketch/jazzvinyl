#!/usr/bin/env python3
"""
Jazz Vinyl Reissue Monitor
==========================
Checks the official label stores for newly added jazz reissue LPs and sends a
notification (Discord / Telegram / e-mail) only when something NEW appears.

How it works
------------
Most audiophile reissue labels run their webshop on Shopify, which exposes a
clean JSON feed at  {store}/collections/{handle}/products.json . We read that
feed per label, compare product IDs against what we saw last time (seen.json),
and report anything new.

First run is treated as a BASELINE: it records what currently exists without
spamming you. From the second run on, only genuine additions are reported.

Configure notifications by setting the relevant environment variables (secrets).
You only need ONE channel; set whichever you like and leave the rest empty.
"""

import datetime
import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

import requests

STATE_FILE = Path(__file__).with_name("seen.json")
DATA_FILE = Path(__file__).with_name("data.json")       # full catalog for the app
BLURB_FILE = Path(__file__).with_name("manual_blurbs.json")  # hand-curated blurbs { id: text }
UA = {"User-Agent": "Mozilla/5.0 (compatible; JazzVinylMonitor/1.0)"}
TIMEOUT = 30

# ---------------------------------------------------------------------------
# Label sources.  All the jazz series below are jazz-only by definition, so
# every new title qualifies.
#
# `collection` = Shopify collection handle. If it ever 404s, the script falls
# back to the store-wide feed and filters by `keyword`, so a renamed handle
# won't silently break the monitor.
# ---------------------------------------------------------------------------
SOURCES = [
    {
        "id": "bluenote_tone_poet",
        "label": "Blue Note \u2014 Tone Poet Series",
        "base": "https://store.bluenote.com",
        "collection": "tone-poet-series",
        "keyword": "tone poet",
    },
    {
        "id": "bluenote_classic",
        "label": "Blue Note \u2014 Classic Vinyl Series",
        "base": "https://store.bluenote.com",
        "collection": "classic-vinyl-series",
        "keyword": "classic vinyl",
    },
    {
        "id": "craft_ojc",
        "label": "Craft Recordings \u2014 OJC Series",
        "base": "https://craftrecordings.com",
        "collection": "original-jazz-classics",
        "keyword": "original jazz classics",
    },
    {
        "id": "verve_acoustic",
        "label": "Verve \u2014 Acoustic Sounds Series",
        "base": "https://store.ververecords.com",
        "collection": "acoustic-sounds",
        "keyword": "acoustic sounds",
    },
    {
        "id": "verve_vault",
        "label": "Verve \u2014 Vault Series",
        "base": "https://store.ververecords.com",
        "collection": "verve-vault",
        "keyword": "vault",
    },
    {
        # Rhino High Fidelity is a genre-MIXED audiophile series (rock/pop/jazz),
        # so unlike the others it needs a genre-tag filter to keep jazz only.
        # The store is Shopify, so it uses the same products.json path; genre_tags
        # is applied in main() against each product's Shopify tags.
        "id": "rhino_hifi",
        "label": "Rhino \u2014 High Fidelity",
        "base": "https://store.rhino.com",
        "collection": "rhino-high-fidelity",
        "keyword": "rhino high fidelity",
        "genre_tags": ["jazz", "fusion", "avant-garde jazz"],
    },
]


def _tags_of(p):
    """Shopify tags come as a list or a comma-separated string; normalise to a
    lowercased list. Also fold in product_type, since some stores put the genre
    there rather than in tags."""
    t = p.get("tags")
    if isinstance(t, str):
        t = [x.strip() for x in t.split(",")]
    out = [str(x).lower() for x in (t or [])]
    pt = p.get("product_type")
    if pt:
        out.append(str(pt).lower())
    return out


def matches_genre(src, p):
    """For a genre-mixed source (has 'genre_tags'), keep only products whose
    Shopify tags/type CONTAIN one of the wanted genre words. Substring match (not
    exact) so 'Jazz/Fusion', 'jazz-blues', 'Jazz ' etc. still match. Sources
    without genre_tags pass everything (unchanged behaviour)."""
    wanted = src.get("genre_tags")
    if not wanted:
        return True
    hay = _tags_of(p)
    return any(w in tag for tag in hay for w in wanted)

# ---------------------------------------------------------------------------
# Analogue Productions (Acoustic Sounds) -- NOT Shopify.
# Acoustic Sounds runs ColdFusion; there is no products.json. The jazz-vinyl
# results page is server-rendered HTML, which we parse below. Filters:
#   labelid=507 (the Analogue Productions label -- canonical, per the store's own
#   /l/507/Analogue_Productions page), CategoryID=5 (Vinyl), GenreID=4 (Jazz).
#   100/page, latest-added first.
# (An earlier version used saleid=448, a sale/promo grouping that happened to
#  work; labelid=507 is the precise catalog label.)
# NOTE: Acoustic Sounds redirects some non-browser clients to the contact page.
# fetch_ap() raises loudly on redirect or zero products so the monitor never
# silently records an empty AP baseline.
AP_BASE = "https://store.acousticsounds.com"
AP_LABEL = "Analogue Productions \u2014 Jazz Vinyl"
AP_ID = "analogue_productions"
AP_RESULTS = (
    "/index.cfm?get=results&labelid=507&CategoryID=5&GenreID=4"
    "&ResultsPerPage=100&orderby=preownedbinmodified2_dt%20desc"
)
AP_UA = {"User-Agent": (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)}

import html as _html  # stdlib; only used by the AP parser

_AP_CARD_SPLIT = re.compile(r'<div class="col-xs-6 col-sm-3 col-md-3 col-lg-3">')
_AP_LINK = re.compile(r'href="(https://store\.acousticsounds\.com/d/(\d+)/[^"]+)"')
_AP_ARTIST_TITLE = re.compile(
    r'<h4 class="h5"[^>]*><strong>(.*?)</strong>\s*/\s*(.*?)</h4>', re.DOTALL)
_AP_FORMAT = re.compile(r'<h4 class="h5"[^>]*>(?!<strong>)(.*?)</h4>', re.DOTALL)
_AP_CATALOG = re.compile(r'<span class="h6"[^>]*><strong>(.*?)</strong></span>', re.DOTALL)
_AP_PRICE = re.compile(r'<span class="h3">\$([\d,]+\.\d{2})</span>')
_AP_ADDED = re.compile(r'Added\s+\w+,\s+(\w+ \d{1,2}, \d{4})')
_AP_IMG = re.compile(r'<img[^>]+src="(https://store\.acousticsounds\.com/images/[^"]+)"')


def _ap_clean(s):
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def _ap_added_iso(s):
    try:
        return datetime.datetime.strptime(s, "%B %d, %Y").date().isoformat()
    except ValueError:
        return ""


def _ap_parse_page(html_text):
    """Extract raw product dicts from one AP results page of HTML."""
    start = html_text.find('id="results"')
    region = html_text[start:] if start != -1 else html_text
    out = []
    for card in _AP_CARD_SPLIT.split(region)[1:]:
        link = _AP_LINK.search(card)
        at = _AP_ARTIST_TITLE.search(card)
        if not link or not at:
            continue
        url, pid = link.group(1), link.group(2)
        fmt = _AP_FORMAT.search(card)
        cat = _AP_CATALOG.search(card)
        price = _AP_PRICE.search(card)
        added = _AP_ADDED.search(card)
        img = _AP_IMG.search(card)
        img_url = img.group(1) if img else ""
        if "NoImage" in img_url:
            img_url = ""
        elif "/images/small/" in img_url:
            img_url = img_url.replace("/images/small/", "/images/large/")
        out.append({
            "pid": pid,
            "artist": _ap_clean(at.group(1)),
            "title": _ap_clean(at.group(2)),
            "format": _ap_clean(fmt.group(1)) if fmt else "",
            "catalog": _ap_clean(cat.group(1)) if cat else "",
            "price": price.group(1).replace(",", "") if price else None,
            "url": url,
            "added": _ap_added_iso(added.group(1)) if added else "",
            "image": img_url,
        })
    return out


def fetch_ap():
    """Page through AP jazz-vinyl results. Raises on redirect/bounce so an empty
    AP source is never silently recorded as a baseline."""
    raw = []
    seen = set()
    for page in range(5):                       # 100/page; ~200 titles -> 2-3 pages
        start = 1 + page * 100
        r = requests.get(f"{AP_BASE}{AP_RESULTS}&start={start}",
                         headers=AP_UA, timeout=TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        if "get=contact" in r.url or "get=login" in r.url:
            raise RuntimeError(
                f"AP fetch redirected to {r.url!r} -- runner is being bounced "
                f"by Acoustic Sounds; cannot scrape AP from here.")
        items = _ap_parse_page(r.text)
        if not items:
            break
        fresh = [it for it in items if it["pid"] not in seen]
        for it in fresh:
            seen.add(it["pid"])
        raw.extend(fresh)
        if len(items) < 100:
            break
    if not raw:
        raise RuntimeError(
            "AP parsed ZERO products -- page shape changed or runner served a "
            "non-results page. Refusing to record an empty AP baseline.")
    return raw


def simplify_ap(p):
    """Normalise an AP raw dict into the same schema simplify() emits, so AP
    items flow through diff/first_seen/blurbs/app exactly like Shopify items."""
    title = f"{p['artist']} \u2014 {p['title']}".strip(" \u2014")
    return {
        "id": f"ap_{p['pid']}",            # namespaced; never collides with Shopify ids
        "label_id": AP_ID,
        "title": title,
        "url": p["url"],
        "price": p["price"],
        "image": p.get("image") or None,  # large cover if present, else None
        "published_at": None,
        "created_at": p["added"],          # store-listing date -> feeds first_seen, not release_date
        "specs": _specs_from(f"{title} {p['format']}"),
        "preorder": "pre order" in p["format"].lower() or "preorder" in p["format"].lower(),
        "catalog": p["catalog"],
    }


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _get_json(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_products(src):
    """Return a list of product dicts for one label, with handle fallback."""
    # Primary: the specific collection feed (paginated, 250/page).
    products = []
    try:
        for page in range(1, 6):  # up to 1250 items, plenty
            url = f"{src['base']}/collections/{src['collection']}/products.json?limit=250&page={page}"
            batch = _get_json(url).get("products", [])
            if not batch:
                break
            products.extend(batch)
        if products:
            return products
    except Exception as e:  # 404 / network / parse -> try fallback
        print(f"  [{src['id']}] collection feed failed ({e}); trying store-wide feed")

    # Fallback: whole store, filtered by keyword in title/type/tags.
    kw = src["keyword"].lower()
    store_products = []
    for page in range(1, 11):
        url = f"{src['base']}/products.json?limit=250&page={page}"
        batch = _get_json(url).get("products", [])
        if not batch:
            break
        store_products.extend(batch)
    filtered = []
    for p in store_products:
        hay = " ".join(
            [p.get("title", ""), p.get("product_type", "")] + (p.get("tags") or [])
        ).lower()
        if kw in hay:
            filtered.append(p)
    return filtered


def _specs_from(text):
    t = (text or "").lower()
    s = []
    if re.search(r"\b(aaa|all[\s-]?analog(ue)?)\b", t): s.append("AAA")
    if re.search(r"200\s?-?\s?(g|gram|gr)\b", t): s.append("200g")
    elif re.search(r"180\s?-?\s?(g|gram|gr)\b", t): s.append("180g")
    if re.search(r"\b45\s?-?\s?rpm\b", t): s.append("45 RPM")
    if re.search(r"\bmono\b", t): s.append("Mono")
    if re.search(r"\bgatefold\b", t): s.append("Gatefold")
    return s


def is_lp(title):
    """True only for vinyl LPs -- excludes digital albums, CDs, t-shirts,
    test pressings, and other non-LP merch so the app lists records only."""
    t = (title or "").lower()
    bad = ("digital album", "(digital", "digital)", "test pressing",
           "t-shirt", "tshirt", "shirt", "hoodie", "poster", "slipmat",
           "bundle")
    if any(b in t for b in bad):
        return False
    if t.rstrip().endswith(" cd") or "(cd" in t:
        return False
    return True


def simplify(src, p):
    """Pull the fields we care about out of a raw Shopify product."""
    variants = p.get("variants") or []
    price = variants[0].get("price") if variants else None
    images = p.get("images") or []
    image = images[0].get("src") if images else None
    handle = p.get("handle", "")
    body = p.get("body_html", "") or ""
    blob = f"{p.get('title','')} {' '.join(p.get('tags') or [])} {body}".lower()
    any_available = any(v.get("available") for v in variants)
    created = p.get("created_at") or p.get("published_at") or ""
    return {
        "id": str(p.get("id")),
        "label_id": src["id"],
        "title": p.get("title", "").strip(),
        "url": f"{src['base']}/products/{handle}" if handle else src["base"],
        "price": price,
        "image": image,
        "published_at": p.get("published_at"),
        "created_at": created,
        "specs": _specs_from(f"{p.get('title','')} {' '.join(p.get('tags') or [])} {body}"),
        "preorder": ("pre-order" in blob or "preorder" in blob or not any_available),
    }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Notifications  (set only the channel you want via env vars)
# ---------------------------------------------------------------------------
def fmt_item(it):
    price = f" \u2014 ${it['price']}" if it.get("price") else ""
    avail = it.get("availability")
    tag = f"  [{avail}]" if avail and avail.lower() != "in stock" else ""
    return f"\u2022 {it['title']}{price}{tag}\n  {it['url']}"


def send_discord(text):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    # Discord caps content at 2000 chars; chunk if needed.
    for chunk in _chunks(text, 1900):
        requests.post(url, json={"content": chunk}, timeout=TIMEOUT)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    for chunk in _chunks(text, 3900):
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": chunk, "disable_web_page_preview": True},
            timeout=TIMEOUT,
        )


def send_email(subject, text):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    to = os.environ.get("EMAIL_TO")
    if not (host and user and pwd and to):
        return
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = MIMEText(text, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())


def _chunks(s, n):
    lines, buf = s.split("\n"), ""
    for line in lines:
        if len(buf) + len(line) + 1 > n:
            yield buf
            buf = ""
        buf += line + "\n"
    if buf.strip():
        yield buf


def notify(subject, body):
    print("\n" + subject + "\n" + body)  # always log
    send_discord(f"**{subject}**\n{body}")
    send_telegram(f"{subject}\n{body}")
    send_email(subject, body)


# ---------------------------------------------------------------------------
# Hand-curated blurbs
# ---------------------------------------------------------------------------
# Blurbs are written on demand (a consensus summary for a specific title) and
# kept in manual_blurbs.json as { release_id: text }. There are no automatic
# AI calls anywhere in this script. To add one, put the text under the
# release's id in manual_blurbs.json; it shows in the app on the next run.
def load_blurbs():
    if BLURB_FILE.exists():
        try:
            return json.loads(BLURB_FILE.read_text())
        except Exception:
            return {}
    return {}


def write_data_json(catalog):
    """Write the full catalog (all current items + blurbs) for the app to read."""
    payload = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "count": len(catalog),
        "items": catalog,
    }
    DATA_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"Wrote {DATA_FILE.name} ({len(catalog)} items).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def diff_source(state, sid, label, items, all_new):
    """Update state for one source; record new items. Returns True if changed."""
    prev = state.get(sid)
    if prev is None:                      # first run -> baseline, no alert
        state[sid] = items
        print(f"  baseline recorded ({len(items)} items, no alert)")
        return True
    new_ids = [i for i in items if i not in prev]
    if new_ids:
        all_new.append((label, [items[i] for i in new_ids]))
        print(f"  {len(new_ids)} NEW")
    merged = dict(prev)                   # keep old ids so sold-out won't re-alert
    merged.update(items)
    if merged != prev:
        state[sid] = merged
        return True
    return False


# first_seen: the date THIS tracker first encountered each release id. Stored
# under the reserved "_first_seen" key in seen.json so it persists across runs
# and is never overwritten. This is what the app sorts on -- unlike Shopify's
# created_at/published_at (store-listing dates that jump around on re-publish),
# first_seen reliably puts genuinely newly-detected releases on top. On the very
# first run after this is added, everything gets stamped "now" (one-time
# backfill); from then on only truly new ids get a fresh stamp.
def stamp_first_seen(state, catalog):
    fs = state.setdefault("_first_seen", {})
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for it in catalog:
        rid = it["id"]
        if rid not in fs:
            fs[rid] = now
        it["first_seen"] = fs[rid]


def main():
    state = load_state()
    blurbs = load_blurbs()
    all_new = []        # list of (label, [items])
    catalog = []        # every current item across all sources, for the app
    changed = False

    # Shopify-based label feeds.
    for src in SOURCES:
        print(f"Checking {src['label']} ...")
        try:
            raw = fetch_products(src)
        except Exception as e:
            print(f"  ERROR fetching {src['id']}: {e}")
            continue
        if src.get("genre_tags"):
            print(f"  fetched {len(raw)} raw products before genre filter")
        # Genre-mixed sources (e.g. Rhino Hi-Fi) keep only jazz-tagged products;
        # all other sources pass everything through (matches_genre returns True).
        if src.get("genre_tags"):
            raw_all = raw
            raw = [p for p in raw_all if matches_genre(src, p)]
            if raw_all and not raw:
                # Fetched products but genre filter dropped all of them. Log the
                # actual tags we saw so we can see what the real genre strings are.
                sample = []
                for p in raw_all[:8]:
                    sample.append(f"{(p.get('title') or '')[:30]} :: tags={p.get('tags')} type={p.get('product_type')}")
                print(f"  genre filter matched 0 of {len(raw_all)}; "
                      f"wanted={src['genre_tags']}; sample of what was seen:")
                for s in sample:
                    print(f"    {s}")
        items = {it["id"]: it for it in (simplify(src, p) for p in raw)
                 if is_lp(it["title"])}
        print(f"  found {len(items)} LPs")
        for it in items.values():
            it["label_name"] = src["label"]
            catalog.append(it)
        if diff_source(state, src["id"], src["label"], items, all_new):
            changed = True

    # Analogue Productions (HTML scrape, not Shopify). Same schema, same flow.
    # Wrapped so an AP failure never aborts the Shopify-based catalog: it logs
    # loudly and skips AP for this run.
    print(f"Checking {AP_LABEL} ...")
    try:
        ap_raw = fetch_ap()
        ap_items = {}
        for p in ap_raw:
            it = simplify_ap(p)
            # CategoryID=5 already limits to vinyl; drop test pressings within it.
            if "test pressing" in p["format"].lower():
                continue
            it["label_name"] = AP_LABEL
            ap_items[it["id"]] = it
        print(f"  found {len(ap_items)} LPs")
        for it in ap_items.values():
            catalog.append(it)
        if diff_source(state, AP_ID, AP_LABEL, ap_items, all_new):
            changed = True
    except Exception as e:
        print(f"  ERROR fetching {AP_ID}: {e}  (skipping AP this run)")

    # Attach hand-curated blurbs. There are NO automatic AI calls: blurbs are
    # written on demand (a consensus summary for a specific title) and stored in
    # manual_blurbs.json as { release_id: text }. Merging here means curated
    # blurbs survive every catalog refresh instead of being overwritten.
    for it in catalog:
        it["blurb"] = blurbs.get(it["id"], "")
    print(f"Attached {sum(1 for it in catalog if it['blurb'])} curated blurb(s).")

    # Stamp each item with first_seen (when this tracker first saw it). New ids
    # get "now"; existing ids keep their original stamp. The app sorts on this.
    before = len(state.get("_first_seen", {}))
    stamp_first_seen(state, catalog)
    if len(state["_first_seen"]) != before:
        changed = True   # new stamps -> persist state

    # Write the catalog file the app reads.
    write_data_json(catalog)

    # Email/notify only about genuinely new releases (unchanged behaviour).
    if all_new:
        sections = []
        total = 0
        for label, items in all_new:
            total += len(items)
            body = "\n".join(fmt_item(it) for it in items)
            sections.append(f"\u25b6 {label}\n{body}")
        subject = f"\U0001f3b7 {total} new jazz vinyl reissue(s)"
        notify(subject, "\n\n".join(sections))
    else:
        print("\nNo new releases.")

    if changed:
        save_state(state)
        print("State updated.")


if __name__ == "__main__":
    sys.exit(main())
