#!/usr/bin/env python3
"""Generate the Danish ICPC-2 supplement from KiAP's Access database.

Python standard library plus the mdbtools commands mdb-tables/mdb-export.
Source downloads are refreshed on every run; the pinned international reference
is cached. No Access queries or macros are executed.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

KIAP_PAGE = "https://www.kiap.dk/praksis/icpc-2"
SYSTEM = "http://hl7.org/fhir/sid/icpc-2"
CANONICAL = "http://hl7.dk/fhir/core/CodeSystem/icpc2E-DK"
REFERENCE_URL = (
    "https://www.helsedirektoratet.no/digitalisering-og-e-helse/"
    "helsefaglige-kodeverk/icpc/icpc-2e--english-version/_/attachment/inline/"
    "7c5c8e7f-8c5a-4a0d-97a7-49bb144a162c%3A"
    "22fb4d59b1033d44af1da42cb84897cb363f7136/ICPC-2e-v7.0.zip"
)
USER_AGENT = "dk-sks-terminology-automation/1.0 (+https://github.com/hl7dk/dk-sks-terminology-automation)"
RELEASE_RE = re.compile(r"ICPC[_-]v(\d+(?:[_.]\d+)*)[_-](\d{8})\.accdb", re.I)
CODE_RE = re.compile(r"[ABDFHKLNPRSTUWXYZ](?:0[1-9]|[12]\d|[789]\d)")
# Reviewed against KiAP 4.4 and international ICPC-2e v7.0. Only these exact
# conflicting sets are accepted. A new label, even on these codes, needs review.
RESOLUTIONS = {
    "P70": (frozenset({"Demens", "Neurologisk sygdom IKA"}), "Demens"),
    "R83": (frozenset({"Infektion i luftveje IKA", "Lungebetændelse"}), "Infektion i luftveje IKA"),
    "S12": (frozenset({"Insektstik", "Myggestik"}), "Insektstik"),
}
COPYRIGHT = (
    "ICPC-2 is copyright WONCA (World Organization of Family Doctors). "
    "DSAM (Danish College of General Practitioners) holds and administers the "
    "Danish rights to ICPC-2-DK, which is maintained by KiAP. Use and "
    "redistribution of this content are subject to the applicable WONCA and "
    "DSAM licensing terms; this supplement grants no additional rights. See "
    "[DSAM rights and stewardship information](https://www.dsam.dk/forskning-og-kvalitet/icpc) "
    "and [KiAP ICPC-2-DK information](https://www.kiap.dk/praksis/icpc-2). "
    "The containing package's CC0 license does not override these source rights or licensing terms."
)


@dataclass(frozen=True)
class Release:
    url: str
    version: str
    date: str


def parse_release(url: str) -> Release:
    if any(character.isspace() for character in url):
        raise ValueError("Source URL cannot contain whitespace")
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Source URL must identify the database directly, without credentials, query or fragment")
    if parts.scheme != "https" or parts.hostname not in {"kiap.dk", "www.kiap.dk", "web.kiap.dk"}:
        raise ValueError("Source URL must be an HTTPS KiAP URL")
    match = RELEASE_RE.fullmatch(Path(urllib.parse.unquote(parts.path)).name)
    if not match:
        raise ValueError(f"Cannot identify KiAP version/date from filename: {url}")
    date = dt.datetime.strptime(match[2], "%Y%m%d").date().isoformat()
    return Release(url, match[1].replace("_", "."), date)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.hrefs.extend(value for key, value in attrs if key == "href" and value)


def discover_release(page: str, page_url: str = KIAP_PAGE) -> Release:
    links = Links()
    links.feed(page)
    releases = {}
    for href in links.hrefs:
        url = urllib.parse.urljoin(page_url, href)
        filename = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
        if RELEASE_RE.fullmatch(filename):
            release = parse_release(url)
            releases[url] = release
    if not releases:
        raise ValueError("No dated ICPC .accdb release found on KiAP page; review upstream links")
    def key(release):
        return tuple(int(v) for v in release.version.split(".")), release.date
    latest_key = max(map(key, releases.values()))
    latest = [r for r in releases.values() if key(r) == latest_key]
    if len(latest) != 1:
        raise ValueError("Multiple URLs identify the latest KiAP release; use --source-url after review")
    return latest[0]


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def mdb(command: list[str]) -> str:
    try:
        return subprocess.run(command, check=True, capture_output=True,
                              text=True, encoding="utf-8", timeout=90).stdout
    except FileNotFoundError as exc:
        raise ValueError("Install mdbtools (apt-get install mdbtools / brew install mdbtools)") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"{command[0]} failed: {exc.stderr.strip()}") from exc


def read_rows(database: Path, table: str | None = None) -> tuple[str, list[dict]]:
    tables = mdb(["mdb-tables", "-1", str(database)]).splitlines()
    if table is None:
        matches = [name for name in tables if re.fullmatch(r"ICPCKONV?2[-_].+", name, re.I)]
        if len(matches) != 1:
            raise ValueError(f"Expected one ICPCKON2 mapping table, found {matches}; specify --table")
        table = matches[0]
    if table not in tables:
        raise ValueError(f"Table not found: {table}")
    reader = csv.DictReader(io.StringIO(mdb(["mdb-export", str(database), table])))
    required = {"ICPC-kode", "ICPC-diagnose"}
    if not required <= set(reader.fieldnames or []):
        raise ValueError(f"{table} lacks required columns {sorted(required)}")
    return table, list(reader)


def reference_codes(archive: Path) -> set[str]:
    with zipfile.ZipFile(archive) as source:
        xml_files = [name for name in source.namelist() if name.lower().endswith(".xml")]
        if len(xml_files) != 1:
            raise ValueError("Expected one ClaML document in the international reference archive")
        root = ET.fromstring(source.read(xml_files[0]).decode("utf-8-sig").lstrip())
    codes = {c.attrib["code"] for c in root.findall("Class") if CODE_RE.fullmatch(c.attrib.get("code", ""))}
    if not codes:
        raise ValueError("No ICPC symptom/diagnosis codes in international reference")
    return codes


def select_labels(rows: list[dict], base_codes: set[str]) -> tuple[dict[str, str], dict]:
    labels = defaultdict(set)
    excluded = set()
    for row in rows:
        code = (row.get("ICPC-kode") or "").strip()
        value = " ".join((row.get("ICPC-diagnose") or "").split())
        if not code and not value:
            continue
        if not code or not value:
            raise ValueError(f"Incomplete ICPC code/label row: {code!r}, {value!r}")
        # KiAP chapter headings and the Danish 'unknown' V00 are not base codes.
        if re.fullmatch(r"[ABDFHKLNPRSTUVWXYZ]00", code):
            excluded.add(code)
            continue
        # Process codes, if published later, are outside this supplement's scope.
        if re.fullmatch(r"(?:[ABDFHKLNPRSTUWXYZ]|-)[3-6]\d", code):
            excluded.add(code)
            continue
        if code not in base_codes:
            raise ValueError(f"KiAP code {code!r} is absent from the international reference; review before publishing")
        labels[code].add(value)
    missing = base_codes - labels.keys()
    if missing:
        raise ValueError(f"KiAP is missing {len(missing)} international symptom/diagnosis codes: {', '.join(sorted(missing))}")
    selected, applied = {}, {}
    for code, values in sorted(labels.items()):
        if len(values) == 1:
            selected[code] = next(iter(values))
        else:
            resolution = RESOLUTIONS.get(code)
            if not resolution or values != resolution[0]:
                raise ValueError(f"Unreviewed conflicting labels for {code}: {sorted(values)}")
            selected[code] = resolution[1]
            applied[code] = resolution[1]
    return selected, {"excluded_codes": sorted(excluded), "resolved_conflicts": applied}


def build_supplement(labels: dict[str, str], release: Release, table: str, resolved: dict) -> dict:
    conflict_note = ""
    if resolved:
        conflict_note = " Conflicting source labels are resolved as follows: " + "; ".join(
            f"{code}: {value}" for code, value in sorted(resolved.items())) + "."
    return {
        "resourceType": "CodeSystem", "id": "icpc2E-DK", "url": CANONICAL,
        "version": release.date, "name": "Icpc2DanishTranslations",
        "title": "ICPC-2 Danish translations (from KiAP)", "status": "active",
        "experimental": False, "date": release.date, "publisher": "HL7 Denmark",
        "contact": [{"name": "HL7 Denmark", "telecom": [
            {"system": "url", "value": "http://www.hl7.dk"},
            {"system": "email", "value": "dk-affiliate@hl7.dk"}]}],
        "description": (
            f"Danish-language designation supplement for ICPC-2, containing {len(labels)} symptom and diagnosis codes. "
            "Process codes and Danish-only grouping codes are excluded. "
            f"The authoritative source is KiAP ICPC-2-DK {release.version}, "
            f"[{Path(urllib.parse.urlsplit(release.url).path).name}]({release.url}), "
            f"table {table}, fields ICPC-kode and ICPC-diagnose. Whitespace is normalized."
            + conflict_note + " Codes are checked against international ICPC-2e v7.0. "
            "Generated by scripts/icpc2_da_supplement.py. The version and date identify "
            "the KiAP source file's release date. [KiAP documentation and releases](" + KIAP_PAGE + ")."
        ),
        "copyright": COPYRIGHT,
        "jurisdiction": [{"coding": [{"system": "urn:iso:std:iso:3166", "code": "DK", "display": "Denmark"}]}],
        "content": "supplement", "supplements": SYSTEM, "count": len(labels),
        "concept": [{"code": code, "designation": [{"language": "da", "value": value}]}
                    for code, value in sorted(labels.items())],
    }


def semantic_content(resource: dict) -> dict:
    # A source re-export alone does not justify a PR. Keep the last published
    # provenance until the labels or other substantive resource metadata change.
    result = {key: value for key, value in resource.items() if key not in {"version", "date", "meta", "description"}}
    result["concept"] = sorted(result.get("concept", []), key=lambda c: c["code"])
    return result


def write_supplement(resource: dict, output: Path, only_if_changed: bool) -> bool:
    if only_if_changed and output.exists():
        previous = json.loads(output.read_text(encoding="utf-8"))
        if semantic_content(previous) == semantic_content(resource):
            return False
    atomic_write(output, (json.dumps(resource, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", help="Specific dated KiAP .accdb URL; otherwise discover latest")
    parser.add_argument("--source-file", type=Path, help="Local .accdb, requires --source-url for provenance")
    parser.add_argument("--table", help="Override table selection if KiAP changes its schema")
    parser.add_argument("--reference-file", type=Path, help="Local international ICPC-2e v7.0 ZIP (offline)")
    parser.add_argument("--cache-dir", type=Path, default=Path(".icpc-kiap-cache"))
    parser.add_argument("--out", type=Path, default=Path("fhir/CodeSystem-icpc2E-DK.json"))
    parser.add_argument("--only-if-changed", action="store_true", help="Preserve existing output when only source stamps/provenance changed")
    parser.add_argument("--report", type=Path, help="Write source and validation details as JSON")
    args = parser.parse_args(argv)
    if args.source_file and not args.source_url:
        parser.error("--source-file requires --source-url")
    try:
        release = parse_release(args.source_url) if args.source_url else discover_release(fetch(KIAP_PAGE).decode("utf-8"))
        if args.source_file:
            database = args.source_file
        else:
            database = args.cache_dir / Path(urllib.parse.urlsplit(release.url).path).name
            atomic_write(database, fetch(release.url))
        reference = args.reference_file or args.cache_dir / "ICPC-2e-v7.0.zip"
        if not reference.exists() and not args.reference_file:
            atomic_write(reference, fetch(REFERENCE_URL))
        table, rows = read_rows(database, args.table)
        labels, details = select_labels(rows, reference_codes(reference))
        resource = build_supplement(labels, release, table, details["resolved_conflicts"])
        changed = write_supplement(resource, args.out, args.only_if_changed)
        report = {"source_url": release.url, "source_version": release.version, "source_date": release.date,
                  "table": table, "concepts": len(labels), "changed": changed, **details}
        if args.report:
            atomic_write(args.report, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
                for key in ("source_url", "source_version", "source_date", "concepts"):
                    handle.write(f"{key}={report[key]}\n")
                handle.write(f"changed={str(changed).lower()}\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, ET.ParseError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        print(f"ICPC generation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
