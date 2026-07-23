#!/usr/bin/env python3
"""
Sync BN24 app content from boxingnews24.com.

Scrapes the live RSS feed plus the schedule, results, and rankings pages on
boxingnews24.com, regenerates the `const DATA = {...};` payload embedded in
index.html, and writes it back in place (byte-for-byte preserving everything
else in the file).

Design notes:
- Pure stdlib + Pillow (for image downscaling). No requests/bs4/feedparser.
- Each of the four sections (feed, schedule, results, rankings) is scraped
  and validated independently. If a section's source page can't be parsed
  confidently, that section's last-known-good data is kept untouched and a
  clear warning is printed to stdout/stderr so the GitHub Actions log shows
  it. Nothing is ever fabricated.
"""
import base64
import html
import io
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from PIL import Image

BASE = "https://www.boxingnews24.com"
FEED_URL = f"{BASE}/feed/"
SCHEDULE_URL = f"{BASE}/boxing-schedule/"
RESULTS_URL = f"{BASE}/boxing-results-who-won-tonight/"
RANKINGS_URL = f"{BASE}/boxing-ratings-rankings-wbo-wbc-ibf/"

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX_PATH = REPO_ROOT / "index.html"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 BN24-app-sync/1.0"
)

FEED_LIMIT = 15
RESULTS_LIMIT = 40

CANONICAL_DIVISIONS = [
    "Heavyweight", "Cruiserweight", "Light Heavyweight", "Super Middleweight",
    "Middleweight", "Jr. Middleweight", "Welterweight", "Jr. Welterweight",
    "Lightweight", "Jr. Lightweight", "Featherweight", "Jr. Featherweight",
    "Bantamweight", "Jr. Bantamweight", "Flyweight", "Jr. Flyweight",
    "Strawweight",
]


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_text(url, timeout=25):
    return fetch(url, timeout=timeout).decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------

def strip_tags(s):
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def canon_div_key(name):
    key = name.lower().replace("junior", "jr")
    key = re.sub(r"[^a-z]", "", key)
    return key


_CANON_MAP = {canon_div_key(n): n for n in CANONICAL_DIVISIONS}


def normalize_division_name(raw):
    key = canon_div_key(raw)
    return _CANON_MAP.get(key, raw.strip())


# --------------------------------------------------------------------------
# FEED (RSS)
# --------------------------------------------------------------------------

def _tag(raw, name):
    m = re.search(
        rf"<{name}>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</{name}>", raw, re.S
    )
    if not m:
        return ""
    return (m.group(1) if m.group(1) is not None else m.group(2)).strip()


def derive_tag_from_categories(categories, title):
    clean = []
    for c in categories:
        cl = c.lower()
        if cl in ("latest", "news", "boxing"):
            continue
        if " vs" in cl or "vs." in cl:
            continue
        clean.append(c)
    if clean:
        return clean[-1]
    # Fallback: last capitalized 2-3 word proper-noun phrase in the title.
    names = re.findall(
        r"\b([A-Z][a-zA-Z'.]*(?:\s+[A-Z][a-zA-Z'.]*){1,2})\b", title
    )
    return names[-1] if names else ""


def parse_feed(xml_text, limit=FEED_LIMIT):
    raw_items = re.findall(r"<item>(.*?)</item>", xml_text, re.S)
    out = []
    for raw in raw_items[:limit]:
        title = html.unescape(_tag(raw, "title"))
        link = _tag(raw, "link")
        author = html.unescape(_tag(raw, "dc:creator"))
        pub = _tag(raw, "pubDate")
        categories = [
            html.unescape((a or b).strip())
            for a, b in re.findall(
                r"<category>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</category>", raw, re.S
            )
        ]
        desc = _tag(raw, "description")

        img_m = re.search(r'<img[^>]+src="([^"]+)"', desc)
        img = img_m.group(1) if img_m else ""

        body = re.sub(r"^\s*<p>\s*<img[^>]*>\s*</p>", "", desc, count=1)
        cut = body.find('<a class="read-more"')
        if cut == -1:
            cut = body.find("<a title=")
        excerpt_html = body[:cut] if cut != -1 else body
        excerpt_text = strip_tags(excerpt_html).rstrip(". ").strip()
        excerpt = f"{excerpt_text}.…" if excerpt_text else ""

        date_label = ""
        time_label = ""
        try:
            dt = parsedate_to_datetime(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            date_label = f"{dt.strftime('%b')} {dt.day}"
            hour12 = dt.hour % 12 or 12
            ampm = "AM" if dt.hour < 12 else "PM"
            time_label = f"{hour12}:{dt.minute:02d} {ampm} UTC"
        except Exception:
            pass

        tag_val = derive_tag_from_categories(categories, title)

        if not title or not link:
            continue

        out.append(
            {
                "title": title,
                "link": link,
                "author": author,
                "pub": pub,
                "img": img,
                "excerpt": excerpt,
                "img64": "",  # filled in by caller
                "dateLabel": date_label,
                "timeLabel": time_label,
                "tag": tag_val,
            }
        )
    return out


def make_img64(url, max_w=480, max_h=320, quality=72):
    try:
        raw = fetch(url)
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGB")
        im.thumbnail((max_w, max_h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        print(f"WARNING: failed to fetch/encode image {url}: {e}", file=sys.stderr)
        return ""


# --------------------------------------------------------------------------
# SCHEDULE
# --------------------------------------------------------------------------

def parse_schedule_header(text):
    parts = [p.strip() for p in text.split("|")]
    if not parts:
        return None
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}):\s*(.+)$", parts[0])
    if not m:
        return None
    month_name, day_str, place = m.group(1), m.group(2), m.group(3).strip()
    mon = month_name[:3].upper()
    try:
        day = int(day_str)
    except ValueError:
        return None

    et = ""
    uk = ""
    tv = ""
    for p in parts[1:]:
        pl = p.lower()
        if pl.startswith("usa et"):
            mt = re.search(r"(\d{1,2}:\d{2}\s*[AP]M)", p)
            if mt:
                et = mt.group(1)
        elif pl.startswith("uk london"):
            mt = re.search(r"(\d{1,2}:\d{2}\s*[AP]M)", p)
            if mt:
                uk = mt.group(1)
        elif pl.startswith("local"):
            continue
        else:
            mt = re.search(r"live on ([^|]+)", p, re.I)
            if mt:
                val = mt.group(1).split(",")[0]
                val = re.sub(r"\bPPV\b\.?\s*$", "", val, flags=re.I).strip()
                tv = val
            elif "ppv" in pl and not tv:
                tv = "PPV"

    if not et or not uk:
        return None
    return {"mon": mon, "day": day, "place": place, "et": et, "uk": uk, "tv": tv}


def parse_schedule_bouts(segment):
    bouts = []
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", segment, re.S):
        content = m.group(1)
        if "\U0001F4CC" not in content:  # 📌
            continue
        for line in re.split(r"<br\b[^>]*>", content):
            text = strip_tags(line).lstrip("\U0001F4CC").strip()
            if text:
                bouts.append({"text": text, "title": "title" in text.lower()})
    for m in re.finditer(r"<li[^>]*>(.*?)</li>", segment, re.S):
        text = strip_tags(m.group(1))
        if text and " vs" in text.lower():
            bouts.append({"text": text, "title": "title" in text.lower()})
    return bouts


def parse_schedule(html_text):
    idx = html_text.find('class="entry-content"')
    if idx == -1:
        raise ValueError("entry-content block not found on schedule page")
    end_idx = html_text.find("ss-inline-share-wrapper", idx)
    if end_idx == -1:
        end_idx = len(html_text)
    block = html_text[idx:end_idx]

    h2_matches = list(re.finditer(r"<h2[^>]*>(.*?)</h2>", block, re.S))
    if not h2_matches:
        raise ValueError("no <h2> date/venue headers found on schedule page")

    cards = []
    for i, m in enumerate(h2_matches):
        header_text = strip_tags(m.group(1))
        card = parse_schedule_header(header_text)
        if card is None:
            continue
        seg_start = m.end()
        seg_end = h2_matches[i + 1].start() if i + 1 < len(h2_matches) else len(block)
        bouts = parse_schedule_bouts(block[seg_start:seg_end])
        if not bouts:
            continue
        card["bouts"] = bouts
        cards.append(card)
    return cards


# --------------------------------------------------------------------------
# RESULTS
# --------------------------------------------------------------------------

_KO_KEYWORDS = (
    "stops", "stopped", "stoppage", "tko", " ko ", "knocks out",
    "knockout", "blasts out", "blast out",
)


def classify_method(title):
    t = title.lower()
    if "live results" in t:
        return "CARD"
    if "draw" in t:
        return "DRAW"
    if any(k in t for k in _KO_KEYWORDS):
        return "KO/TKO"
    return "DEC"


def parse_results(html_text, limit=RESULTS_LIMIT):
    m = re.search(r'<ul class="lcp_catlist"[^>]*>(.*?)</ul>', html_text, re.S)
    if not m:
        raise ValueError("results list (ul.lcp_catlist) not found")
    block = m.group(1)

    items = re.findall(
        r'<li>\s*<a href="([^"]+)"[^>]*>(.*?)</a>\s*([\d/]{8,10})\s*</li>', block, re.S
    )
    if not items:
        raise ValueError("no <li> result rows parsed")

    out = []
    for link, title_html, date_str in items[:limit]:
        title = html.unescape(strip_tags(title_html))
        date_label = ""
        try:
            dt = datetime.strptime(date_str.strip(), "%m/%d/%Y")
            date_label = f"{dt.strftime('%b')} {dt.day}"
        except ValueError:
            pass
        out.append(
            {
                "title": title,
                "dateLabel": date_label,
                "link": link,
                "method": classify_method(title),
            }
        )
    return out


# --------------------------------------------------------------------------
# RANKINGS
# --------------------------------------------------------------------------

def parse_rankings(html_text):
    idx = html_text.find('class="entry-content"')
    if idx == -1:
        raise ValueError("entry-content block not found on rankings page")
    block = html_text[idx:]

    intro = ""
    m = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
    if m:
        intro = strip_tags(m.group(1))

    p4p = []
    p4p_m = re.search(r"P4P RANKINGS.*?<ol[^>]*>(.*?)</ol>", block, re.S | re.I)
    if p4p_m:
        for li in re.finditer(r"<li[^>]*>(.*?)</li>", p4p_m.group(1), re.S):
            text = strip_tags(li.group(1))
            fm = re.match(
                r"^(.*?)(\*)?\s*—\s*([A-Za-z]{2,4})\s*—\s*"
                r"([\d\-\(\) ]+)\s*—\s*(.+)$",
                text,
            )
            if not fm:
                continue
            name, champmark, cty, rec, div = fm.groups()
            p4p.append(
                {
                    "name": name.strip(),
                    "champ": bool(champmark),
                    "cty": cty.strip().upper(),
                    "rec": rec.strip(),
                    "div": normalize_division_name(div.strip()),
                }
            )
    if not p4p:
        raise ValueError("no P4P ranking rows parsed")

    divisions = []
    h1_matches = list(re.finditer(r"<h1[^>]*>(.*?)</h1>", block, re.S))
    for i, hm in enumerate(h1_matches):
        raw_name = strip_tags(hm.group(1))
        if "boxing rankings" in raw_name.lower():
            continue
        dm = re.match(r"^(.*?)\(([^/\)]+)", raw_name)
        if not dm:
            continue
        name = normalize_division_name(dm.group(1).strip())
        limit = dm.group(2).strip()

        seg_start = hm.end()
        seg_end = h1_matches[i + 1].start() if i + 1 < len(h1_matches) else len(block)
        segment = block[seg_start:seg_end]

        champ = "OPEN"
        h3m = re.search(r"<h3[^>]*>(.*?)</h3>", segment, re.S)
        if h3m:
            h3text = strip_tags(h3m.group(1))
            cm = re.search(r"Champion:\s*(.+?)\s*—\s*Last week", h3text)
            if cm:
                champ_val = cm.group(1).strip()
                champ = "OPEN" if champ_val.upper() == "OPEN" else champ_val

        fighters = []
        olm = re.search(r"<ol[^>]*>(.*?)</ol>", segment, re.S)
        if olm:
            for pos, li in enumerate(
                re.finditer(r"<li[^>]*>(.*?)</li>", olm.group(1), re.S), start=1
            ):
                text = strip_tags(li.group(1))
                fm = re.match(
                    r"^(.*?)\s*—\s*([\d\-\(\) ]+)\s*—\s*"
                    r"([A-Za-z]{2,4})\s*—\s*Last week:\s*(.+)$",
                    text,
                )
                if not fm:
                    continue
                fname, rec, cty, lastweek = fm.groups()
                lastweek = lastweek.strip()
                try:
                    lw_num = int(lastweek)
                    move = lw_num - pos
                    is_new = False
                except ValueError:
                    move = 0
                    is_new = True
                fighters.append(
                    {
                        "name": fname.strip(),
                        "rec": rec.strip(),
                        "cty": cty.strip().upper(),
                        "move": move,
                        "new": is_new,
                    }
                )
        if fighters:
            divisions.append(
                {"name": name, "limit": limit, "champ": champ, "fighters": fighters}
            )

    if len(divisions) < 10:
        raise ValueError(f"only parsed {len(divisions)} weight divisions")

    return {"intro": intro, "p4p": p4p, "divisions": divisions}


# --------------------------------------------------------------------------
# DATA block replacement
# --------------------------------------------------------------------------

def load_current_data(html_text):
    idx = html_text.find("const DATA = ")
    if idx == -1:
        raise ValueError("could not find 'const DATA = ' in index.html")
    start = idx + len("const DATA = ")
    dec = json.JSONDecoder()
    data, end = dec.raw_decode(html_text, start)
    return data, start, end


def replace_data_block(html_text, new_data):
    idx = html_text.find("const DATA = ")
    if idx == -1:
        raise ValueError("could not find 'const DATA = ' in index.html")
    start = idx + len("const DATA = ")
    dec = json.JSONDecoder()
    _, end = dec.raw_decode(html_text, start)
    new_json = json.dumps(new_data, ensure_ascii=False)
    return html_text[:start] + new_json + html_text[end:]


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    html_text = INDEX_PATH.read_text(encoding="utf-8")
    old_data, _, _ = load_current_data(html_text)

    new_data = {
        "updated": old_data.get("updated", ""),
        "feed": old_data.get("feed", []),
        "schedule": old_data.get("schedule", []),
        "results": old_data.get("results", []),
        "rankings": old_data.get("rankings", {}),
    }

    # ---- FEED ----
    try:
        feed_xml = fetch_text(FEED_URL)
        feed_items = parse_feed(feed_xml)
        if not feed_items:
            raise ValueError("parsed 0 feed items")
        for item in feed_items:
            item["img64"] = make_img64(item["img"]) if item["img"] else ""
        new_data["feed"] = feed_items
        print(f"OK   feed: parsed {len(feed_items)} items from {FEED_URL}")
    except Exception as e:
        print(
            f"WARNING: feed scrape/parse failed ({e}); keeping previous feed data",
            file=sys.stderr,
        )

    # ---- SCHEDULE ----
    try:
        sched_html = fetch_text(SCHEDULE_URL)
        schedule_items = parse_schedule(sched_html)
        if len(schedule_items) < 3:
            raise ValueError(f"only parsed {len(schedule_items)} schedule cards")
        new_data["schedule"] = schedule_items
        print(f"OK   schedule: parsed {len(schedule_items)} cards from {SCHEDULE_URL}")
    except Exception as e:
        print(
            f"WARNING: schedule scrape/parse failed ({e}); keeping previous schedule data",
            file=sys.stderr,
        )

    # ---- RESULTS ----
    try:
        results_html = fetch_text(RESULTS_URL)
        results_items = parse_results(results_html)
        if len(results_items) < 5:
            raise ValueError(f"only parsed {len(results_items)} results")
        new_data["results"] = results_items
        print(f"OK   results: parsed {len(results_items)} entries from {RESULTS_URL}")
    except Exception as e:
        print(
            f"WARNING: results scrape/parse failed ({e}); keeping previous results data",
            file=sys.stderr,
        )

    # ---- RANKINGS ----
    try:
        rankings_html = fetch_text(RANKINGS_URL)
        rankings = parse_rankings(rankings_html)
        print(
            f"OK   rankings: parsed {len(rankings['divisions'])} divisions, "
            f"{len(rankings['p4p'])} P4P entries from {RANKINGS_URL}"
        )
        new_data["rankings"] = rankings
    except Exception as e:
        print(
            f"WARNING: rankings scrape/parse failed ({e}); keeping previous rankings data",
            file=sys.stderr,
        )

    now = datetime.now(timezone.utc)
    new_data["updated"] = f"{now.strftime('%B')} {now.day}, {now.year}"

    new_html = replace_data_block(html_text, new_data)
    INDEX_PATH.write_text(new_html, encoding="utf-8")
    print(f"index.html updated. updated={new_data['updated']}")


if __name__ == "__main__":
    main()
