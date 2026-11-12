import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import feedparser
import requests

DATA_DIR = Path("site/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

RSS_BILLS = "https://www.althingi.is/rss/frettir.rss?feed=skjol"
RSS_CONSULT = "https://www.althingi.is/rss/frettir.rss?feed=umsagnarbeidnir"
MEETINGS_XML = "https://www.althingi.is/altext/xml/nefndarfundir/?lthing={lthing}"
CURRENT_THING_XML = "https://www.althingi.is/altext/xml/loggjafarthing/yfirstandandi/"

def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def parse_date_rss(raw: str) -> str:
    """Parse RSS date like 'Mon, 16 Feb 2026 17:43:00 GMT' → '2026-02-16'."""
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:
        return ""

def parse_rss(url: str, kind: str):
    feed = feedparser.parse(url)
    items = []
    for e in feed.entries:
        title = clean(getattr(e, "title", ""))
        link = clean(getattr(e, "link", ""))
        summary = clean(getattr(e, "summary", ""))
        raw_date = clean(getattr(e, "published", "") or getattr(e, "updated", ""))

        items.append({
            "kind": kind,
            "title": title,
            "link": link,
            "published": parse_date_rss(raw_date),
            "summary": summary[:500],
        })
    return items

def current_lthing(*item_lists):
    """Finnur núverandi löggjafarþing: hæsta ltg-gildi í RSS-hlekkjum og
    XML-vefþjónustu Alþingis (yfirstandandi þing); tekur hærra gildið."""
    nums = [int(m) for items in item_lists for x in items
            for m in re.findall(r"[?&]ltg=(\d+)", x.get("link", ""))]
    try:
        r = requests.get(CURRENT_THING_XML, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for el in root.iter():
            num = el.get("númer") or el.get("numer")
            if num and num.isdigit() and 100 < int(num) < 300:
                nums.append(int(num))
                break
    except Exception:
        pass
    return max(nums) if nums else None

def fetch_meetings_xml(lthing):
    """Sækir nefndarfundi úr Althingi XML API."""
    if not lthing:
        return []
    try:
        r = requests.get(MEETINGS_XML.format(lthing=lthing), timeout=30)
        r.raise_for_status()
    except Exception:
        return []

    root = ET.fromstring(r.content)
    meetings = []

    for fund in root.findall("nefndarfundur"):
        nefnd = fund.findtext("nefnd", "").strip()
        dagur = ""
        timi = ""
        hefst = fund.find("hefst")
        if hefst is not None:
            dagur = (hefst.findtext("dagur") or "").strip()
            timi = (hefst.findtext("timi") or hefst.findtext("tími") or "").strip()

        stadur = clean(fund.findtext("staður", ""))

        # HTML hlekkur á dagskrá
        link = ""
        nanar = fund.find("nánar")
        if nanar is not None:
            dagskra = nanar.find("dagskrá")
            if dagskra is not None:
                link = clean(dagskra.findtext("html", ""))

        title = f"{nefnd} — {dagur} kl. {timi}" if timi else f"{nefnd} — {dagur}"
        if stadur:
            title += f" ({stadur})"

        meetings.append({
            "kind": "nefndarfundur",
            "title": title,
            "link": link,
            "published": dagur,
            "nefnd": nefnd,
        })

    return meetings

def write_json(path: Path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def main():
    bills = parse_rss(RSS_BILLS, "skjol")
    consultations = parse_rss(RSS_CONSULT, "umsagnarbeidnir")
    lthing = current_lthing(bills, consultations)
    print(f"Löggjafarþing: {lthing}")
    meetings = fetch_meetings_xml(lthing)

    out = {"generated_at": now_iso(), "lthing": lthing}

    write_json(DATA_DIR / "bills.json", {**out, "items": bills})
    write_json(DATA_DIR / "consultations.json", {**out, "items": consultations})
    write_json(DATA_DIR / "meetings.json", {**out, "items": meetings})

if __name__ == "__main__":
    main()
