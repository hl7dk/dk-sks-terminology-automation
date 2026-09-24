#!/usr/bin/env python3
"""Build the ICPC-2 -> ICD-10 ConceptMap from KiAP's forward mapping table.

Uses ICPCKON2, not the distinct reverse ICD10-ICPC register. Requires mdbtools.
With --supplement-out, generate both ICPC artifacts from the same database.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from pathlib import Path

import icpc2_da_supplement as kiap
from sks_icd10_diff import fetch_icd10_codesystem

ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10"
EXTENSIONS_SYSTEM = "http://hl7.dk/fhir/core/CodeSystem/icd10-danish-extensions"
SKS_SYSTEM = "urn:oid:1.2.208.176.2.4.12"
CANONICAL = "http://hl7.dk/fhir/core/ConceptMap/icpc2-icd10"
OPEN_END = dt.date(2500, 1, 1)
# KiAP 4.4 has a missing digit in the middle (last-modified) date for this row.
# The valid-from and closing dates are intact. Do not invent a modified date.
KNOWN_HISTORY_TYPO = ("A01", "DR521", "20240701206010125000101")


def concepts(resource: dict):
    for concept in resource.get("concept", []):
        yield concept
        yield from concepts(concept)


class Targets:
    """Resolve an exact source SKS code without inventing WHO ICD-10 codes."""
    def __init__(self, icd10: dict, extensions: dict, sks: dict):
        self.resources = {ICD10_SYSTEM: icd10, EXTENSIONS_SYSTEM: extensions, SKS_SYSTEM: sks}
        self.codes = {}
        for system, resource in self.resources.items():
            if resource.get("resourceType") != "CodeSystem" or resource.get("url") != system:
                raise ValueError(f"Expected CodeSystem {system}")
            if not resource.get("version") or not resource.get("concept"):
                raise ValueError(f"Target CodeSystem {system} must include its version and concepts")
            entries = list(concepts(resource))
            self.codes[system] = {c["code"]: c for c in entries}
            if len(self.codes[system]) != len(entries):
                raise ValueError(f"Duplicate codes in {system}")
        self.extensions_by_sks = {}
        for concept in self.codes[EXTENSIONS_SYSTEM].values():
            for prop in concept.get("property", []):
                if prop.get("code") == "sksCode":
                    source = prop.get("valueCode") or prop.get("valueString")
                    if source in self.extensions_by_sks:
                        raise ValueError(f"Ambiguous extension mapping for {source}")
                    self.extensions_by_sks[source] = concept["code"]

    def resolve(self, sks_code: str) -> tuple[str, str, str]:
        if not re.fullmatch(r"D[A-Z][A-Z0-9]+", sks_code):
            raise ValueError(f"Expected D-prefixed SKS diagnosis code, got {sks_code!r}")
        plain = sks_code[1:]
        dotted = plain[:3] + "." + plain[3:] if len(plain) > 3 else plain
        for code in (dotted, plain):
            if code in self.codes[ICD10_SYSTEM]:
                return ICD10_SYSTEM, code, self.codes[ICD10_SYSTEM][code].get("display", "")
        code = self.extensions_by_sks.get(sks_code)
        if code:
            return EXTENSIONS_SYSTEM, code, self.codes[EXTENSIONS_SYSTEM][code].get("display", "")
        if sks_code in self.codes[SKS_SYSTEM]:
            return SKS_SYSTEM, sks_code, self.codes[SKS_SYSTEM][sks_code].get("display", "")
        raise ValueError(f"Unresolved KiAP target {sks_code}; update/review target CodeSystems before publishing")


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


def build_conceptmap(rows: list[dict], labels: dict[str, str], release: kiap.Release,
                     table: str, targets: Targets) -> tuple[dict, dict]:
    as_of = dt.date.fromisoformat(release.date)
    grouped = defaultdict(lambda: defaultdict(dict))
    seen = set()
    excluded = []
    warnings = []
    mapped = set()
    preferred = 0
    for row in rows:
        code = (row.get("ICPC-kode") or "").strip()
        raw_target = (row.get("ICD10-kode") or "").strip().upper()
        if not code and not raw_target:
            continue
        if re.fullmatch(r"[ABDFHKLNPRSTUVWXYZ]00", code) and not raw_target:
            continue
        if code not in labels or not raw_target:
            raise ValueError(f"Invalid forward mapping row: {code!r}/{raw_target!r}")
        flag = (row.get("Udl") or "").strip()
        if flag not in {"0", "1"}:
            raise ValueError(f"Unknown preferred-match flag for {code}/{raw_target}: {flag!r}")
        history = (row.get("Gyldig fra dato") or "").strip()
        start, end, typo = validity(code, raw_target, history)
        if typo:
            warnings.append(f"{code}/{raw_target}: malformed last-change date in {history}; intact validity endpoints used")
        if not start <= as_of < end:
            excluded.append({"code": code, "sks_target": raw_target, "history": history})
            continue
        system, target_code, _ = targets.resolve(raw_target)
        display = " ".join((row.get("ICD10-diagnose") or "").split())
        if not display:
            raise ValueError(f"Empty KiAP target label for {code}/{raw_target}")
        comment = "KiAP Udl=" + flag + (": preferred ICD-10 match." if flag == "1" else ": additional ICD-10 match.")
        comment += f" SKS source code: {raw_target}. Valid from {start.isoformat()}."
        if end != OPEN_END:
            comment += f" Closing date: {end.isoformat()}."
        target = {"code": target_code, "equivalence": "relatedto", "comment": comment}
        if display:
            target["display"] = display
        previous = grouped[system][code].get(target_code)
        if previous is not None and previous != target:
            raise ValueError(f"Conflicting mapping rows for {code}/{raw_target}")
        key = (code, system, target_code)
        if key not in seen:
            preferred += flag == "1"
            seen.add(key)
        grouped[system][code][target_code] = target
        mapped.add(code)
    missing = labels.keys() - mapped
    if missing:
        raise ValueError(f"No active forward mapping for ICPC codes: {', '.join(sorted(missing))}")
    groups = []
    for system in sorted(grouped):
        groups.append({
            "source": kiap.SYSTEM, "target": system,
            "targetVersion": targets.resources[system]["version"],
            "element": [{"code": code, "display": labels[code],
                         "target": [mapping[c] for c in sorted(mapping)]}
                        for code, mapping in sorted(grouped[system].items())],
        })
    description = (
        f"ICPC-2 to Danish ICD-10/SKS mappings from KiAP ICPC-2-DK {release.version}, "
        f"[{Path(release.url).name}]({release.url}), table {table}. "
        "Uses KiAP's forward ICPCKON2 register, not an inversion or union of the separate ICD10-ICPC register. "
        f"Includes mappings valid on the source release date {release.date}; "
        "creation and closing dates are taken from the first and last eight digits of Gyldig fra dato. "
        "The closing date is exclusive. International ICD-10, Danish extensions, and SKS-only targets "
        "are emitted in separate groups under their own systems. KiAP's preferred/additional match flag "
        "(Udl) is retained in target comments. Relationships are 'relatedto': neither mapping count "
        "nor the preferred-match flag is treated as a clinical equivalence assertion. "
        "Source and target labels use KiAP Danish texts, with whitespace normalized. "
        "Generated by scripts/icpc2_icd10_conceptmap.py. Version and date identify the KiAP release."
    )
    if warnings:
        description += " Source history note: " + "; ".join(warnings) + "."
    resource = {
        "resourceType": "ConceptMap", "id": "icpc2-icd10", "url": CANONICAL,
        "version": release.date, "name": "Icpc2ToIcd10", "title": "ICPC-2 to ICD-10 (Danish, from KiAP)",
        "status": "draft", "experimental": True, "date": release.date, "language": "da",
        "publisher": "HL7 Denmark", "jurisdiction": [{"coding": [
            {"system": "urn:iso:std:iso:3166", "code": "DK", "display": "Denmark"}]}],
        "description": description, "copyright": kiap.COPYRIGHT.replace("this supplement", "this ConceptMap"),
        "group": groups,
    }
    report = {"mapped_codes": len(mapped), "mapping_pairs": len(seen), "preferred_matches": preferred,
              "target_counts": {system: sum(len(mapping) for mapping in codes.values())
                                for system, codes in sorted(grouped.items())},
              "excluded_by_validity": excluded, "source_warnings": warnings}
    return resource, report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", help="Specific KiAP release; default: discover latest")
    parser.add_argument("--source-file", type=Path, help="Local Access database (requires --source-url)")
    parser.add_argument("--reference-file", type=Path, help="Local ICPC-2e v7.0 ZIP for offline runs")
    parser.add_argument("--table", help="Override the ICPCKON2 table name")
    parser.add_argument("--cache-dir", type=Path, default=Path(".icpc-kiap-cache"))
    parser.add_argument("--icd10-file", type=Path, help="WHO ICD-10 CodeSystem JSON; otherwise download from tx.fhir.org")
    parser.add_argument("--extensions-file", type=Path, default=Path("fhir/CodeSystem-icd10-danish-extensions.json"))
    parser.add_argument("--sks-file", type=Path, default=Path("fhir/CodeSystem-sks-diagnoses.json"))
    parser.add_argument("--out", type=Path, default=Path("fhir/ConceptMap-icpc2-icd10.json"))
    parser.add_argument("--supplement-out", type=Path, help="Also regenerate the supplement from this same KiAP snapshot")
    parser.add_argument("--only-if-changed", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.source_file and not args.source_url:
        parser.error("--source-file requires --source-url")
    try:
        release, table, rows, labels, details = kiap.load_source(
            args.source_url, args.source_file, args.reference_file, args.cache_dir, args.table)
        required = {"ICPC-kode", "ICD10-kode", "ICD10-diagnose", "Udl", "Gyldig fra dato"}
        if not rows or not required <= rows[0].keys():
            raise ValueError(f"{table} lacks required forward mapping columns: {sorted(required)}")
        if args.icd10_file:
            icd10 = json.loads(args.icd10_file.read_text(encoding="utf-8"))
        else:
            args.cache_dir.mkdir(parents=True, exist_ok=True)
            icd10 = fetch_icd10_codesystem("https://tx.fhir.org/r4", ICD10_SYSTEM,
                                         str(args.cache_dir / "icd10.json"), force=True)
        targets = Targets(icd10, json.loads(args.extensions_file.read_text(encoding="utf-8")),
                          json.loads(args.sks_file.read_text(encoding="utf-8")))
        resource, stats = build_conceptmap(rows, labels, release, table, targets)
        # Validate both artifacts before changing either published file.
        supplement = kiap.build_supplement(labels, release, table, details["resolved_conflicts"])
        map_changed = kiap.write_resource(resource, args.out, args.only_if_changed)
        supplement_changed = False
        if args.supplement_out:
            supplement_changed = kiap.write_resource(supplement, args.supplement_out, args.only_if_changed)
        report = {"source_url": release.url, "source_version": release.version, "source_date": release.date,
                  "table": table, "concepts": len(labels), "conceptmap_changed": map_changed,
                  "supplement_changed": supplement_changed, "changed": map_changed or supplement_changed,
                  **details, **stats}
        if args.report:
            kiap.atomic_write(args.report, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
                for key in ("source_url", "source_version", "source_date", "concepts", "mapping_pairs"):
                    handle.write(f"{key}={report[key]}\n")
                handle.write(f"changed={str(report['changed']).lower()}\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, ET.ParseError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        print(f"KiAP mapping generation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
