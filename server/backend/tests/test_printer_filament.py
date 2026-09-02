"""Filament accounting: an estimate, counted once, never invented.

Two facts about the source shape every assertion here, and both were read off
the deployed history rather than inferred from field names:

* ``weight_grams`` is the slicer's plan for the job. A stored job that aborted
  after two seconds still carries 154.5 g, so treating it as consumption would
  overstate the total badly.
* ``amsDetailMapping`` carries a per-slot ``filamentType`` and ``weight`` whose
  sum equals ``weight_grams`` exactly across every stored record, so a
  multi-material job needs no guessing and no even division.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.printer_intelligence import PrinterIntelligenceStore
from app.printer_persistence import PrinterStore

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _store(tmp_path: Path) -> PrinterIntelligenceStore:
    database = tmp_path / "printer.sqlite3"
    local = PrinterStore(database)
    local.initialize()
    intelligence = PrinterIntelligenceStore(database)
    intelligence.initialize()
    return intelligence


def _cloud(
    cloud_id: str,
    *,
    status: int = 2,
    weight: float | None = 100.0,
    ams: list[dict] | None = None,
    hours: float = 1.0,
    start: datetime = NOW,
    length: float | None = 3.0,
):
    record = {
        "id": cloud_id,
        "title": f"job-{cloud_id}",
        "status": status,
        "startTime": _iso(start),
        "endTime": _iso(start + timedelta(hours=hours)),
        "costTime": int(hours * 3600),
        "amsDetailMapping": ams if ams is not None else [],
        "length": length,
    }
    if weight is not None:
        record["weight"] = weight
    return record


def _slot(material: str, weight: float, *, slot: int = 0):
    return {
        "ams": 0,
        "amsId": 0,
        "slotId": slot,
        "nozzleId": 0,
        "filamentType": material,
        "weight": weight,
        "sourceColor": "FFFFFFFF",
        "targetColor": "FFFFFFFF",
        "targetFilamentType": "",
    }


def _local_session(
    intelligence: PrinterIntelligenceStore,
    session_id: str,
    *,
    result: str = "completed",
    start: datetime = NOW,
) -> None:
    connection = sqlite3.connect(intelligence.database_path)
    with connection:
        connection.execute(
            """INSERT INTO print_sessions (
                   session_id, printer_id, job_id, job_name, started_at_utc,
                   start_provenance, ended_at_utc, end_provenance, result,
                   material, material_provenance, active_tool, ams_slot, source,
                   updated_at_utc
               ) VALUES (?, 'x2d', NULL, ?, ?, 'observed', ?, 'observed', ?,
                         NULL, 'observed', NULL, NULL, 'locally_observed', ?)""",
            (
                session_id,
                f"job-{session_id}",
                _iso(start),
                _iso(start + timedelta(hours=1)),
                result,
                _iso(NOW),
            ),
        )
    connection.close()


def _filament(intelligence: PrinterIntelligenceStore) -> dict:
    return intelligence.usage_summary("x2d", now=NOW)


def _by_material(summary: dict) -> dict[str, float]:
    return {
        item["material"]: item["grams"]
        for item in summary["tracked_filament_by_material"]
    }


# --- single material ------------------------------------------------------


def test_a_single_material_job_is_counted_once(tmp_path: Path) -> None:
    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d",
        [_cloud("a", weight=250.0, ams=[_slot("PETG", 250.0)])],
        imported_at=NOW,
    )

    summary = _filament(intelligence)

    assert summary["tracked_filament_estimate_g"] == 250.0
    assert summary["tracked_filament_estimate_kg"] == 0.25
    assert summary["tracked_filament_job_count"] == 1
    assert _by_material(summary) == {"PETG": 250.0}


def test_the_total_is_never_presented_as_a_measurement(tmp_path: Path) -> None:
    """The number is Bambu's slicer plan; the API must say so."""

    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d", [_cloud("a", weight=100.0, ams=[_slot("PLA", 100.0)])], imported_at=NOW
    )

    summary = _filament(intelligence)

    assert summary["tracked_filament_measured"] is False
    assert "estimate" in summary["tracked_filament_semantics"]
    assert summary["tracked_filament_history_complete"] is False
    assert (
        "filament_amount_is_a_slicer_estimate_not_a_measurement"
        in summary["tracked_filament_history_completeness_reasons"]
    )


# --- multi-material -------------------------------------------------------


def test_multi_material_uses_the_per_slot_masses(tmp_path: Path) -> None:
    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d",
        [
            _cloud(
                "a",
                weight=95.73,
                ams=[_slot("PETG", 92.57), _slot("PLA", 3.16, slot=1)],
            )
        ],
        imported_at=NOW,
    )

    summary = _filament(intelligence)

    assert _by_material(summary) == {"PETG": 92.57, "PLA": 3.16}
    assert summary["tracked_filament_unallocated_g"] == 0.0


def test_repeated_slots_of_one_material_are_summed(tmp_path: Path) -> None:
    """A job can map the same filament into two trays."""

    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d",
        [
            _cloud(
                "a",
                weight=21.67,
                ams=[
                    _slot("PETG", 0.45),
                    _slot("PLA", 15.51, slot=1),
                    _slot("PLA", 5.71, slot=2),
                ],
            )
        ],
        imported_at=NOW,
    )

    summary = _filament(intelligence)

    assert _by_material(summary) == {"PLA": 21.22, "PETG": 0.45}
    # Counted once per job per material, not once per tray.
    counts = {
        item["material"]: item["job_count"]
        for item in summary["tracked_filament_by_material"]
    }
    assert counts == {"PLA": 1, "PETG": 1}


def test_a_mapped_but_unused_slot_contributes_nothing(tmp_path: Path) -> None:
    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d",
        [_cloud("a", weight=42.54, ams=[_slot("PETG", 42.54), _slot("PLA", 0.0, slot=1)])],
        imported_at=NOW,
    )

    assert _by_material(_filament(intelligence)) == {"PETG": 42.54}


def test_a_total_without_an_allocation_is_unallocated_not_divided(
    tmp_path: Path,
) -> None:
    """Two materials and one number is not two numbers.

    Splitting the total evenly would invent per-material figures the source
    never provided, so the mass is reported as known-but-unallocated.
    """

    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d", [_cloud("a", weight=300.0, ams=[])], imported_at=NOW
    )

    summary = _filament(intelligence)

    assert summary["tracked_filament_estimate_g"] == 300.0
    assert summary["tracked_filament_unallocated_g"] == 300.0
    assert summary["tracked_filament_by_material"] == []


# --- material naming ------------------------------------------------------


def test_specialty_materials_stay_distinguishable(tmp_path: Path) -> None:
    """PETG-ESD is not PETG, and PLA-CF is not PLA."""

    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d",
        [
            _cloud("a", weight=10.0, ams=[_slot("PETG", 10.0)]),
            _cloud("b", weight=20.0, ams=[_slot("PETG-ESD", 20.0)], start=NOW - timedelta(days=1)),
            _cloud("c", weight=30.0, ams=[_slot("PLA-CF", 30.0)], start=NOW - timedelta(days=2)),
            _cloud("d", weight=40.0, ams=[_slot("PA6-GF", 40.0)], start=NOW - timedelta(days=3)),
        ],
        imported_at=NOW,
    )

    summary = _filament(intelligence)
    materials = _by_material(summary)

    assert materials == {"PA6-GF": 40.0, "PLA-CF": 30.0, "PETG-ESD": 20.0, "PETG": 10.0}
    families = {
        item["material"]: (item["family"], item["variant"])
        for item in summary["tracked_filament_by_material"]
    }
    assert families["PETG-ESD"] == ("PETG", "ESD")
    assert families["PA6-GF"] == ("PA6", "GF")
    assert families["PETG"] == ("PETG", "")


def test_the_source_name_is_retained_beside_the_normalized_one(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d", [_cloud("a", weight=10.0, ams=[_slot("petg", 10.0)])], imported_at=NOW
    )

    entry = _filament(intelligence)["tracked_filament_by_material"][0]

    assert entry["material"] == "PETG"
    assert entry["raw_names"] == ["petg"]


def test_a_missing_material_name_becomes_unknown_not_a_guess(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d", [_cloud("a", weight=10.0, ams=[_slot("", 10.0)])], imported_at=NOW
    )

    assert _by_material(_filament(intelligence)) == {"unknown": 10.0}


# --- results --------------------------------------------------------------


def test_an_aborted_job_does_not_contribute_its_planned_weight(
    tmp_path: Path,
) -> None:
    """The deployed history has a job that stopped after 2s carrying 154.5 g.

    That number is the plan for the whole print. Counting it would claim
    filament that was never extruded.
    """

    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d",
        [
            _cloud("done", weight=100.0, ams=[_slot("PLA", 100.0)]),
            _cloud(
                "aborted",
                status=-1,
                weight=154.5,
                ams=[_slot("PLA", 154.5)],
                hours=0.001,
                start=NOW - timedelta(days=1),
            ),
        ],
        imported_at=NOW,
    )

    summary = _filament(intelligence)

    assert summary["tracked_filament_estimate_g"] == 100.0
    assert summary["tracked_filament_job_count"] == 1
    # The print happened and used filament; only the amount is unknown.
    assert summary["tracked_filament_incomplete_job_count"] == 1
    assert _by_material(summary) == {"PLA": 100.0}


def test_a_completed_job_without_a_weight_is_counted_as_unknown_not_zero(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d",
        [
            _cloud("a", weight=100.0, ams=[_slot("PLA", 100.0)]),
            _cloud("b", weight=None, ams=[], start=NOW - timedelta(days=1)),
        ],
        imported_at=NOW,
    )

    summary = _filament(intelligence)

    assert summary["tracked_filament_estimate_g"] == 100.0
    assert summary["tracked_filament_unknown_amount_job_count"] == 1
    assert summary["tracked_filament_job_count"] == 1


# --- deduplication and monotonicity ---------------------------------------


def test_a_job_seen_locally_and_in_the_cloud_counts_once(tmp_path: Path) -> None:
    """The same physical print reconciled from both sources is one job."""

    intelligence = _store(tmp_path)
    _local_session(intelligence, "local-1")
    intelligence.import_cloud_records(
        "x2d", [_cloud("a", weight=250.0, ams=[_slot("PETG", 250.0)])], imported_at=NOW
    )
    connection = sqlite3.connect(intelligence.database_path)
    with connection:
        connection.execute(
            "UPDATE cloud_print_history SET reconciled_session_id='local-1'"
        )
    connection.close()

    summary = _filament(intelligence)

    assert summary["tracked_filament_estimate_g"] == 250.0
    assert summary["tracked_filament_job_count"] == 1


def test_re_importing_the_same_history_does_not_grow_the_total(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    records = [
        _cloud("a", weight=250.0, ams=[_slot("PETG", 250.0)]),
        _cloud("b", weight=125.0, ams=[_slot("PLA", 125.0)], start=NOW - timedelta(days=1)),
    ]
    intelligence.import_cloud_records("x2d", records, imported_at=NOW)
    first = _filament(intelligence)["tracked_filament_estimate_g"]

    for _ in range(3):
        intelligence.import_cloud_records("x2d", records, imported_at=NOW)

    assert _filament(intelligence)["tracked_filament_estimate_g"] == first == 375.0


def test_out_of_order_history_does_not_duplicate(tmp_path: Path) -> None:
    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d",
        [_cloud("b", weight=125.0, ams=[_slot("PLA", 125.0)], start=NOW - timedelta(days=5))],
        imported_at=NOW,
    )
    intelligence.import_cloud_records(
        "x2d",
        [_cloud("a", weight=250.0, ams=[_slot("PETG", 250.0)], start=NOW - timedelta(days=9))],
        imported_at=NOW,
    )
    intelligence.import_cloud_records(
        "x2d",
        [_cloud("b", weight=125.0, ams=[_slot("PLA", 125.0)], start=NOW - timedelta(days=5))],
        imported_at=NOW,
    )

    summary = _filament(intelligence)

    assert summary["tracked_filament_estimate_g"] == 375.0
    assert summary["tracked_filament_job_count"] == 2


def test_the_total_only_grows_as_history_arrives(tmp_path: Path) -> None:
    intelligence = _store(tmp_path)
    totals = []
    for index in range(4):
        intelligence.import_cloud_records(
            "x2d",
            [
                _cloud(
                    f"job-{index}",
                    weight=100.0,
                    ams=[_slot("PLA", 100.0)],
                    start=NOW - timedelta(days=index),
                )
            ],
            imported_at=NOW,
        )
        totals.append(_filament(intelligence)["tracked_filament_estimate_g"])

    assert totals == sorted(totals)
    assert totals == [100.0, 200.0, 300.0, 400.0]


# --- coverage -------------------------------------------------------------


def test_coverage_is_reported_beside_the_total(tmp_path: Path) -> None:
    intelligence = _store(tmp_path)
    first = NOW - timedelta(days=30)
    intelligence.import_cloud_records(
        "x2d",
        [
            _cloud("old", weight=10.0, ams=[_slot("PLA", 10.0)], start=first),
            _cloud("new", weight=20.0, ams=[_slot("PLA", 20.0)]),
        ],
        imported_at=NOW,
    )

    summary = _filament(intelligence)

    assert summary["tracked_filament_first_job_at"] == _iso(first)
    assert summary["tracked_filament_history_complete"] is False
    assert (
        "locally_observed_only_jobs_carry_no_filament_amount"
        in summary["tracked_filament_history_completeness_reasons"]
    )


def test_a_locally_only_job_is_a_known_print_with_unknown_filament(
    tmp_path: Path,
) -> None:
    """The observer records material and slot but never a mass."""

    intelligence = _store(tmp_path)
    _local_session(intelligence, "local-1")

    summary = _filament(intelligence)

    assert summary["tracked_filament_estimate_g"] == 0.0
    assert summary["tracked_filament_unknown_amount_job_count"] == 1
    # The print itself is still counted in the runtime total.
    assert summary["tracked_job_count"] == 1


def test_filament_totals_sit_beside_the_runtime_totals(tmp_path: Path) -> None:
    """One summary, one coverage story, no parallel accounting model."""

    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d", [_cloud("a", weight=250.0, ams=[_slot("PETG", 250.0)])], imported_at=NOW
    )

    summary = _filament(intelligence)

    assert summary["tracked_print_hours"] > 0
    assert summary["tracked_filament_estimate_g"] == 250.0
    assert summary["printer_reported_lifetime_hours"] is None
