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
        return icpc.build_supplement({"A01": "Almen smerte"}, icpc.parse_release(URL), "ICPCKON2-v4", {})

    def test_regeneration_is_deterministic(self):
        self.assertEqual(self.resource(), self.resource())
        self.assertEqual(self.resource()["date"], "2026-06-29")
        self.assertEqual(self.resource()["version"], "2026-06-29")

    def test_no_change_keeps_published_bytes_but_real_label_change_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "cs.json"
            original = self.resource()
            icpc.write_supplement(original, out, True)
            before = out.read_bytes()
            revised = copy.deepcopy(original)
            revised.update(version="2026-09-01", date="2026-09-01", description="Source re-export")
            self.assertFalse(icpc.write_supplement(revised, out, True))
            self.assertEqual(out.read_bytes(), before)
            revised["concept"][0]["designation"][0]["value"] = "Ny tekst"
            self.assertTrue(icpc.write_supplement(revised, out, True))
            self.assertEqual(json.loads(out.read_text()), revised)

    @patch.object(icpc, "read_rows", return_value=("ICPCKON2-v4", [row("A01", "One"), row("A01", "Two")]))
    @patch.object(icpc, "reference_codes", return_value={"A01"})
    def test_failed_generation_does_not_overwrite_published_file(self, *_):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "cs.json"
            out.write_text("original")
            result = icpc.main(["--source-url", URL, "--source-file", "local.accdb", "--reference-file", "local.zip", "--out", str(out)])
            self.assertEqual(result, 1)
            self.assertEqual(out.read_text(), "original")


if __name__ == "__main__":
    unittest.main()
