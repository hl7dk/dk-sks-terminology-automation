"""Offline tests for KiAP forward mappings and publication safeguards."""
import copy
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import icpc2_icd10_conceptmap as mapping

URL = "https://web.kiap.dk/resources/files/icpc/systemhuse/ICPC_v4_4_20260629.accdb"
RELEASE = mapping.kiap.parse_release(URL)


def codesystem(system, codes):
    return {"resourceType": "CodeSystem", "url": system, "version": "test-version",
            "concept": codes}


def catalogs():
    return [codesystem(mapping.ICD10_SYSTEM, [{"code": "R52.9", "display": "Pain"}]),
            codesystem(mapping.EXTENSIONS_SYSTEM, [
                {"code": "R67", "display": "Danish extension", "property": [
                    {"code": "sksCode", "valueString": "DR670"}]}]),
            codesystem(mapping.SKS_SYSTEM, [{"code": "DX999", "display": "SKS only"}])]


def base(mapped):
    """Base ICPC-2 CodeSystem with its WONCA mapping in icd10 properties."""
    return {"resourceType": "CodeSystem", "url": mapping.kiap.SYSTEM, "version": "7.0", "concept": [
        {"code": "A", "concept": [
            {"code": code, "property": [{"code": "kind", "valueCode": "symptom"}] + [
                {"code": "icd10", "valueCoding": {"system": mapping.ICD10_SYSTEM, "code": target}}
                for target in targets]}
            for code, targets in mapped.items()]}]}


def row(code="A01", target="DR529", flag="1", history="200801012026050125000101"):
    return {"ICPC-kode": code, "ICPC-diagnose": "Almen smerte", "ICD10-kode": target,
            "ICD10-diagnose": "Smerte UNS", "Udl": flag, "Gyldig fra dato": history,
            **{field: "" for field, _, _ in mapping.kiap.GUIDANCE_FIELDS}}


def build(rows=None, labels=None):
    return mapping.build_conceptmap(rows or [row()], labels or {"A01": "Almen smerte"},
                                    RELEASE, "ICPCKON2-v4", mapping.Targets(*catalogs()))


class TargetTests(unittest.TestCase):
    def test_resolves_real_codes_and_keeps_systems_separate(self):
        targets = mapping.Targets(*catalogs())
        for raw, expected in [("DR529", (mapping.ICD10_SYSTEM, "R52.9")),
                              ("DR670", (mapping.EXTENSIONS_SYSTEM, "R67")),
                              ("DX999", (mapping.SKS_SYSTEM, "DX999"))]:
            self.assertEqual(targets.resolve(raw)[:2], expected)
        for unknown in ("DR999", "R529", ""):
            with self.subTest(unknown=unknown), self.assertRaises(ValueError):
                targets.resolve(unknown)

    def test_rejects_wrong_system_missing_version_and_ambiguous_extension(self):
        for alteration in ("url", "version", "duplicate"):
            data = catalogs()
            if alteration == "duplicate":
                duplicate = copy.deepcopy(data[1]["concept"][0])
                duplicate["code"] = "R67B"
                data[1]["concept"].append(duplicate)
            else:
                data[0][alteration] = ""
            with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                mapping.Targets(*data)


class MappingTests(unittest.TestCase):
    def test_labels_flags_and_conservative_relationships_in_separate_groups(self):
        rows = [row(), row(target="DR670", flag="0"), row(target="DX999", flag="0")]
        resource, report = build(rows)
        self.assertEqual(report["mapping_pairs"], 3)
        self.assertEqual(report["preferred_matches"], 1)
        self.assertEqual(resource["language"], "da")
        for group in resource["group"]:
            self.assertNotIn("|", group["target"])
            self.assertEqual(group["targetVersion"], "test-version")
            self.assertEqual(group["element"][0]["display"], "Almen smerte")
            target = group["element"][0]["target"][0]
            self.assertEqual(target["display"], "Smerte UNS")
            self.assertEqual(target["equivalence"], "relatedto")
            self.assertIn("Udl=", target["comment"])
        self.assertEqual(build(rows), build(list(reversed(rows))))

    def test_one_target_does_not_imply_equivalence(self):
        resource, _ = build()
        self.assertEqual(resource["group"][0]["element"][0]["target"][0]["equivalence"], "relatedto")

    def test_duplicate_pairs_deduplicate_but_conflicting_flags_fail(self):
        _, report = build([row(), row()])
        self.assertEqual(report["mapping_pairs"], 1)
        self.assertEqual(report["preferred_matches"], 1)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            build([row(), row(flag="0")])

    def test_missing_coverage_bad_labels_and_flags_fail(self):
        with self.assertRaisesRegex(ValueError, "No active forward mapping"):
            build(labels={"A01": "Pain", "W91": "Missing"})
        for key, value in [("ICPC-kode", "BAD"), ("ICD10-kode", ""),
                           ("ICD10-diagnose", "  "), ("Udl", "2")]:
            item = row()
            item[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                build([item])

    def test_only_blank_and_unmapped_grouping_rows_are_skipped(self):
        _, report = build([row(), {}, {"ICPC-kode": "A00"}])
        self.assertEqual(report["mapping_pairs"], 1)
        for code in ("A00", "A30"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                build([row(), row(code=code)])

    def test_validity_filters_at_release_date_before_resolving_targets(self):
        rows = [row(), row(target="DUNKNOWN", history="200801012026050120260629"),
                row(target="DUNKNOWN", history="202606302026063025000101")]
        _, report = build(rows)
        self.assertEqual(report["mapping_pairs"], 1)
        self.assertEqual(len(report["excluded_by_validity"]), 2)
        _, report = build([row(history="202606292026062925000101")])
        self.assertEqual(report["mapping_pairs"], 1)

    def test_history_errors_fail_except_exact_reviewed_typo(self):
        start, end, warning = mapping.kiap.validity(*mapping.kiap.KNOWN_HISTORY_TYPO)
        self.assertEqual((start, end, warning), (dt.date(2024, 7, 1), mapping.kiap.OPEN_END, True))
        for history in ("", "20240701206010125000102", "200802302026050125000101",
                        "202606292026010125000101", "200801012026050120070101"):
            with self.subTest(history=history), self.assertRaises(ValueError):
                mapping.kiap.validity("A01", "DR521", history)


class BaseComparisonTests(unittest.TestCase):
    def test_classifies_codes_and_lists_pairs_missing_from_base(self):
        def element(code, *targets):
            return {"code": code, "target": [{"code": target} for target in targets]}
        resource = {"group": [
            {"target": mapping.ICD10_SYSTEM, "element": [
                element("A01", "R52.9"), element("A02", "R68.8"), element("A03", "R50.8", "R50.9"),
                element("A04", "G93.3")]},
            {"target": mapping.EXTENSIONS_SYSTEM, "element": [element("A04", "R53.9"), element("A05", "R53.9E")]}]}
        result = mapping.compare_with_base(resource, base({
            "A01": ["R52.9"], "A02": ["R68.8", "R68.0"], "A03": ["R50.8"], "A04": ["R53"],
            "A05": ["R53"], "A99": ["R69"]}))
        self.assertEqual(result["codes"], {"identical": 1, "conceptmap_subset_of_base": 2,
                                           "conceptmap_superset_of_base": 1, "differing": 1})
        self.assertEqual((result["base_pairs"], result["conceptmap_international_pairs"],
                          result["conceptmap_other_pairs"]), (6, 5, 2))
        self.assertEqual(result["codes_without_international_target"], 1)
        self.assertEqual(result["pairs_only_in_base"], 3)
        self.assertEqual(result["pairs_only_in_conceptmap"], [
            {"code": "A03", "target": "R50.9"}, {"code": "A04", "target": "G93.3"}])


class PublicationTests(unittest.TestCase):
    def test_content_change_writes_but_source_reexport_does_not(self):
        resource, _ = build()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "map.json"
            mapping.kiap.write_resource(resource, out, True)
            original = out.read_bytes()
            changed = copy.deepcopy(resource)
            changed.update(version="2027-01-01", date="2027-01-01", description="Reexport")
            self.assertFalse(mapping.kiap.write_resource(changed, out, True))
            self.assertEqual(out.read_bytes(), original)
            changed["group"][0]["element"][0]["target"][0]["comment"] = "New flag"
            self.assertTrue(mapping.kiap.write_resource(changed, out, True))

    def test_shared_generation_and_failure_preserve_both_outputs(self):
        source = (RELEASE, "ICPCKON2-v4", [row()], {"A01": "Almen smerte"},
                  {"resolved_conflicts": {}, "base_version": "7.0"}, base({"A01": ["R52.9"]}))
        source[2][0]["Kriterier"] = "Danske kriterier"
        with tempfile.TemporaryDirectory() as directory, patch.object(mapping.kiap, "load_source", return_value=source) as load:
            root = Path(directory)
            files = []
            for index, catalog in enumerate(catalogs()):
                path = root / f"catalog{index}.json"
                path.write_text(json.dumps(catalog))
                files.append(str(path))
            output, supplement = root / "map.json", root / "supplement.json"
            args = ["--icd10-file", files[0], "--extensions-file", files[1], "--sks-file", files[2],
                    "--out", str(output), "--supplement-out", str(supplement), "--only-if-changed"]
            with patch.dict(mapping.os.environ, {"GITHUB_OUTPUT": str(root / "outputs")}):
                self.assertEqual(mapping.main(args), 0)
                load.assert_called_once()
                self.assertIn("mapping_pairs=1", (root / "outputs").read_text())
                self.assertIn("base_identical_codes=1\nbase_only_pairs=0\nkiap_only_pairs=0",
                              (root / "outputs").read_text())
                self.assertEqual(json.loads(supplement.read_text())["concept"][0]["property"],
                                 [{"code": "criteria-da", "valueString": "Danske kriterier"}])
                self.assertEqual(json.loads(output.read_text())["version"], "4.4.20260629")
                self.assertEqual(json.loads(supplement.read_text())["supplements"], mapping.kiap.SYSTEM + "|7.0")
                before = output.read_bytes(), supplement.read_bytes()
                # A valid map with invalid guidance must preserve both published artifacts.
                del source[2][0]["Bemærk"]
                self.assertEqual(mapping.main(args), 1)
                self.assertEqual((output.read_bytes(), supplement.read_bytes()), before)
                source[2][0]["Bemærk"] = ""
                source[2][0]["ICD10-kode"] = "DUNKNOWN"
                self.assertEqual(mapping.main(args), 1)
                self.assertEqual((output.read_bytes(), supplement.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
