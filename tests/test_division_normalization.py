"""BUG-126: Division labels are canonicalised for comparison and display.

Roster/account data can contain both ``"1"`` and ``"Division 1"`` for the
same cohort. Filters, groups and faculty scope must treat them as equal.
"""

import json

import pandas as pd

from app import auth, views
from app.divisions import division_key, normalize_division
from app.ui_helpers import apply_pair_filter, apply_value_filter


class TestNormalizeDivision:
    def test_common_label_styles(self):
        assert normalize_division("1") == "1"
        assert normalize_division("Division 1") == "1"
        assert normalize_division("division 1") == "1"
        assert normalize_division("Div. 1") == "1"
        assert normalize_division(" Division A ") == "A"
        assert normalize_division("1.0") == "1"

    def test_missing_values(self):
        for value in (None, "", "nan", "none", float("nan"), "N/A", "<NA>"):
            assert normalize_division(value) == ""

    def test_comparison_key(self):
        assert division_key("Division 1") == division_key("1")
        assert division_key("Division A") == division_key("a")


class TestDivisionFilters:
    def _frame(self):
        return pd.DataFrame(
            {
                "Division": ["1", "Division 1", "2", "Division A"],
                "Batch": ["1", "2", "1", "1"],
            }
        )

    def test_value_filter_matches_both_stored_styles(self):
        df = self._frame()
        for selected in ("1", "Division 1", "Div. 1"):
            out = apply_value_filter(df, "Division", selected)
            assert sorted(out["Division"].tolist()) == ["1", "Division 1"]

    def test_value_filter_multi_select(self):
        out = apply_value_filter(self._frame(), "Division", "1,2")
        assert sorted(out["Division"].tolist()) == sorted(["1", "Division 1", "2"])

    def test_pair_filter_canonicalises_division(self):
        out = apply_pair_filter(self._frame(), ["1|2"])
        assert out["Division"].tolist() == ["Division 1"]
        out = apply_pair_filter(self._frame(), ["Division 1|1"])
        assert out["Division"].tolist() == ["1"]

    def test_pairs_list_canonicalises_legacy_urls(self):
        assert views._pairs_list("Division 1|1,2|3") == ["1|1", "2|3"]
        assert views._pairs_list([("Division 1", "1")]) == ["1|1"]


class TestDivisionOptionsAndGroups:
    def test_dist_options_dedupes_stored_styles(self):
        options = views.dist_options(["1", "Division 1", "2", "all"], normalize_division)
        assert options == ["All", "1", "2"]

    def test_division_groups_collapse_into_one_entry(self):
        df = pd.DataFrame(
            {
                "Division": ["1", "Division 1", "2"],
                "Batch": ["1", "2", "1"],
            }
        )
        groups = views.division_batch_groups(df)
        by_key = {g["key"]: g for g in groups}
        assert set(by_key) == {"1", "2"}
        assert by_key["1"]["label"] == "Division 1"
        assert by_key["1"]["division"] == "1"
        assert by_key["1"]["batches"] == ["1", "2"]

    def test_selected_divisions_are_canonical(self):
        assert views._division_filter_list("Division 1,1,2") == ["1", "2"]


class TestFacultyDivisionScope:
    def test_valid_division_accepts_legacy_label(self):
        assert auth.valid_division("1")
        assert auth.valid_division("Division 1")
        assert auth.valid_division("Div. 14")
        assert not auth.valid_division("15")
        assert not auth.valid_division("Division 15")

    def test_signup_batch_helpers_normalise_label(self):
        assert auth.signup_practical_batches("Division 15") == ("1", "2", "3")

    def test_parse_teaching_merges_legacy_and_canonical_keys(self):
        raw = json.dumps(
            {
                "Division 1": {"1": []},
                "1": {"2": ["Semester 3"]},
            }
        )
        assert auth.parse_faculty_teaching(raw) == {
            "1": {"1": [], "2": ["Semester 3"]}
        }

    def test_teaching_includes_tolerates_either_key_style(self):
        canonical = {"1": {"1": []}}
        legacy = {"Division 1": {"1": ["Semester 5"]}}
        assert auth.teaching_includes(canonical, "Division 1", "1")
        assert auth.teaching_includes(legacy, "1", "1", "Semester 5")
        assert not auth.teaching_includes(legacy, "1", "1", "Semester 3")

    def test_teaching_display_and_rows_are_canonical(self):
        teaching = {"Division 1": {"1": []}, "1": {"2": []}}
        assert auth.teaching_scope_label(teaching) == "Division 1 (Batch 1, 2)"
        rows = auth.teaching_rows({"Division 1": {"1": []}})
        assert rows == [{"division": "1", "batch": "1", "semester": ""}]
