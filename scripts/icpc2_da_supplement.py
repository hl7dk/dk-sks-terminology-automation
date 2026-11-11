#!/usr/bin/env python3
"""Generate the Danish ICPC-2 supplement from KiAP's Access database.

Python standard library plus the mdbtools commands mdb-tables/mdb-export.
Source downloads are refreshed on every run, including the base ICPC-2
CodeSystem from tx.fhir.org that the supplement is validated against and pinned
to. No Access queries or macros are executed.
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
from collections import defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from sks_icd10_diff import fetch_icd10_codesystem

KIAP_PAGE = "https://www.kiap.dk/praksis/icpc-2"
SYSTEM = "http://hl7.org/fhir/sid/icpc-2"
CANONICAL = "http://hl7.dk/fhir/core/CodeSystem/icpc2E-DK"
TX_BASE = "https://tx.fhir.org/r4"
# Base concept kinds outside this supplement's symptom/diagnosis scope.
OUT_OF_SCOPE_KINDS = {"chapter", "process"}
USER_AGENT = "dk-sks-terminology-automation/1.0 (+https://github.com/hl7dk/dk-sks-terminology-automation)"
RELEASE_RE = re.compile(r"ICPC[_-]v(\d+(?:[_.]\d+)*)[_-](\d{8})\.accdb", re.I)
CODE_RE = re.compile(r"[ABDFHKLNPRSTUWXYZ](?:0[1-9]|[12]\d|[789]\d)")
OPEN_END = dt.date(2500, 1, 1)
# Reviewed KiAP 4.4 typo in the middle (last-modified) date; endpoints are intact.
KNOWN_HISTORY_TYPO = ("A01", "DR521", "20240701206010125000101")
# Local property URIs identify these Danish narrative fields, not executable rules.
# The base ICPC-2 CodeSystem already defines criteria/inclusion/exclusion/consider/
# note with English text; the -da suffix keeps the Danish values distinguishable.
GUIDANCE_FIELDS = (
    ("Kriterier", "criteria-da", "Criteria for assigning the ICPC concept."),
    ("Inkl", "inclusion-da", "Terms and conditions included under the ICPC concept."),
    ("Ekskl", "exclusion-da", "Conditions to code elsewhere, including source cross-references."),
    ("Overvej", "consider-da", "Alternative ICPC concepts to consider."),
    ("Bemærk", "note-da", "Supplementary coding notes for the ICPC concept."),
)
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

    @property
    def fhir_version(self) -> str:
        """KiAP business version plus the filename's release date, e.g. 4.4.20260629."""
        return f"{self.version}.{self.date.replace('-', '')}"


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


def concepts(resource: dict):
    for concept in resource.get("concept", []):
        yield concept
        yield from concepts(concept)


def reference_codes(base: dict) -> tuple[set[str], str]:
    """Symptom/diagnosis codes and version of the base ICPC-2 CodeSystem."""
    if base.get("resourceType") != "CodeSystem" or base.get("url") != SYSTEM:
        raise ValueError(f"Expected the base CodeSystem {SYSTEM}")
    if base.get("content") != "complete" or not base.get("version"):
        raise ValueError("Base ICPC-2 CodeSystem must be complete and state its version")
    codes = set()
    for concept in concepts(base):
        kinds = {p.get("valueCode") for p in concept.get("property", []) if p.get("code") == "kind"}
        if len(kinds) != 1:
            raise ValueError(f"Base ICPC-2 concept {concept.get('code')!r} must have exactly one kind")
        if kinds & OUT_OF_SCOPE_KINDS:
            continue
        if not CODE_RE.fullmatch(concept["code"]):
            raise ValueError(f"Unexpected symptom/diagnosis code {concept['code']!r} in base ICPC-2; review before publishing")
        codes.add(concept["code"])
    if not codes:
        raise ValueError("No ICPC symptom/diagnosis codes in the base CodeSystem")
    return codes, base["version"]


def load_base(reference_file: Path | None, cache_dir: Path) -> dict:
    if reference_file:
        return json.loads(reference_file.read_text(encoding="utf-8"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        return fetch_icd10_codesystem(TX_BASE, SYSTEM, str(cache_dir / "icpc-2.json"), force=True)
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc


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
            raise ValueError(f"KiAP code {code!r} is absent from the base ICPC-2 CodeSystem; review before publishing")
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


def validity(code: str, target: str, history: str) -> tuple[dt.date, dt.date, bool]:
    """KiAP forward table stores creation, last change, closure (YYYYMMDD each)."""
    known_typo = (code, target, history) == KNOWN_HISTORY_TYPO
    if not re.fullmatch(r"\d{24}", history) and not known_typo:
        raise ValueError(f"Invalid KiAP history for {code}/{target}: {history!r}")
    start = dt.datetime.strptime(history[:8], "%Y%m%d").date()
    end = dt.datetime.strptime(history[-8:], "%Y%m%d").date()
    if not known_typo:
        changed = dt.datetime.strptime(history[8:16], "%Y%m%d").date()
        if changed < start:
            raise ValueError(f"Last-change date precedes creation for {code}/{target}")
    if end < start:
        raise ValueError(f"Closing date precedes creation for {code}/{target}")
    return start, end, known_typo


def select_guidance(rows: list[dict], labels: dict[str, str], release: Release) -> tuple[dict, dict]:
    """Extract concept guidance from active preferred rows (KiAP spec §8.1.2).

    Additional-match rows can describe a specific ICD-10 diagnosis. Report their
    text separately instead of promoting it to an ICPC-wide property.
    """
    required = {"ICPC-kode", "ICD10-kode", "Udl", "Gyldig fra dato"}
    required.update(field for field, _, _ in GUIDANCE_FIELDS)
    if not rows or not required <= rows[0].keys():
        raise ValueError(f"Missing required guidance columns: {sorted(required)}")
    as_of = dt.date.fromisoformat(release.date)
    preferred = set()
    values = defaultdict(lambda: defaultdict(set))
    additional = {}
    for row in rows:
        code = (row.get("ICPC-kode") or "").strip()
        if code not in labels:
            continue
        if not required <= row.keys():
            raise ValueError(f"Missing required guidance columns for {code}")
        target = (row["ICD10-kode"] or "").strip().upper()
        flag = (row["Udl"] or "").strip()
        if flag not in {"0", "1"}:
            raise ValueError(f"Unknown preferred-match flag for {code}/{target}: {flag!r}")
        start, end, _ = validity(code, target, (row["Gyldig fra dato"] or "").strip())
        if not start <= as_of < end:
            continue
        properties = [
            {"code": prop, "valueString": text}
            for field, prop, _ in GUIDANCE_FIELDS
            if (text := " ".join((row[field] or "").split()))
        ]
        if flag == "1":
            preferred.add(code)
            for prop in properties:
                values[code][prop["code"]].add(prop["valueString"])
        else:
            key = (code, target)
            if key in additional and additional[key] != properties:
                raise ValueError(f"Conflicting additional-match guidance for {code}/{target}")
            additional[key] = properties
    missing = labels.keys() - preferred
    if missing:
        raise ValueError(f"No active preferred row for ICPC guidance: {', '.join(sorted(missing))}")
    selected = {}
    counts = {prop: 0 for _, prop, _ in GUIDANCE_FIELDS}
    for code in sorted(values):
        properties = []
        for _, prop, _ in GUIDANCE_FIELDS:
            texts = values[code][prop]
            if len(texts) > 1:
                raise ValueError(f"Conflicting preferred guidance for {code}/{prop}: {sorted(texts)}")
            if texts:
                properties.append({"code": prop, "valueString": next(iter(texts))})
                counts[prop] += 1
        if properties:
            selected[code] = properties
    return selected, {
        "guidance_concepts": len(selected),
        "guidance_property_counts": counts,
        "additional_match_guidance": [
            {"code": code, "sks_target": target, "properties": properties}
            for (code, target), properties in sorted(additional.items()) if properties
        ],
    }


def build_supplement(labels: dict[str, str], release: Release, table: str, resolved: dict,
                     guidance: dict[str, list[dict]], base_version: str) -> dict:
    conflict_note = ""
    if resolved:
        conflict_note = " Conflicting source labels are resolved as follows: " + "; ".join(
            f"{code}: {value}" for code, value in sorted(resolved.items())) + "."
    return {
        "resourceType": "CodeSystem", "id": "icpc2E-DK", "url": CANONICAL,
        "version": release.fhir_version, "name": "Icpc2DanishTranslations",
        "title": ("International Classification of Primary Care, second edition (1998), "
                  "Danish translations and coding guidance (from KiAP)"), "status": "active",
        "experimental": False, "date": release.date, "publisher": "HL7 Denmark",
        "contact": [{"name": "HL7 Denmark", "telecom": [
            {"system": "url", "value": "http://www.hl7.dk"},
            {"system": "email", "value": "dk-affiliate@hl7.dk"}]}],
        "description": (
            f"Danish-language designation and coding-guidance supplement for ICPC-2, containing {len(labels)} symptom and diagnosis codes. "
            "Process codes and Danish-only grouping codes are excluded. "
            f"The authoritative source is KiAP ICPC-2-DK {release.version}, "
            f"[{Path(urllib.parse.urlsplit(release.url).path).name}]({release.url}), "
            f"table {table}, fields ICPC-kode and ICPC-diagnose. "
            "Narrative properties " + ", ".join(prop for _, prop, _ in GUIDANCE_FIELDS)
            + " preserve Kriterier, Inkl, Ekskl, Overvej and Bemærk from preferred (Udl=1) rows valid on the "
            "source release date; the -da suffix distinguishes them from the English properties of the base system. "
            "Additional-match guidance is reported separately by the generator, not promoted to ICPC concept properties. "
            "Whitespace is normalized; cross-references remain source text, not executable rules."
            + conflict_note + f" Codes are checked against the base ICPC-2 CodeSystem, version {base_version}. "
            "Generated by scripts/icpc2_da_supplement.py. The version combines the KiAP release version "
            "with the source file's release date, which is also the resource date. "
            "[KiAP documentation and releases](" + KIAP_PAGE + ")."
        ),
        "copyright": COPYRIGHT,
        "jurisdiction": [{"coding": [{"system": "urn:iso:std:iso:3166", "code": "DK", "display": "Denmark"}]}],
        "content": "supplement", "supplements": f"{SYSTEM}|{base_version}", "count": len(labels),
        "property": [
            {"code": prop, "uri": f"{CANONICAL}#{prop}",
             "description": f"{description} Danish source field: {field}; active preferred KiAP rows only.",
             "type": "string"}
            for field, prop, description in GUIDANCE_FIELDS
        ],
        "concept": [{"code": code, "designation": [{"language": "da", "value": value}],
                     **({"property": guidance[code]} if guidance.get(code) else {})}
                    for code, value in sorted(labels.items())],
    }


def semantic_content(resource: dict) -> dict:
    # A source re-export alone does not justify a PR. Keep the last published
    # provenance until the labels or other substantive resource metadata change.
    result = {key: value for key, value in resource.items() if key not in {"version", "date", "meta", "description"}}
    if "concept" in result:
        result["concept"] = sorted(result["concept"], key=lambda c: c["code"])
    return result


def write_resource(resource: dict, output: Path, only_if_changed: bool) -> bool:
    if only_if_changed and output.exists():
        previous = json.loads(output.read_text(encoding="utf-8"))
        if semantic_content(previous) == semantic_content(resource):
            return False
    atomic_write(output, (json.dumps(resource, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return True


def load_source(source_url: str | None, source_file: Path | None,
                reference_file: Path | None, cache_dir: Path,
                table: str | None = None) -> tuple[Release, str, list[dict], dict, dict, dict]:
    """Acquire and validate one KiAP snapshot and the base CodeSystem, shared by both generators."""
    if source_file and not source_url:
        raise ValueError("--source-file requires --source-url")
    release = parse_release(source_url) if source_url else discover_release(fetch(KIAP_PAGE).decode("utf-8"))
    if source_file:
        database = source_file
    else:
        database = cache_dir / Path(urllib.parse.urlsplit(release.url).path).name
        atomic_write(database, fetch(release.url))
    base = load_base(reference_file, cache_dir)
    base_codes, base_version = reference_codes(base)
    table, rows = read_rows(database, table)
    labels, details = select_labels(rows, base_codes)
    return release, table, rows, labels, {**details, "base_version": base_version}, base


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", help="Specific dated KiAP .accdb URL; otherwise discover latest")
    parser.add_argument("--source-file", type=Path, help="Local .accdb, requires --source-url for provenance")
    parser.add_argument("--table", help="Override table selection if KiAP changes its schema")
    parser.add_argument("--reference-file", type=Path, help="Local base ICPC-2 CodeSystem JSON (offline); otherwise download from tx.fhir.org")
    parser.add_argument("--cache-dir", type=Path, default=Path(".icpc-kiap-cache"))
    parser.add_argument("--out", type=Path, default=Path("fhir/CodeSystem-icpc2E-DK.json"))
    parser.add_argument("--only-if-changed", action="store_true", help="Preserve existing output when only source stamps/provenance changed")
    parser.add_argument("--report", type=Path, help="Write source and validation details as JSON")
    args = parser.parse_args(argv)
    if args.source_file and not args.source_url:
        parser.error("--source-file requires --source-url")
    try:
        release, table, rows, labels, details, _ = load_source(
            args.source_url, args.source_file, args.reference_file, args.cache_dir, args.table)
        guidance, guidance_details = select_guidance(rows, labels, release)
        resource = build_supplement(labels, release, table, details["resolved_conflicts"], guidance,
                                    details["base_version"])
        changed = write_resource(resource, args.out, args.only_if_changed)
        report = {"source_url": release.url, "source_version": release.version, "source_date": release.date,
                  "table": table, "concepts": len(labels), "changed": changed, **details, **guidance_details}
        if args.report:
            atomic_write(args.report, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
                for key in ("source_url", "source_version", "source_date", "concepts"):
                    handle.write(f"{key}={report[key]}\n")
                handle.write(f"changed={str(changed).lower()}\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"ICPC generation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
