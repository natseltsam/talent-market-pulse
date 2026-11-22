"""Tests for function_map.py. These run on every pull request, so a bad override can't be merged."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import function_map as fm  # noqa: E402


def write_overrides(text):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    tmp.write(text)
    tmp.close()
    return Path(tmp.name)


class OverrideFileTests(unittest.TestCase):
    def test_committed_overrides_file_is_valid(self):
        # Loading raises on an unknown function or a duplicate row
        overrides = fm.load_overrides()
        for function in overrides.values():
            self.assertIn(function, fm.ALLOWED_FUNCTIONS)

    def test_typo_in_function_is_rejected(self):
        path = write_overrides("company,department,function,suggested_by,confidence,reason\n"
                               "acme,Widgets,Enginering,Claude,high,typo\n")
        with self.assertRaises(ValueError):
            fm.load_overrides(path)

    def test_duplicate_row_is_rejected(self):
        path = write_overrides("company,department,function,suggested_by,confidence,reason\n"
                               "acme,Widgets,Engineering,Claude,high,a\n"
                               "acme,Widgets,Finance,Claude,high,b\n")
        with self.assertRaises(ValueError):
            fm.load_overrides(path)


class MappingTests(unittest.TestCase):
    def test_override_beats_rule(self):
        overrides = {("acme", "Security Engineering"): "Engineering"}
        self.assertEqual(fm.map_function("acme", "Security Engineering", overrides), ("Engineering", "override"))

    def test_override_is_company_specific(self):
        overrides = {("acme", "Applied AI"): "Sales & GTM"}
        self.assertEqual(fm.map_function("other_co", "Applied AI", overrides), ("Engineering", "rule"))

    def test_override_matches_cleaned_department(self):
        # Okta-style cost-center suffixes are stripped before matching
        overrides = {("okta", "Widgets"): "Finance"}
        self.assertEqual(fm.map_function("okta", "Widgets-123", overrides), ("Finance", "override"))

    def test_reviewed_other_is_not_unmapped(self):
        overrides = {("acme", "Talent Network"): "Other"}
        self.assertEqual(fm.map_function("acme", "Talent Network", overrides), ("Other", "override"))

    def test_unmatched_department_is_flagged(self):
        self.assertEqual(fm.map_function("acme", "Zzyzx", {}), ("Other", "none"))

    def test_rule_order_spot_checks(self):
        cases = {
            "Security Engineering": "Security & Trust",
            "Accounting Operations": "Finance",
            "Key Accounts Restaurants": "Sales & GTM",
            "Product Marketing": "Marketing & Comms",
            "BT People Legal and Finance Technology": "IT & Business Systems",
        }
        for department, expected in cases.items():
            with self.subTest(department=department):
                self.assertEqual(fm.classify_function(department), expected)


if __name__ == "__main__":
    unittest.main()
