"""Offline tests for source selection and publishing safeguards."""
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/icpc2_da_supplement.py"
spec = importlib.util.spec_from_file_location("icpc", SCRIPT)
icpc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = icpc
spec.loader.exec_module(icpc)
URL = "https://web.kiap.dk/resources/files/icpc/systemhuse/ICPC_v4_4_20260629.accdb"


def row(code, label):
    return {"ICPC-kode": code, "ICPC-diagnose": label}


def guidance_row(code="A01", target="DR529", flag="1", **fields):
    return {**row(code, "Almen smerte"), "ICD10-kode": target, "Udl": flag,
            "Gyldig fra dato": "200801012026050125000101",
            **{field: "" for field, _, _ in icpc.GUIDANCE_FIELDS}, **fields}


class DiscoveryTests(unittest.TestCase):
    def test_finds_latest_numeric_version_not_first_link(self):
        html = '<a href="' + URL + '">current</a>'
        html += '<a href="/files/ICPC_v4_10_20270901.accdb">new</a>'
        html += '<a href="/files/ICPC_v4_9_20270301.accdb">old</a>'
        result = icpc.discover_release(html)
        self.assertEqual((result.version, result.date), ("4.10", "2027-09-01"))
        self.assertEqual(result.url, "https://www.kiap.dk/files/ICPC_v4_10_20270901.accdb")

    def test_revised_same_version_uses_later_date_and_deduplicates(self):
        later = URL.replace("20260629", "20260901")
        result = icpc.discover_release(f'<a href="{URL}"></a><a href="{later}"></a><a href="{later}"></a>')
        self.assertEqual(result.date, "2026-09-01")

    def test_missing_or_ambiguous_releases_fail(self):
        for html in ("<p>Unavailable</p>", f'<a href="{URL}"></a><a href="{URL.replace("web.", "www.")}"></a>'):
            with self.subTest(html=html), self.assertRaises(ValueError):
                icpc.discover_release(html)

    def test_invalid_source_is_rejected(self):
        for url in (URL.replace("20260629", "20260230"), URL.replace("web.kiap.dk", "example.com"), URL.replace("https:", "http:"), URL + "?x=1\nchanged=true", URL + "#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                icpc.parse_release(url)


class LabelTests(unittest.TestCase):
    def test_duplicates_whitespace_and_grouping_entries(self):
        rows = [row("A01", "Almen  smerte "), row("A01", "Almen smerte"), row("A00", "Chapter"), row("V00", "Unknown"), row("A30", "Process"), row(None, None)]
        selected, details = icpc.select_labels(rows, {"A01"})
        self.assertEqual(selected, {"A01": "Almen smerte"})
        self.assertEqual(details["excluded_codes"], ["A00", "A30", "V00"])

    def test_reviewed_conflicts_are_order_independent(self):
        rows = [row(code, label) for code, (labels, _) in icpc.RESOLUTIONS.items() for label in labels]
        forward = icpc.select_labels(rows, set(icpc.RESOLUTIONS))
        self.assertEqual(forward, icpc.select_labels(list(reversed(rows)), set(icpc.RESOLUTIONS)))
        self.assertEqual(forward[0], {c: value for c, (_, value) in icpc.RESOLUTIONS.items()})

    def test_resolved_upstream_conflict_uses_current_single_label(self):
        labels, details = icpc.select_labels([row("S12", "Ny godkendt tekst")], {"S12"})
        self.assertEqual(labels["S12"], "Ny godkendt tekst")
        self.assertEqual(details["resolved_conflicts"], {})

    def test_new_or_changed_conflicts_fail(self):
        cases = ([row("A01", "One"), row("A01", "Two")],
                 [row("P70", "Demens"), row("P70", "New unexpected label")])
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "Unreviewed conflicting"):
                icpc.select_labels(rows, {rows[0]["ICPC-kode"]})

    def test_unknown_codes_missing_codes_and_empty_labels_fail(self):
        cases = ([row("A01", "Pain"), row("A12", "Unknown")], [], [row("A01", "")], [row("", "Orphan")])
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                icpc.select_labels(rows, {"A01"})


class ReaderTests(unittest.TestCase):
    @patch.object(icpc, "mdb")
    def test_mapping_table_selection_and_csv_quoting(self, mdb):
        mdb.side_effect = ["DIFFERENS_ICPCKONV2_v4\nICPCKON2-v4_4_20260501\n", '"ICPC-kode","ICPC-diagnose"\n"A01","Smerte, almen"\n']
        table, rows = icpc.read_rows(Path("source.accdb"))
        self.assertEqual(table, "ICPCKON2-v4_4_20260501")
        self.assertEqual(rows, [row("A01", "Smerte, almen")])

    @patch.object(icpc, "mdb")
    def test_schema_change_and_multiple_tables_fail(self, mdb):
        mdb.side_effect = ["ICPCKON2-v4\nICPCKON2-v5\n"]
        with self.assertRaisesRegex(ValueError, "Expected one"):
            icpc.read_rows(Path("source.accdb"))
        mdb.side_effect = ["ICPCKON2-v4\n", "code,label\nA01,pain\n"]
        with self.assertRaisesRegex(ValueError, "lacks required columns"):
            icpc.read_rows(Path("source.accdb"))

    def test_international_reference_excludes_chapters_and_process_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "base.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("reference.xml", '<ClaML><Class code="A"/><Class code="A01"/><Class code="A30"/><Class code="-30"/><Class code="V00"/></ClaML>')
            self.assertEqual(icpc.reference_codes(archive), {"A01"})


class PublicationTests(unittest.TestCase):
    def resource(self):
        return icpc.build_supplement({"A01": "Almen smerte"}, icpc.parse_release(URL), "ICPCKON2-v4", {}, {})

    def test_regeneration_is_deterministic(self):
        self.assertEqual(self.resource(), self.resource())
        self.assertEqual(self.resource()["date"], "2026-06-29")
        self.assertEqual(self.resource()["version"], "2026-06-29")

    def test_no_change_keeps_published_bytes_but_real_label_change_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "cs.json"
            original = self.resource()
            icpc.write_resource(original, out, True)
            before = out.read_bytes()
            revised = copy.deepcopy(original)
            revised.update(version="2026-09-01", date="2026-09-01", description="Source re-export")
            self.assertFalse(icpc.write_resource(revised, out, True))
            self.assertEqual(out.read_bytes(), before)
            revised["concept"][0]["designation"][0]["value"] = "Ny tekst"
            self.assertTrue(icpc.write_resource(revised, out, True))
            self.assertEqual(json.loads(out.read_text()), revised)

    def test_guidance_only_changes_and_removal_trigger_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "cs.json"
            resource = self.resource()
            icpc.write_resource(resource, out, True)
            resource["concept"][0]["property"] = [{"code": "note", "valueString": "Dansk note"}]
            self.assertTrue(icpc.write_resource(resource, out, True))
            resource["concept"][0]["property"][0]["valueString"] = "Ændret note"
            self.assertTrue(icpc.write_resource(resource, out, True))
            del resource["concept"][0]["property"]
            self.assertTrue(icpc.write_resource(resource, out, True))

    @patch.object(icpc, "read_rows")
    @patch.object(icpc, "reference_codes", return_value={"A01"})
    def test_standalone_exports_guidance_and_preserves_output_on_conflict(self, _, read_rows):
        rows = [guidance_row(Kriterier="Danske kriterier", **{"Bemærk": "En note"})]
        read_rows.return_value = ("ICPCKON2-v4", rows)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "cs.json"
            args = ["--source-url", URL, "--source-file", "local.accdb",
                    "--reference-file", "local.zip", "--out", str(out)]
            self.assertEqual(icpc.main(args), 0)
            resource = json.loads(out.read_text())
            self.assertEqual(resource["concept"][0]["property"], [
                {"code": "criteria", "valueString": "Danske kriterier"},
                {"code": "note", "valueString": "En note"}])
            before = out.read_bytes()
            rows.append(guidance_row(target="DR520", Kriterier="Modstridende kriterier"))
            self.assertEqual(icpc.main(args), 1)
            self.assertEqual(out.read_bytes(), before)

    @patch.object(icpc, "read_rows", return_value=("ICPCKON2-v4", [row("A01", "One"), row("A01", "Two")]))
    @patch.object(icpc, "reference_codes", return_value={"A01"})
    def test_failed_generation_does_not_overwrite_published_file(self, *_):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "cs.json"
            out.write_text("original")
            result = icpc.main(["--source-url", URL, "--source-file", "local.accdb", "--reference-file", "local.zip", "--out", str(out)])
            self.assertEqual(result, 1)
            self.assertEqual(out.read_text(), "original")


class GuidanceTests(unittest.TestCase):
    def select(self, rows, labels=None):
        return icpc.select_guidance(rows, labels or {"A01": "Almen smerte"}, icpc.parse_release(URL))

    def test_all_fields_are_typed_properties_and_empty_values_are_omitted(self):
        rows = [guidance_row(Kriterier="  Dansk\n kriterium ", Inkl="Inklusion",
                             Ekskl="Anden tilstand A02", Overvej="A03",
                             **{"Bemærk": "Dobbeltkod A04"}),
                guidance_row(code="A02")]
        labels = {"A01": "Almen smerte", "A02": "Andet"}
        guidance, report = self.select(rows, labels)
        resource = icpc.build_supplement(labels, icpc.parse_release(URL), "ICPCKON2-v4", {}, guidance)
        self.assertEqual(report["guidance_concepts"], 1)
        self.assertEqual(report["guidance_property_counts"], {
            "criteria": 1, "inclusion": 1, "exclusion": 1, "consider": 1, "note": 1})
        self.assertEqual(guidance["A01"][0], {"code": "criteria", "valueString": "Dansk kriterium"})
        self.assertNotIn("property", resource["concept"][1])
        for definition, value in zip(resource["property"], guidance["A01"]):
            self.assertEqual(definition["type"], "string")
            self.assertEqual(definition["code"], value["code"])
            self.assertEqual(definition["uri"], icpc.CANONICAL + "#" + value["code"])

    def test_additional_guidance_is_reported_without_becoming_concept_properties(self):
        rows = [guidance_row(Inkl="Generel inklusion"),
                guidance_row(target="DR521", flag="0", Kriterier="Specifikt kriterium", Inkl="Specifik inklusion")]
        guidance, report = self.select(rows)
        self.assertEqual(guidance["A01"], [{"code": "inclusion", "valueString": "Generel inklusion"}])
        self.assertEqual(report["additional_match_guidance"], [{
            "code": "A01", "sks_target": "DR521", "properties": [
                {"code": "criteria", "valueString": "Specifikt kriterium"},
                {"code": "inclusion", "valueString": "Specifik inklusion"}]}])
        self.assertEqual(self.select(rows), self.select(list(reversed(rows)) + [rows[0], rows[1]]))

    def test_preferred_duplicates_and_blank_rows_merge_but_conflicts_fail(self):
        rows = [guidance_row(Kriterier="Samme tekst"), guidance_row(target="DR520"),
                guidance_row(target="DR521", Kriterier="Samme  tekst")]
        self.assertEqual(self.select(rows)[0]["A01"], [{"code": "criteria", "valueString": "Samme tekst"}])
        rows[-1]["Kriterier"] = "Anden tekst"
        with self.assertRaisesRegex(ValueError, "Conflicting preferred guidance"):
            self.select(rows)

    def test_validity_boundaries_do_not_merge_old_or_future_guidance(self):
        rows = [guidance_row(Kriterier="Aktuel", **{"Gyldig fra dato": "202606292026062925000101"}),
                guidance_row(Kriterier="Gammel", **{"Gyldig fra dato": "200801012026050120260629"}),
                guidance_row(Kriterier="Fremtidig", **{"Gyldig fra dato": "202606302026063025000101"}),
                guidance_row(flag="0", Kriterier="Udløbet", **{"Gyldig fra dato": "200801012026050120260629"})]
        guidance, report = self.select(rows)
        self.assertEqual(guidance["A01"], [{"code": "criteria", "valueString": "Aktuel"}])
        self.assertEqual(report["additional_match_guidance"], [])
        with self.assertRaisesRegex(ValueError, "No active preferred row"):
            self.select(rows[1:])

    def test_missing_schema_unknown_flags_and_invalid_dates_fail(self):
        missing = guidance_row()
        del missing["Bemærk"]
        for rows in ([], [missing], [guidance_row(flag="2")],
                     [guidance_row(**{"Gyldig fra dato": "bad"})]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.select(rows)

    def test_known_history_typo_remains_supported_and_out_of_scope_rows_are_skipped(self):
        rows = [guidance_row(), guidance_row(target="DR521", flag="0", Kriterier="Specifikt",
                **{"Gyldig fra dato": icpc.KNOWN_HISTORY_TYPO[2]}), row("A00", "Kapitel"), {}]
        guidance, report = self.select(rows)
        self.assertEqual(guidance, {})
        self.assertEqual(len(report["additional_match_guidance"]), 1)


if __name__ == "__main__":
    unittest.main()
