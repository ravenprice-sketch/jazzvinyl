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
import time
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
        # Rhino High Fidelity: genre-MIXED audiophile series (rock/pop/jazz) on
        # Rhino's own Shopify store. Feed carries NO genre tags, so jazz is found
        # by a cached one-shot AI title-classification (see classify_genre_ai).
        # 'ai_genre' enables it; 'lp_types' drops reel-to-reel/bundles.
        # Paired with the Acoustic Sounds Rhino source below; the catalog is then
        # de-duplicated on normalised title so overlapping titles appear once.
        "id": "rhino_jazz",
        "label": "Rhino \u2014 High Fidelity",
        "base": "https://store.rhino.com",
        "collection": "rhino-high-fidelity",
        "keyword": "rhino high fidelity",
        "ai_genre": "jazz",
        "lp_types": ["vinyl"],
    },
]

# ---------------------------------------------------------------------------
# AI genre classification (for genre-mixed Shopify sources like Rhino Hi-Fi,
# whose feed carries no genre tags). One cached, batched Anthropic call; each
# product id is judged once and cached in state, so later runs only classify
# genuinely new titles. Degrades safely: no key / failed call -> classify
# nothing this run (never crashes, never lets non-jazz leak in).
# ---------------------------------------------------------------------------
RHINO_GENRE_OVERRIDES = {
    # "product_id": True,   # force-keep   (model said no, you say yes)
    # "product_id": False,  # force-drop   (model said yes, you say no)
}


def _lp_type_ok(src, p):
    wanted = src.get("lp_types")
    if not wanted:
        return True
    pt = str(p.get("product_type") or "").lower()
    return any(w in pt for w in wanted)


def _ai_title(p):
    t = (p.get("title") or "").strip()
    t = re.sub(r"\s*\((?:Rhino High Fidelity|Audiophile Bundle)[^)]*\)\s*$", "", t, flags=re.I)
    vendor = (p.get("vendor") or "").strip()
    return f"{vendor} - {t}".strip(" -") if vendor else t


def classify_genre_ai(src, products, state):
    want = src["ai_genre"]
    cache = state.setdefault("_ai_genre", {}).setdefault(src["id"], {})
    for pid, val in RHINO_GENRE_OVERRIDES.items():
        cache[pid] = bool(val)

    pending = [p for p in products if str(p.get("id")) not in cache]
    if pending:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            print(f"  [ai_genre] {len(pending)} unclassified but no ANTHROPIC_API_KEY "
                  f"-> classifying nothing this run")
        else:
            numbered = "\n".join(f"{i}. {_ai_title(p)}" for i, p in enumerate(pending))
            prompt = (
                f"Below is a numbered list of album reissues (artist - title). For each, "
                f"decide whether its PRIMARY musical genre is {want}. Be strict: only albums "
                f"whose core genre is {want} count (for jazz: bebop, hard bop, cool, modal, "
                f"fusion, spiritual/avant-garde jazz). Soul, funk, R&B, rock, pop, folk, "
                f"country do NOT count, even if jazz-adjacent.\n\n{numbered}\n\n"
                f'Reply with ONLY a JSON object: {{"match": [numbers that are {want}]}} '
                f"and nothing else."
            )
            try:
                r = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 500,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=TIMEOUT,
                )
                r.raise_for_status()
                text = "".join(b.get("text", "") for b in r.json().get("content", [])).strip().strip("`")
                if text.lower().startswith("json"):
                    text = text[4:].strip()
                match_nums = set(json.loads(text).get("match", []))
                for i, p in enumerate(pending):
                    cache[str(p.get("id"))] = (i in match_nums)
                kept = sum(1 for i in range(len(pending)) if i in match_nums)
                print(f"  [ai_genre] classified {len(pending)} new: {kept} {want}, {len(pending)-kept} other")
            except Exception as e:
                print(f"  [ai_genre] classification failed ({e}) -> classifying nothing this run")

    for pid, val in RHINO_GENRE_OVERRIDES.items():
        cache[pid] = bool(val)
    return [p for p in products if cache.get(str(p.get("id"))) is True]


# ---------------------------------------------------------------------------
# Acoustic Sounds (store.acousticsounds.com) -- NOT Shopify.
# Acoustic Sounds runs ColdFusion; there is no products.json. The jazz-vinyl
# results page is server-rendered HTML, which we parse below. Each label is
# scoped by a numeric labelid crossed with CategoryID=5 (Vinyl) + GenreID=4
# (Jazz), so genre comes PRE-TAGGED by the store -- no classification needed.
# Multiple labels (Analogue Productions, Rhino) share the same parser; they
# differ only by labelid / label name / id namespace.
# NOTE: Acoustic Sounds redirects some non-browser clients to the contact page.
# fetch_acoustic() raises loudly on redirect or zero products so the monitor
# never silently records an empty Acoustic Sounds source as a baseline.
AS_BASE = "https://store.acousticsounds.com"
AS_UA = {"User-Agent": (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)}

# Each Acoustic Sounds label source. labelid values are the store's own:
#   507  = Analogue Productions  (per /l/507/Analogue_Productions)
#   531  = Rhino                 (per /l/531/Rhino; jazz slice via GenreID=4)
ACOUSTIC_SOURCES = [
    {
        "id": "analogue_productions",
        "label": "Analogue Productions \u2014 Jazz Vinyl",
        "labelid": 507,
        "prefix": "ap",                # id namespace -> ap_{pid}
    },
    {
        # Unique state id (rhino_as) so it doesn't collide with the Shopify
        # Rhino source in seen.json, but shares label_id 'rhino_jazz' so both
        # Rhino sources appear under one label/pill in the app.
        "id": "rhino_as",
        "label_id": "rhino_jazz",
        "label": "Rhino \u2014 Jazz Vinyl",
        "labelid": 531,
        "prefix": "rh",                # id namespace -> rh_{pid}
    },
]


def _as_results_url(labelid, start):
    return (f"{AS_BASE}/index.cfm?get=results&labelid={labelid}"
            f"&CategoryID=5&GenreID=4&ResultsPerPage=100"
            f"&orderby=preownedbinmodified2_dt%20desc&start={start}")



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


def fetch_acoustic(src):
    """Page through one Acoustic Sounds label's jazz-vinyl results. Raises on
    redirect/bounce or zero products so an empty source is never recorded as a
    baseline."""
    raw = []
    seen = set()
    for page in range(5):                       # 100/page
        start = 1 + page * 100
        url = _as_results_url(src["labelid"], start)
        # Acoustic Sounds occasionally times out / refuses a connection from the
        # runner; retry a few times with backoff before giving up on the source.
        r = None
        last_err = None
        for attempt in range(4):
            try:
                r = requests.get(url, headers=AS_UA, timeout=45, allow_redirects=True)
                r.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                last_err = e
                if attempt < 3:
                    wait = 5 * (attempt + 1)     # 5s, 10s, 15s
                    print(f"  [{src['id']}] attempt {attempt+1} failed ({type(e).__name__}); "
                          f"retrying in {wait}s")
                    time.sleep(wait)
        if r is None:
            raise RuntimeError(f"{src['id']} fetch failed after retries: {last_err}")
        if "get=contact" in r.url or "get=login" in r.url:
            raise RuntimeError(
                f"{src['id']} fetch redirected to {r.url!r} -- runner is being "
                f"bounced by Acoustic Sounds; cannot scrape from here.")
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
            f"{src['id']} parsed ZERO products -- page shape changed or runner "
            f"served a non-results page. Refusing to record an empty baseline.")
    return raw


def simplify_acoustic(src, p):
    """Normalise an Acoustic Sounds raw dict into the same schema simplify()
    emits, so items flow through diff/first_seen/blurbs/app like Shopify items."""
    title = f"{p['artist']} \u2014 {p['title']}".strip(" \u2014")
    return {
        "id": f"{src['prefix']}_{p['pid']}",   # namespaced; never collides
        "label_id": src.get("label_id", src["id"]),
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


def _norm_title(t):
    """Normalise a title for cross-source dedup: lowercase, strip the trailing
    label/series suffix in parens, collapse punctuation/whitespace."""
    t = (t or "").lower()
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t)        # drop trailing "(...series...)"
    t = t.replace("\u2014", " ").replace("-", " ")
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def write_data_json(catalog):
    """Write the full catalog (all current items + blurbs) for the app to read.
    Dedups on id, then on normalised title, so a record reaching the catalog via
    more than one path (e.g. a Rhino title on both Rhino's store and Acoustic
    Sounds) appears once. First occurrence wins -- sources earlier in catalog
    order (Shopify, with native covers/URLs) take priority over later ones."""
    seen_ids = set()
    kept_titles = {}            # label_id -> list of normalised titles kept
    deduped = []
    for it in catalog:
        iid = it.get("id")
        if iid in seen_ids:
            continue
        lbl = it.get("label_id")
        nt = _norm_title(it.get("title"))
        # Within the same label, treat titles where one contains the other as the
        # same record (handles "Coltrane's Sound" vs "John Coltrane - Coltrane's
        # Sound" across the two Rhino sources). First occurrence wins.
        existing = kept_titles.setdefault(lbl, [])
        if nt and any(nt in e or e in nt for e in existing):
            continue
        seen_ids.add(iid)
        existing.append(nt)
        deduped.append(it)
    dropped = len(catalog) - len(deduped)
    if dropped:
        print(f"  (deduped {dropped} duplicate item(s) on id/title)")
    payload = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "count": len(deduped),
        "items": deduped,
    }
    DATA_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"Wrote {DATA_FILE.name} ({len(deduped)} items).")


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
        # Genre-mixed source (Rhino Hi-Fi): drop non-LP types, then keep only the
        # wanted genre via cached AI title-classification. Others skip both steps.
        if src.get("ai_genre"):
            n0 = len(raw)
            raw = [p for p in raw if _lp_type_ok(src, p)]
            print(f"  fetched {n0} raw; {len(raw)} vinyl LPs; classifying genre ...")
            raw = classify_genre_ai(src, raw, state)
            print(f"  {len(raw)} classified as {src['ai_genre']}")
        label_id = src.get("label_id", src["id"])
        items = {}
        for p in raw:
            it = simplify(src, p)
            if not is_lp(it["title"]):
                continue
            it["label_id"] = label_id
            items[it["id"]] = it
        print(f"  found {len(items)} LPs")
        for it in items.values():
            it["label_name"] = src["label"]
            catalog.append(it)
        if diff_source(state, src["id"], src["label"], items, all_new):
            changed = True

    # Acoustic Sounds labels (HTML scrape, not Shopify). Same schema, same flow.
    # Each is wrapped so one label's failure never aborts the others or the
    # Shopify sources: it logs loudly and skips that label this run.
    for src in ACOUSTIC_SOURCES:
        print(f"Checking {src['label']} ...")
        try:
            raw = fetch_acoustic(src)
            items = {}
            for p in raw:
                it = simplify_acoustic(src, p)
                # CategoryID=5 already limits to vinyl; drop test pressings within it.
                if "test pressing" in p["format"].lower():
                    continue
                it["label_name"] = src["label"]
                items[it["id"]] = it
            print(f"  found {len(items)} LPs")
            for it in items.values():
                catalog.append(it)
            if diff_source(state, src["id"], src["label"], items, all_new):
                changed = True
        except Exception as e:
            print(f"  ERROR fetching {src['id']}: {e}  (skipping this run)")

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
