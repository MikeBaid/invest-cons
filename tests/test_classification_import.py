from pathlib import Path

import pytest

from scripts.import_preview_classifications import prepare_assignments


def test_preview_import_deduplicates_same_instrument_across_accounts() -> None:
    preview = {"as_of": "2026-10-04T00:00:00+00:00", "positions": [
        {"assignment_status": "confirmed", "contour": "rf", "instrument_id": "UID",
         "candidate_bucket": "rf.bonds.rub_fixed", "reason": "fixed"},
        {"assignment_status": "confirmed", "contour": "rf", "instrument_id": "UID",
         "candidate_bucket": "rf.bonds.rub_fixed", "reason": "fixed"},
        {"assignment_status": "unclassified", "contour": "rf", "instrument_id": "OTHER",
         "candidate_bucket": None},
    ]}

    rows = prepare_assignments(preview, Path("config/ips/personal_main.draft.yaml"))
    assert len(rows) == 1
    assert rows[0]["bucket_id"] == "rf.bonds.rub_fixed"
    assert rows[0]["classification_version"] == "draft-1"


def test_preview_import_rejects_conflicting_confirmed_assignments() -> None:
    preview = {"positions": [
        {"assignment_status": "confirmed", "contour": "rf", "instrument_id": "UID",
         "candidate_bucket": "rf.bonds.rub_fixed"},
        {"assignment_status": "confirmed", "contour": "rf", "instrument_id": "UID",
         "candidate_bucket": "rf.bonds.rub_floater"},
    ]}
    with pytest.raises(ValueError, match="Conflicting buckets"):
        prepare_assignments(preview, Path("config/ips/personal_main.draft.yaml"))


def test_manual_override_wins_over_preview_assignment() -> None:
    preview = {"positions": [
        {"assignment_status": "confirmed", "contour": "rf",
         "instrument_id": "fd6da192-a299-43dc-a6c6-ba99be6079d2",
         "candidate_bucket": "rf.bonds.rub_floater"},
    ]}
    rows = prepare_assignments(
        preview,
        Path("config/ips/personal_main.draft.yaml"),
        Path("config/ips/instrument_classifications.yaml"),
    )

    by_id = {row["instrument_id"]: row for row in rows}
    assert len(rows) == 2
    assert by_id["fd6da192-a299-43dc-a6c6-ba99be6079d2"]["bucket_id"] == "rf.bonds.rub_fixed"
    assert by_id["fd6da192-a299-43dc-a6c6-ba99be6079d2"]["source"] == "user_confirmed_override"
    assert by_id["FPSTB5.KZ"]["bucket_id"] == "external.bonds.kz_fx"
