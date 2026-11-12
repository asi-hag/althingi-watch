"""Vöktun umsagnarbeiðna Alþingis: hverjir fá beiðni og hverjir skila umsögn.

Safnar umsagnarbeiðnum úr RSS-straumi Alþingis (og geymir alla sögu), sækir
fyrir hvert mál lista yfir viðtakendur umsagnarbeiðna og innsend erindi, og
merkir hvort ASÍ og samtök sem skilgreind eru í umsagnir_config.json fengu
beiðni / skiluðu umsögn.

Úttak: site/data/umsagnir.json  (lesið af site/umsagnir.html)
Hrágögn fyrstu mála eru vistuð í debug/ til að hægt sé að sannreyna þáttun.
"""
import argparse
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

import feedparser
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "site" / "data"
OUT = DATA / "umsagnir.json"
SEED = DATA / "consultations.json"
CONFIG = Path(__file__).resolve().parent / "umsagnir_config.json"
DEBUG = ROOT / "debug"

RSS = "https://www.althingi.is/rss/frettir.rss?feed=umsagnarbeidnir"
THINGMAL_XML = "https://www.althingi.is/altext/xml/thingmalalisti/thingmal/?lthing={ltg}&malnr={mnr}"
FERILL_HTML = "https://www.althingi.is/thingstorf/thingmalalistar-eftir-thingum/ferill/?ltg={ltg}&mnr={mnr}"

UA = {"User-Agent": "ASI-althingi-watch/1.0 (+https://github.com/asi-hag/althingi-watch)"}
SLEEP = 0.7          # sek. milli fyrirspurna
MAX_FETCH = 150      # hámark mála sem eru sótt í hverri keyrslu
REFRESH_DAYS = 30    # halda áfram að uppfæra mál þetta lengi eftir frest
DEBUG_N = 3          # hve mörg mál fá hrágögn vistuð

MONTHS = {
    "janúar": 1, "febrúar": 2, "mars": 3, "apríl": 4, "maí": 5, "júní": 6,
    "júlí": 7, "ágúst": 8, "september": 9, "október": 10, "nóvember": 11, "desember": 12,
}
TITLE_RE = re.compile(r"^(?P<nefnd>.+?) hefur óskað eftir umsögnum í máli (?P<mnr>\d+),\s*(?P<heiti>.+)$")
DEADLINE_RE = re.compile(r"Umsagnarfrest\w* er til (\d{1,2})\.\s*([a-záéíóúýþæö]+)\s+(\d{4})", re.I)


# ---------------------------------------------------------------- hjálparföll
def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def norm(s):
    return clean(s).lower()


def tag(el):
    """Nafn XML-merkis án namespace, lágstafir."""
    return el.tag.split("}")[-1].lower() if isinstance(el.tag, str) else ""


def parse_deadline(text):
    m = DEADLINE_RE.search(text or "")
    if not m:
        return ""
    d, mon, y = m.groups()
    mon = MONTHS.get(mon.lower())
    if not mon:
        return ""
    try:
        return date(int(y), mon, int(d)).isoformat()
    except ValueError:
        return ""


def parse_rss_date(raw):
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except Exception:
        return raw[:10] if re.match(r"\d{4}-\d{2}-\d{2}", raw or "") else ""


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def get(url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    time.sleep(SLEEP)
    return r


# ---------------------------------------------------------------- samtök
class Matcher:
    def __init__(self, orgs):
        self.orgs = []
        for o in orgs:
            pats = [re.compile(r"(?<!\w)" + re.escape(p.lower()) + r"(?!\w)") for p in o["patterns"]]
            self.orgs.append((o["key"], pats))

    def match(self, name):
        n = norm(name)
        return [k for k, pats in self.orgs if any(p.search(n) for p in pats)]


# ---------------------------------------------------------------- RSS
def parse_item(title, link, summary, published):
    m = TITLE_RE.match(clean(title))
    if not m:
        return None
    qs = parse_qs(urlparse(link).query)
    ltg = (qs.get("ltg") or [""])[0]
    mnr = (qs.get("mnr") or [m["mnr"]])[0]
    if not ltg:
        return None
    nefnd = clean(m["nefnd"])
    return {
        "id": f"{ltg}-{mnr}-{norm(nefnd)}-{published}",
        "mal": f"{ltg}-{mnr}",
        "ltg": int(ltg),
        "mnr": int(mnr),
        "nefnd": nefnd,
        "heiti": clean(m["heiti"]),
        "beidni_dags": published,
        "frestur": parse_deadline(summary),
        "link": FERILL_HTML.format(ltg=ltg, mnr=mnr),
    }


def fetch_rss():
    feed = feedparser.parse(RSS, request_headers=UA)
    out = []
    for e in feed.entries:
        it = parse_item(getattr(e, "title", ""), getattr(e, "link", ""), getattr(e, "summary", ""),
                        parse_rss_date(getattr(e, "published", "") or getattr(e, "updated", "")))
        if it:
            out.append(it)
    return out


def seed_from_consultations():
    """Fyrsta keyrsla: nýta umsagnarbeiðnir sem vaktin hefur þegar sótt."""
    d = load_json(SEED, {})
    out = []
    for x in d.get("items", []):
        it = parse_item(x.get("title", ""), x.get("link", ""), x.get("summary", ""), x.get("published", ""))
        if it:
            out.append(it)
    return out


# ---------------------------------------------------------------- mál: viðtakendur og erindi
NAME_TAGS = ("nafn", "viðtakandi", "vidtakandi", "aðili", "adili", "umsagnaraðili", "sendandi", "heiti")


def leaf_names(el):
    """Nöfn innan staks: fyrst þekkt nafnamerki, annars allur laufatexti sem líkist nafni."""
    named = [clean(x.text) for x in el.iter() if tag(x) in NAME_TAGS and clean(x.text)]
    if named:
        return named
    names = []
    for x in el.iter():
        if len(x) == 0:
            t = clean(x.text)
            if len(t) > 2 and not re.match(r"^[\d\s.:/-]+$", t) and not t.startswith("http"):
                names.append(t)
    return names


def recipients_from_xml(root):
    """Leitar að stökum sem heita umsagnab*/umsagnarb* og tínir nöfn viðtakenda úr þeim."""
    blocks = [el for el in root.iter() if re.match(r"umsagnar?b", tag(el))]
    if not blocks:
        return None
    names = []
    for b in blocks:
        for child in list(b) or [b]:
            names.extend(leaf_names(child))
    return list(dict.fromkeys(names))  # fjarlægja tvítekningar, halda röð


def recipients_from_html(html):
    """Varaleið: finnur fyrirsögn/hluta um umsagnabeiðnir á ferilsíðu og tínir nöfn úr lista/töflu."""
    soup = BeautifulSoup(html, "html.parser")
    for h in soup.find_all(string=re.compile(r"umsagnar?beiðni", re.I)):
        node = h.parent
        for _ in range(4):
            if node is None:
                break
            sib = node.find_next(["ul", "ol", "table"])
            if sib is not None:
                cells = sib.find_all(["li", "td"])
                names = [clean(c.get_text(" ")) for c in cells]
                names = [n for n in names if len(n) > 2 and not re.match(r"^[\d\s.:/-]+$", n)]
                if names:
                    return list(dict.fromkeys(names))
            node = node.parent
    return None


def erindi_from_xml(root):
    out = []
    for el in root.iter():
        if tag(el) != "erindi":
            continue
        sendandi = clean(el.findtext("sendandi") or "")
        if not sendandi:
            continue
        teg_el = next((x for x in el.iter() if tag(x).startswith("tegund")), None)
        tegund = clean((teg_el.text if teg_el is not None else "") or "")
        dags = ""
        for x in el.iter():
            if tag(x) in ("komudagur", "sendingadagur", "dagsetning", "dags"):
                dags = clean(x.text)[:10]
                break
        link = ""
        for x in el.iter():
            if tag(x) in ("pdf", "html", "slóð", "slod") and clean(x.text).startswith("http"):
                link = clean(x.text)
                break
        out.append({"sendandi": sendandi, "tegund": tegund, "dags": dags, "link": link})
    return out


def fetch_mal(ltg, mnr, debug_slot):
    info = {"fetched_at": now_iso(), "vidtakendur": None, "erindi": [], "heimild": "", "villa": ""}
    try:
        r = get(THINGMAL_XML.format(ltg=ltg, mnr=mnr))
        if debug_slot:
            DEBUG.mkdir(exist_ok=True)
            (DEBUG / f"thingmal_{ltg}_{mnr}.xml").write_bytes(r.content)
        root = ET.fromstring(r.content)
        info["vidtakendur"] = recipients_from_xml(root)
        info["erindi"] = erindi_from_xml(root)
        if info["vidtakendur"] is not None:
            info["heimild"] = "xml"
    except Exception as ex:
        info["villa"] = f"xml: {ex}"[:200]

    if info["vidtakendur"] is None:
        try:
            r = get(FERILL_HTML.format(ltg=ltg, mnr=mnr))
            if debug_slot:
                DEBUG.mkdir(exist_ok=True)
                (DEBUG / f"ferill_{ltg}_{mnr}.html").write_bytes(r.content)
            info["vidtakendur"] = recipients_from_html(r.text)
            if info["vidtakendur"] is not None:
                info["heimild"] = "html"
        except Exception as ex:
            info["villa"] = (info["villa"] + f" | html: {ex}")[:300]
    return info


def classify(info, matcher):
    beidni, skiladi = set(), set()
    for n in info.get("vidtakendur") or []:
        beidni.update(matcher.match(n))
    for e in info.get("erindi") or []:
        skiladi.update(matcher.match(e["sendandi"]))
    info["beidni_til"] = sorted(beidni)
    info["skiladi"] = sorted(skiladi)
    return info


# ---------------------------------------------------------------- aðalfall
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="Sækja ekkert, bara endurþátta/flokka")
    args = ap.parse_args()

    cfg = load_json(CONFIG, {"orgs": [], "groups": {}})
    matcher = Matcher(cfg["orgs"])
    state = load_json(OUT, {"requests": [], "mal": {}})

    reqs = {r["id"]: r for r in state.get("requests", [])}
    incoming = seed_from_consultations() if not reqs else []
    if not args.offline:
        try:
            incoming += fetch_rss()
        except Exception as ex:
            print("RSS villa:", ex)
    new = 0
    for it in incoming:
        if it["id"] not in reqs:
            new += 1
        reqs[it["id"]] = {**reqs.get(it["id"], {}), **it}

    mal = state.get("mal", {})
    today = date.today()
    # mál sem þarf að sækja: ný, eða frestur ekki of langt að baki, og ekki sótt í dag
    due = {}
    for r in reqs.values():
        key = r["mal"]
        info = mal.get(key)
        fr = r.get("frestur") or r.get("beidni_dags") or ""
        recent = not fr or date.fromisoformat(fr) >= today - timedelta(days=REFRESH_DAYS)
        fetched_today = info and info.get("fetched_at", "")[:10] == now_iso()[:10]
        if (info is None or recent) and not fetched_today:
            due[key] = (r["ltg"], r["mnr"])

    fetched = 0
    if not args.offline:
        for i, (key, (ltg, mnr)) in enumerate(sorted(due.items(), key=lambda kv: kv[0], reverse=True)[:MAX_FETCH]):
            info = fetch_mal(ltg, mnr, debug_slot=i < DEBUG_N)
            old = mal.get(key, {})
            # halda eldri viðtakendalista ef sókn mistókst
            if info["vidtakendur"] is None and old.get("vidtakendur") is not None:
                info["vidtakendur"] = old["vidtakendur"]
                info["heimild"] = old.get("heimild", "")
            mal[key] = info
            fetched += 1
            nv = len(info["vidtakendur"]) if info["vidtakendur"] is not None else "?"
            print(f"  {key}: {nv} viðtakendur, {len(info['erindi'])} erindi {info['villa']}")

    for key in mal:
        classify(mal[key], matcher)

    payload = {
        "generated_at": now_iso(),
        "orgs": cfg["orgs"],
        "groups": cfg.get("groups", {}),
        "requests": sorted(reqs.values(), key=lambda r: (r.get("frestur", ""), r["id"]), reverse=True),
        "mal": mal,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    known = sum(1 for m in mal.values() if m.get("vidtakendur") is not None)
    print(f"Umsagnarbeiðnir: {len(reqs)} ({new} nýjar). Mál sótt: {fetched}. "
          f"Mál með þekkta viðtakendur: {known}/{len(mal)}.")


if __name__ == "__main__":
    main()
