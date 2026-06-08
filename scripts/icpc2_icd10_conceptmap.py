#!/usr/bin/env python3
"""
icpc2_icd10_conceptmap.py
=========================

Build a FHIR R4 ConceptMap mapping Danish ICPC-2 codes to ICD-10.

The tool exposes a small JSON/HTML backend, ``/icpc/icpcserver.php``:
  - ``action=lookupICPC&query=<text>&maxResults=N`` -> an HTML ``<li>`` list of
    matching ICPC codes; querying a bare chapter letter (A, B, ... Z) returns
    every code in that chapter together with its Danish title.
  - ``action=convert&icpc=<code>``                 -> an HTML list of the SKS
    diagnosis codes (``D`` + ICD-10 form, e.g. ``di109`` == ICD-10 ``I10.9``)
    that the ICPC rubric maps onto.

We enumerate the 17 ICPC-2 chapters, convert every code, turn each SKS ``D``
code back into its ICD-10 form (validated/displayed against a cached ICD-10
CodeSystem if available), and emit one ConceptMap.

  source = http://hl7.org/fhir/sid/icpc-2
  target = http://hl7.org/fhir/sid/icd-10

Equivalence: a rubric mapping to exactly one ICD-10 code is emitted as
``equivalent``; one mapping to several is emitted as ``narrower`` (the ICPC
rubric is broader than each individual ICD-10 code).

LICENSING / PROVENANCE
----------------------
ICPC-2 is copyright WONCA; the Danish ICPC-2-DK rights are held by DSAM, and
the mapping data harvested here originates from sundhed.dk. This generated
ConceptMap is therefore **not** an authoritative or openly-licensed artifact —
it is a convenience mapping derived from a public lookup tool. Do not
redistribute it outside the terms under which you hold ICPC-2-DK rights.

Only the Python standard library is used. HTTP responses are cached under
``--cache-dir`` so re-runs are offline and the source is hit only once.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE_URL = "https://dake2.dudal.com/icpc/icpcserver.php"
ICPC2_SYSTEM = "http://hl7.org/fhir/sid/icpc-2"
ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10"
CONCEPTMAP_CANONICAL = "http://hl7.dk/fhir/core/ConceptMap/icpc2-icd10"

# ICPC-2 chapters (process / symptom / diagnosis components share the letter).
CHAPTERS = "ABDFHKLNPRSTUWXYZ"
USER_AGENT = "dk-core-icpc2-icd10/1.0 (+https://github.com/hl7dk; one-time mapping harvest)"

LI_RE = re.compile(r"<li[^>]*icpc='([^']*)'[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
RESULT_RE = re.compile(r"<div class='icpc_result'><b>([^<]+)</b></div>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


def http_get(url: str, params: dict, cache_path: str, sleep: float,
             force: bool) -> str:
    """GET ``url?params`` as Latin-1 text, cached to ``cache_path``."""
    if os.path.exists(cache_path) and not force:
        with open(cache_path, "r", encoding="utf-8") as fh:
            return fh.read()
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("latin-1")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if sleep:
        time.sleep(sleep)   # be a polite one-time visitor
    return text


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub("", s))).strip()


def enumerate_icpc(base_url: str, cache_dir: str, sleep: float,
                   force: bool) -> dict[str, str]:
    """Return {icpc_code: danish_title} across all chapters."""
    codes: dict[str, str] = {}
    for ch in CHAPTERS:
        text = http_get(base_url,
                         {"action": "lookupICPC", "maxResults": 1000,
                          "query": ch},
                         os.path.join(cache_dir, f"chapter-{ch}.html"),
                         sleep, force)
        for code, inner in LI_RE.findall(text):
            code = code.strip()
            if not code:
                continue
            title = strip_tags(inner)
            # the inner text starts with the code itself; drop it for display
            if title.upper().startswith(code.upper()):
                title = title[len(code):].strip()
            codes[code] = title
        print(f"  chapter {ch}: {sum(1 for c in codes if c[0] == ch):>4} codes")
    return codes


def read_codes_file(path: str) -> dict[str, str]:
    """Read a supplemental ``CODE<TAB>TITLE`` list (e.g. the official ICPC-2
    code card). Blank lines and ``#`` comments are ignored. Used to add codes
    the chapter search misses (e.g. D86) and the ICPC-1 process codes."""
    out: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t", 1)
            code = parts[0].strip()
            title = parts[1].strip() if len(parts) > 1 else ""
            if code:
                out[code] = title
    return out


def convert_icpc(base_url: str, code: str, cache_dir: str, sleep: float,
                 force: bool) -> list[str]:
    """Return the SKS 'D' diagnosis codes an ICPC code maps to (deduped)."""
    text = http_get(base_url,
                     {"action": "convert", "maxResults": 999, "icpc": code,
                      "icd10": ""},
                     os.path.join(cache_dir, f"convert-{code}.html"),
                     sleep, force)
    seen, out = set(), []
    for d in RESULT_RE.findall(text):
        d = d.strip()
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def dform_to_icd10(dcode: str) -> str:
    """'di109' -> 'I10.9'  (strip leading D, uppercase, dot after 3 chars)."""
    s = dcode.strip().upper()
    if s.startswith("D"):
        s = s[1:]
    return s[:3] + "." + s[3:] if len(s) > 3 else s


class Icd10Display:
    """Optional ICD-10 display lookup from a cached CodeSystem JSON."""

    def __init__(self, path: str | None):
        self.codes: set[str] = set()
        self.display: dict[str, str] = {}
        self.version = ""
        if path and os.path.exists(path):
            cs = json.load(open(path, "r", encoding="utf-8"))
            self.version = cs.get("version", "")
            for c in cs.get("concept", []):
                self.codes.add(c["code"])
                self.display[c["code"]] = c.get("display", "")

    def resolve(self, dotted: str) -> tuple[str, str, bool]:
        """Return (icd10_code, display, in_base). Falls back to the no-dot
        form, then to the dotted form as-is when the base is unavailable."""
        plain = dotted.replace(".", "")
        for cand in (dotted, plain):
            if cand in self.codes:
                return cand, self.display.get(cand, ""), True
        return dotted, "", not self.codes  # unknown when no base loaded


def build_conceptmap(titles: dict[str, str], mappings: dict[str, list[str]],
                     icd: Icd10Display, canonical: str, version: str,
                     date: str) -> dict:
    elements = []
    mapped = unmapped = total_targets = not_in_base = 0
    for code in sorted(titles):
        dcodes = mappings.get(code, [])
        targets = []
        seen = set()
        for d in dcodes:
            dotted = dform_to_icd10(d)
            icd_code, disp, in_base = icd.resolve(dotted)
            if icd_code in seen:
                continue
            seen.add(icd_code)
            if not in_base:
                not_in_base += 1
            t = {"code": icd_code}
            if disp:
                t["display"] = disp
            targets.append(t)
        element = {"code": code}
        if titles[code]:
            element["display"] = titles[code]
        if targets:
            targets.sort(key=lambda t: t["code"])
            equiv = "equivalent" if len(targets) == 1 else "narrower"
            for t in targets:
                t["equivalence"] = equiv
                if equiv == "narrower":
                    t["comment"] = ("ICPC-2 rubric is broader than this "
                                    "ICD-10 code")
            element["target"] = targets
            mapped += 1
            total_targets += len(targets)
        else:
            # no ICD-10 mapping (e.g. process/component codes)
            element["target"] = [{
                "equivalence": "unmatched",
                "comment": "No ICD-10 mapping in the source tool"}]
            unmapped += 1
        elements.append(element)

    icd_ver = f"|{icd.version}" if icd.version else ""
    return {
        "resourceType": "ConceptMap",
        "id": canonical.rstrip("/").rsplit("/", 1)[-1],
        "url": canonical,
        "version": version,
        "name": "Icpc2ToIcd10",
        "title": "ICPC-2 to ICD-10 (Danish)",
        "status": "draft",
        "experimental": True,
        "date": date,
        "publisher": "HL7 Denmark",
        "jurisdiction": [{"coding": [{
            "system": "urn:iso:std:iso:3166", "code": "DK",
            "display": "Denmark"}]}],
        "description": (
            "Mapping of Danish ICPC-2 codes to ICD-10. ICPC-2 rubrics are broader "
            "than ICD-10; a rubric mapping to one ICD-10 code is 'equivalent', "
            "to several 'narrower'. NOTE: ICPC-2 is copyright WONCA and the "
            "Danish ICPC-2-DK rights are held by DSAM; this convenience mapping "
            "is not an authoritative or openly-licensed artifact."),
        "sourceUri": ICPC2_SYSTEM,
        "targetUri": ICD10_SYSTEM,
        "group": [{
            "source": ICPC2_SYSTEM,
            "target": ICD10_SYSTEM + icd_ver,
            "element": elements,
        }],
        # bookkeeping only; main() pops this before writing (not valid FHIR)
        "_stats": {"icpc_codes": len(elements), "mapped": mapped,
                   "unmapped": unmapped, "targets": total_targets,
                   "targets_not_in_base_icd10": not_in_base},
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Build an ICPC-2 -> ICD-10 ConceptMap.")
    p.add_argument("--cache-dir", default=".icpc-cache",
                   help="Where harvested HTTP responses are cached.")
    p.add_argument("--out", default="sks-icd10-out/ConceptMap-icpc2-icd10.json")
    p.add_argument("--icd10-cache", default=".sks-cache/icd10.json",
                   help="Cached ICD-10 CodeSystem JSON for displays "
                        "(optional; produced by sks_icd10_diff.py).")
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--codes-file", default=None,
                   help="Optional CODE<TAB>TITLE list (e.g. the official ICPC-2 "
                        "code card) to supplement the chapter search with codes "
                        "it misses; titles fill in where the tool has none.")
    p.add_argument("--canonical", default=CONCEPTMAP_CANONICAL)
    p.add_argument("--version", default=None,
                   help="ConceptMap version (default: today's date).")
    p.add_argument("--sleep", type=float, default=0.2,
                   help="Seconds to wait between source requests.")
    p.add_argument("--force", action="store_true",
                   help="Re-fetch from the source even if cached.")
    args = p.parse_args(argv)

    os.makedirs(args.cache_dir, exist_ok=True)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    today = dt.date.today().isoformat()
    version = args.version or today

    print("[1/3] Enumerating ICPC-2 codes by chapter ...")
    titles = enumerate_icpc(args.base_url, args.cache_dir, args.sleep,
                            args.force)
    print(f"  chapter search: {len(titles):,} codes")
    if args.codes_file:
        extra = read_codes_file(args.codes_file)
        added = 0
        for code, title in extra.items():
            if code not in titles:
                added += 1
            # supplied title fills in only when the tool gave none
            if title and not titles.get(code):
                titles[code] = title
            else:
                titles.setdefault(code, "")
        print(f"  +{added} from {args.codes_file} "
              f"(now {len(titles):,} codes)")
    print(f"  total ICPC-2 codes: {len(titles):,}")

    print("[2/3] Converting each ICPC-2 code to ICD-10 ...")
    mappings: dict[str, list[str]] = {}
    for i, code in enumerate(sorted(titles), 1):
        mappings[code] = convert_icpc(args.base_url, code, args.cache_dir,
                                      args.sleep, args.force)
        if i % 50 == 0:
            print(f"  {i:>4}/{len(titles)} converted")

    print("[3/3] Building ConceptMap ...")
    icd = Icd10Display(args.icd10_cache)
    if icd.codes:
        print(f"  ICD-10 displays from {args.icd10_cache} "
              f"(version {icd.version}, {len(icd.codes):,} codes)")
    else:
        print("  no ICD-10 cache found; targets emitted without displays")
    cm = build_conceptmap(titles, mappings, icd, args.canonical, version, today)
    s = cm.pop("_stats")   # bookkeeping only; not valid FHIR, keep out of the file
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(cm, fh, ensure_ascii=False, indent=2)

    print(f"  wrote {args.out}")
    print(f"  ICPC-2 codes : {s['icpc_codes']:>6,}")
    print(f"    mapped     : {s['mapped']:>6,}")
    print(f"    unmapped   : {s['unmapped']:>6,}")
    print(f"  ICD-10 targets : {s['targets']:>6,}  "
          f"({s['targets_not_in_base_icd10']:,} not in the loaded ICD-10)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except urllib.error.URLError as e:
        print(f"network error: {e}", file=sys.stderr)
        sys.exit(2)
