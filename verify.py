#!/usr/bin/env python3
"""Plot Finder checking protocol — 4 layers:

1. DEPLOY SMOKE TEST — checks the LIVE site: page loads, data parses,
   every PDF URL returns a real PDF.
2. GOLDEN SET — known-answer facts that must always hold.
3. DATA-QUALITY GATES — build refuses to ship bad data.
4. EXTERNAL-DEPENDENCY HEALTH — upstream services alive.

Usage:
  python3 verify.py --deployed   # smoke test against live site (post-deploy)
  python3 verify.py --local      # data gates + golden set on local files
  python3 verify.py --health     # upstream dependency health
  python3 verify.py --all        # everything (daily cron)
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "out"
LIVE_BASE = "https://sincelabs.github.io/plotfinder/"
TIMEOUT = 25

FAILURES = []
WARNINGS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append((name, detail))
    return ok


def fetch(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "PlotFinder-Verify/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


# ---------------------------------------------------------------- layer 3: data gates
def load_local_data():
    """Parse ui/data.js (const DATA = [...]) into Python."""
    text = (ROOT / "ui" / "data.js").read_text()
    m = re.search(r"const DATA\s*=\s*(\[.*\]);?\s*$", text, re.S)
    if not m:
        m = re.search(r"const DATA\s*=\s*(\[.*\]);", text, re.S)
    return json.loads(m.group(1))


def data_gates():
    print("\n=== Layer 3: DATA-QUALITY GATES (local build output) ===")
    entries = load_local_data()

    check("gate: entries exist", len(entries) >= 20, f"{len(entries)} entries")

    # Swedish-marker purity scan
    sv_markers = re.compile(
        r"\b(får|byggas|kvarter|tomt|bostadsplats|v-m2|m2vy|bp\.|bp/|för|och)\b", re.I
    )
    bad = []
    for e in entries:
        q = e.get("quote", "")
        if e.get("lang") == "sv" or (sv_markers.search(q) and "bp" in q.lower()):
            bad.append(e.get("kaava") or e.get("plan") or "?")
    check("gate: quote language purity (no Swedish twins)", not bad, str(bad[:5]))

    # Cut-word detection: quote must not end mid-word without punctuation
    cut = [e.get("kaava", "?") for e in entries
           if e.get("quote") and not re.search(r"[.)\]\s]$", e["quote"].strip())]
    check("gate: no mid-word cut quotes", not cut, str(cut[:5]))

    # Every candidate must have a PDF URL
    cands = [e for e in entries if e.get("class") == "candidate"]
    nopdf = [e.get("kaava", "?") for e in cands if not e.get("pdf_url")]
    check("gate: all candidates have pdf_url", not nopdf, str(nopdf[:5]))

    # No-document entries must carry a note
    nodoc = [e for e in entries if e.get("class") in ("no_document", "missing_doc")]
    check("gate: no-document entries have notes",
          all(e.get("note") for e in nodoc), f"{len(nodoc)} entries")

    return entries, cands


# ---------------------------------------------------------------- layer 2: golden set
def golden_set(entries, cands):
    print("\n=== Layer 2: GOLDEN SET (known-answer regression) ===")
    by_kaava = {}
    for e in entries:
        k = str(e.get("kaava") or e.get("plan") or "")
        if k:
            by_kaava.setdefault(k, e)

    # G1: 3375 Masalanportti is a candidate with named plots
    # (3375 legitimately has 2 entries: the permission clause [candidate]
    #  and the parking exemption [excluded] — check the candidate one)
    g = next((e for e in entries if str(e.get("kaava")) == "3375"
              and e.get("class") == "candidate"), None)
    check("golden: 3375 has candidate entry", g is not None,
          "no candidate entry for 3375")
    plots = json.dumps((g or {}).get("key_info", {})) + json.dumps((g or {}).get("plots", [])) + (g or {}).get("quote", "")
    check("golden: 3375 names kortteli 2034/2064",
          "2034" in plots and "2064" in plots)

    # G2: 3336 Vesitorninmäki is a candidate
    check("golden: 3336 is candidate",
          by_kaava.get("3336", {}).get("class") == "candidate",
          f"class={by_kaava.get('3336', {}).get('class')}")

    # G3: 2882 + 35600 are no_document
    check("golden: 2882 flagged missing_doc",
          by_kaava.get("2882", {}).get("class") in ("no_document", "missing_doc"),
          f"class={by_kaava.get('2882', {}).get('class')}")
    check("golden: 35600 flagged missing_doc",
          by_kaava.get("35600", {}).get("class") in ("no_document", "missing_doc"),
          f"class={by_kaava.get('35600', {}).get('class')}")

    # G4: both cities present
    cities = {e.get("city") for e in entries if e.get("city")}
    check("golden: both cities present", {"kirkkonummi", "espoo"} <= cities, str(cities))

    # G5: candidate floor — matching candidates below this means the matcher broke
    kk_c = [e for e in cands if e.get("city") == "kirkkonummi"]
    es_c = [e for e in cands if e.get("city") == "espoo"]
    check("golden: KK candidates >= 12", len(kk_c) >= 12, f"{len(kk_c)}")
    check("golden: Espoo candidates >= 9", len(es_c) >= 9, f"{len(es_c)}")


# ---------------------------------------------------------------- layer 1: deployed smoke test
def deployed_smoke(entries):
    print("\n=== Layer 1: DEPLOYED SMOKE TEST (live site) ===")
    try:
        st, ct, body = fetch(LIVE_BASE)
        ok = st == 200 and b"Plot Finder" in body
        check("smoke: live page loads", ok, f"{st} {ct}")
    except Exception as ex:
        check("smoke: live page loads", False, str(ex))
        return

    try:
        st, ct, js = fetch(LIVE_BASE + "data.js")
        m = re.search(rb"const DATA\s*=\s*(\[.*\]);", js, re.S)
        live_data = json.loads(m.group(1)) if m else None
        check("smoke: live data.js parses", live_data is not None,
              f"{len(js)} bytes")
    except Exception as ex:
        check("smoke: live data.js parses", False, str(ex))
        return

    # Every live candidate PDF URL must return a real PDF
    cands = [e for e in live_data if e.get("class") == "candidate" and e.get("pdf_url")]
    bad = []
    for e in cands:
        url = e["pdf_url"]
        try:
            st, ct, body = fetch(url)
            if st != 200 or ("pdf" not in ct.lower() and not body[:5] == b"%PDF-"):
                bad.append((e.get("kaava"), st, ct[:40]))
        except Exception as ex:
            bad.append((e.get("kaava"), "ERR", str(ex)[:60]))
    check(f"smoke: all {len(cands)} live candidate PDFs fetch", not bad, str(bad[:3]))

    # Live count parity: live data must match local build
    check("smoke: live/local entry counts match",
          len(live_data) == len(entries),
          f"live={len(live_data)} local={len(entries)}")

    for f in ("plan_polygons.json", "plots.js"):
        try:
            st, ct, _ = fetch(LIVE_BASE + f)
            check(f"smoke: {f} deployed", st == 200, f"{st}")
        except Exception as ex:
            check(f"smoke: {f} deployed", False, str(ex)[:50])


# ---------------------------------------------------------------- layer 4: dependency health
def dependency_health():
    print("\n=== Layer 4: EXTERNAL-DEPENDENCY HEALTH ===")
    deps = [
        ("KK ArcGIS FeatureServer",
         "https://services-eu1.arcgis.com/P1cxApjmyq5VQU8z/arcgis/rest/services/"
         "Asemakaava_Kirkkonummi/FeatureServer/0/query?where=1%3D1&returnCountOnly=true&f=json"),
        ("Espoo Tekla WFS",
         "https://kartat.espoo.fi/teklaogcweb/wfs.ashx?service=WFS&version=1.1.0&request=GetCapabilities"),
        ("KK portal", "https://web.dmcity.fi/kirkkonummi/public/"),
    ]
    for name, url in deps:
        try:
            st, ct, body = fetch(url)
            ok = st == 200
            extra = ""
            if ok and b"error" in body[:400].lower():
                ok, extra = False, "error in body"
            check(f"dep: {name}", ok, f"{st} {extra}")
        except Exception as ex:
            check(f"dep: {name}", False, str(ex)[:60])


# ---------------------------------------------------------------- 5. before-demo checklist
def demo_checklist():
    print("\n=== Layer 5: BEFORE-DEMO HUMAN CHECKLIST (manual, 5 min) ===")
    print("""Run this in a real browser before showing the tool:
  [ ] Open https://sincelabs.github.io/plotfinder/ (hard refresh)
  [ ] List shows candidates for Kirkkonummi, Espoo, and All
  [ ] Click 3 PDF links across both cities — each opens a real PDF
  [ ] Switch to Kartta tab — polygons render, click one popup
  [ ] Search 'Masala' filters list AND map
  [ ] Yellow 'no document' polygons visible on map
If any box fails, run: python3 src/verify.py --all""")


# ---------------------------------------------------------------- main
if __name__ == "__main__":
    args = sys.argv[1:]
    mode = args[0] if args else "--all"

    if mode in ("--local", "--all"):
        entries, cands = data_gates()
        golden_set(entries, cands)
    if mode in ("--deployed", "--all"):
        entries = load_local_data()
        deployed_smoke(entries)
    if mode in ("--health", "--all"):
        dependency_health()

    print(f"\n{'='*50}")
    if FAILURES:
        print(f"RESULT: FAIL — {len(FAILURES)} failure(s)")
        for n, d in FAILURES:
            print(f"  ✗ {n}: {d[:80]}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
