"""
Tests for scripts/enroll_vehicle_from_alert.py — Phase 6B.166 §11.87.7.

Verifies the rewrite of the vehicle-enrollment CLI:
  - Module header follows refactor-module-header standard
  - argparse wiring exposes all expected flags (--alert-id, --label,
    --color, --type, --make, --model, --plate, --note,
    --distinctive-features, --date, --dry-run, --alerts-dir,
    --known-file)
  - Paths come from infra.paths by default (no hardcoded
    /Users/jill/ai_camera_monitor references)
  - Writes go through KnownVehicleStore so the on-disk schema is the
    dict-wrapped `{"version": 1, "vehicles": [...]}` form that the
    canonical store (Phase 6B.83) requires. The previous version wrote
    a top-level list which silently corrupted the file.
  - Alert-derived fields (color/type/make/model/distinctive_features)
    flow through to the entry
  - Override flags win over alert-derived fields
  - Duplicate label/id detection works
  - Missing-file → empty store, not an error
  - --dry-run does not write
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "enroll_vehicle_from_alert.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_alert(alert_id: str = "TEST-ALERT-001", **attrs) -> dict:
    """Build a synthetic unknown-vehicle alert with sensible defaults."""
    base_attrs = {
        "color": "white",
        "type": "pickup",
        "make": "Chevrolet",
        "model": "Silverado 1500",
        "distinctive_features": ["outside-bed wheel flares"],
    }
    base_attrs.update(attrs)
    return {
        "alert_id": alert_id,
        "camera": "OFS",
        "frame_path": "/tmp/synthetic_frame.jpg",
        "vehicle_attributes": base_attrs,
    }


def _make_alerts_dir(tmp_path: Path, alert: dict, date: str = "2026-08-30") -> Path:
    """Write one alert to <tmp_path>/alerts/<date>.jsonl and return alerts_dir."""
    alerts = tmp_path / "alerts"
    alerts.mkdir(exist_ok=True)
    (alerts / f"{date}.jsonl").write_text(json.dumps(alert) + "\n")
    return alerts


# ===========================================================================
# Module header
# ===========================================================================


class TestEnrollVehicleModuleHeader:
    """Per refactor-module-header skill — every script in scripts/ must
    have the full standard header."""

    def _src(self):
        import infra.tests.test_enroll_vehicle_argparse as t
        return t.SCRIPT.read_text()

    def test_source_has_status_block(self):
        src = self._src()
        assert "STATUS:" in src
        assert "provisional" in src  # matches the actual status

    def test_source_has_thread_safety_block(self):
        src = self._src()
        assert "THREAD SAFETY:" in src

    def test_source_has_inputs_block(self):
        src = self._src()
        assert "INPUTS:" in src

    def test_source_has_outputs_block(self):
        src = self._src()
        assert "OUTPUTS:" in src

    def test_source_has_public_api_block(self):
        src = self._src()
        assert "PUBLIC API:" in src
        # Verify the documented functions are present
        assert "build_entry" in src
        assert "enroll(" in src
        assert "find_alert" in src
        assert "main(" in src

    def test_source_has_does_not_do_block(self):
        src = self._src()
        assert "DOES NOT DO:" in src

    def test_source_has_called_by_and_calls_into(self):
        src = self._src()
        assert "CALLED BY:" in src
        assert "CALLS INTO:" in src

    def test_no_hardcoded_production_repo_paths(self):
        """Per AGENTS.md Step 3 isolation rule — scripts/ may not reference
        /Users/jill/ai_camera_monitor/ (only the refactor path is allowed)."""
        src = self._src()
        for line in src.splitlines():
            # Allow comments/docstrings that *mention* the old repo for context
            if "/Users/jill/ai_camera_monitor" in line and "refactor" not in line:
                # The only acceptable mention is in the WHY HERE docstring
                # describing the rewrite history.
                if line.lstrip().startswith("#"):
                    continue
                pytest.fail(f"Hardcoded production-repo path in line: {line!r}")


# ===========================================================================
# argparse wiring
# ===========================================================================


class TestEnrollVehicleArgparse:
    """Verify the CLI exposes all flags from the documented surface."""

    def test_help_exits_and_lists_required_flags(self, capsys):
        from scripts import enroll_vehicle_from_alert as eva
        with pytest.raises(SystemExit), mock.patch.object(
            sys, "argv", [str(SCRIPT), "--help"]
        ):
            eva.main()
        out = capsys.readouterr().out
        # Required flags
        assert "--alert-id" in out
        assert "--label" in out
        # Override flags
        assert "--color" in out
        assert "--type" in out
        assert "--make" in out
        assert "--model" in out
        assert "--plate" in out
        assert "--note" in out
        assert "--distinctive-features" in out
        # Scope / mode flags
        assert "--date" in out
        assert "--dry-run" in out
        # Path overrides
        assert "--alerts-dir" in out
        assert "--known-file" in out

    def test_alert_id_is_required(self):
        from scripts import enroll_vehicle_from_alert as eva
        parser = eva._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_label_is_required(self):
        from scripts import enroll_vehicle_from_alert as eva
        parser = eva._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--alert-id", "x"])

    def test_overrides_collected_from_all_flags(self):
        from scripts import enroll_vehicle_from_alert as eva
        parser = eva._build_parser()
        args = parser.parse_args([
            "--alert-id", "X",
            "--label", "L",
            "--color", "red",
            "--type", "sedan",
            "--make", "Toyota",
            "--model", "Camry",
            "--plate", "ABC-123",
            "--note", "weekly",
            "--distinctive-features", "sunroof",
            "--date", "2026-08-30",
            "--known-file", "/tmp/foo.json",
            "--alerts-dir", "/tmp/alerts",
        ])
        assert args.color == "red"
        assert args.type == "sedan"
        assert args.make == "Toyota"
        assert args.model == "Camry"
        assert args.plate == "ABC-123"
        assert args.note == "weekly"
        assert args.distinctive_features == "sunroof"
        assert args.date == "2026-08-30"
        assert args.known_file == "/tmp/foo.json"
        assert args.alerts_dir == "/tmp/alerts"

    def test_dry_run_is_store_true(self):
        from scripts import enroll_vehicle_from_alert as eva
        parser = eva._build_parser()
        args = parser.parse_args(["--alert-id", "X", "--label", "L"])
        assert args.dry_run is False
        args = parser.parse_args(["--alert-id", "X", "--label", "L", "--dry-run"])
        assert args.dry_run is True


# ===========================================================================
# find_alert — alert lookup
# ===========================================================================


class TestFindAlert:
    """find_alert() scans one or all date JSONLs for a matching alert."""

    def test_finds_alert_by_id(self, tmp_path):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert("FIND-001")
        alerts_dir = _make_alerts_dir(tmp_path, alert)
        found = eva.find_alert("FIND-001", alerts_dir=str(alerts_dir))
        assert found is not None
        assert found["alert_id"] == "FIND-001"

    def test_returns_none_for_missing_id(self, tmp_path):
        from scripts import enroll_vehicle_from_alert as eva
        alerts_dir = _make_alerts_dir(tmp_path, _sample_alert("OTHER"))
        assert eva.find_alert("MISSING", alerts_dir=str(alerts_dir)) is None

    def test_returns_none_for_missing_alerts_dir(self, tmp_path):
        from scripts import enroll_vehicle_from_alert as eva
        assert eva.find_alert("ANY", alerts_dir=str(tmp_path / "nonexistent")) is None

    def test_date_filter_limits_scan(self, tmp_path):
        from scripts import enroll_vehicle_from_alert as eva
        # Two different dates
        _make_alerts_dir(tmp_path, _sample_alert("DATE-A"), date="2026-08-29")
        _make_alerts_dir(tmp_path, _sample_alert("DATE-B"), date="2026-08-30")
        alerts_dir = tmp_path / "alerts"
        # Without date filter → finds the newer one first
        found = eva.find_alert("DATE-A", alerts_dir=str(alerts_dir))
        assert found is not None
        # With wrong date → None
        assert eva.find_alert("DATE-A", alerts_dir=str(alerts_dir),
                              date_str="2026-08-30") is None

    def test_skips_alerts_without_vehicle_attributes(self, tmp_path):
        from scripts import enroll_vehicle_from_alert as eva
        # Two alerts with same id, only second has vehicle_attributes
        alerts = tmp_path / "alerts"
        alerts.mkdir(exist_ok=True)
        path = alerts / "2026-08-30.jsonl"
        path.write_text(json.dumps({"alert_id": "X", "motion": True}) + "\n")
        assert eva.find_alert("X", alerts_dir=str(alerts)) is None


# ===========================================================================
# build_entry — pure, no I/O
# ===========================================================================


class TestBuildEntry:
    """build_entry() is the pure transform. No file I/O."""

    def test_derives_id_from_label(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert()
        entry = eva.build_entry(alert, "Jeremiah's Chevy pickup", {})
        # Apostrophe stripped via [^a-z0-9]+ collapse, then adjacent _s_
        assert entry["id"] == "v_jeremiah_s_chevy_pickup"

    def test_id_normalization(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert()
        cases = [
            # Apostrophe eaten by [^a-z0-9]+ collapse, s sticks to _
            ("operator's blue Tesla", "v_operator_s_blue_tesla"),
            # Trailing ! collapses and gets stripped
            ("OFS test truck!", "v_ofs_test_truck"),
            ("a", "v_a"),
        ]
        for label, expected_id in cases:
            entry = eva.build_entry(alert, label, {})
            assert entry["id"] == expected_id, f"{label!r} → {entry['id']!r}"

    def test_empty_label_yields_v_unknown(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert()
        entry = eva.build_entry(alert, "!@#$", {})
        assert entry["id"] == "v_unknown"

    def test_alert_attributes_flow_through(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert()
        entry = eva.build_entry(alert, "L", {})
        assert entry["color"] == "white"
        assert entry["type"] == "pickup"
        assert entry["make"] == "Chevrolet"
        assert entry["model"] == "Silverado 1500"
        assert entry["distinctive_features"] == ["outside-bed wheel flares"]

    def test_overrides_win_over_alert(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert()  # white/pickup
        entry = eva.build_entry(alert, "L", {"color": "black", "type": "sedan"})
        assert entry["color"] == "black"
        assert entry["type"] == "sedan"

    def test_empty_override_treated_as_no_override(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert()  # color=white
        entry = eva.build_entry(alert, "L", {"color": ""})
        assert entry["color"] == "white"

    def test_none_override_treated_as_no_override(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert()
        entry = eva.build_entry(alert, "L", {"make": None})
        assert entry["make"] == "Chevrolet"

    def test_source_alert_id_and_frame_captured(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert("ALERT-XYZ")
        alert["frame_path"] = "/path/to/frame.jpg"
        entry = eva.build_entry(alert, "L", {})
        assert entry["source_alert_id"] == "ALERT-XYZ"
        assert entry["source_frame"] == "/path/to/frame.jpg"

    def test_enrolled_at_is_iso8601_utc(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert()
        entry = eva.build_entry(alert, "L", {})
        # Must be parseable as ISO-8601; the +00:00 suffix means UTC-aware
        from datetime import datetime
        parsed = datetime.fromisoformat(entry["enrolled_at"])
        assert parsed.tzinfo is not None

    def test_verified_defaults_to_false(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert()
        entry = eva.build_entry(alert, "L", {})
        assert entry["verified"] is False

    def test_missing_color_raises_value_error(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert(color=None)
        with pytest.raises(ValueError, match="missing required vehicle fields"):
            eva.build_entry(alert, "L", {})

    def test_missing_type_raises_value_error(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert(type=None)
        with pytest.raises(ValueError, match="missing required vehicle fields"):
            eva.build_entry(alert, "L", {})

    def test_empty_alert_attributes_raises_lookup_error(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = {"alert_id": "X", "vehicle_attributes": None}
        with pytest.raises(LookupError, match="not an unknown-vehicle alert"):
            eva.build_entry(alert, "L", {})

    def test_no_vehicle_attributes_raises_lookup_error(self):
        from scripts import enroll_vehicle_from_alert as eva
        alert = {"alert_id": "X"}
        with pytest.raises(LookupError, match="not an unknown-vehicle alert"):
            eva.build_entry(alert, "L", {})


# ===========================================================================
# enroll — full I/O through KnownVehicleStore
# ===========================================================================


class TestEnrollIO:
    """enroll() writes via KnownVehicleStore. This is the critical-path
    test that catches the schema mismatch the rewrite was meant to fix."""

    def test_writes_dict_wrapped_canonical_schema(self, tmp_path):
        from scripts import enroll_vehicle_from_alert as eva
        known_file = tmp_path / "vehicles" / "known_vehicles.json"
        entry = eva.enroll(_sample_alert(), "Test Truck", {}, known_file=str(known_file))
        # Read raw JSON — must be the dict-wrapped schema, not a top-level list
        raw = json.loads(known_file.read_text())
        assert isinstance(raw, dict), "schema must be dict-wrapped, not list"
        assert raw["version"] == 1
        assert isinstance(raw["vehicles"], list)
        assert len(raw["vehicles"]) == 1
        # Sanity: the return value matches the persisted entry.
        assert raw["vehicles"][0]["label"] == entry["label"]
        assert raw["vehicles"][0]["source_alert_id"] == entry["source_alert_id"]

    def test_written_file_is_loadable_by_canonical_store(self, tmp_path):
        """If known_vehicles/store.py can read it back, the file is valid
        per the canonical schema. This is the regression pin for the
        bug §11.87.7 fixed."""
        from known_vehicles.store import KnownVehicleStore
        from scripts import enroll_vehicle_from_alert as eva
        known_file = tmp_path / "vehicles" / "known_vehicles.json"
        eva.enroll(_sample_alert(), "Test Truck", {}, known_file=str(known_file))
        store = KnownVehicleStore.from_file(known_file)
        assert len(store) == 1
        assert store.get_by_id("v_test_truck") is not None

    def test_enroll_returns_appended_entry(self, tmp_path):
        from scripts import enroll_vehicle_from_alert as eva
        known_file = tmp_path / "vehicles" / "known_vehicles.json"
        entry = eva.enroll(_sample_alert(), "Test Truck", {}, known_file=str(known_file))
        assert entry["id"] == "v_test_truck"
        assert entry["label"] == "Test Truck"
        assert entry["color"] == "white"

    def test_missing_known_file_creates_empty_store(self, tmp_path):
        """First-time enrollment in a fresh project shouldn't fail — the
        script starts from an empty store if the file doesn't exist."""
        from scripts import enroll_vehicle_from_alert as eva
        known_file = tmp_path / "vehicles" / "known_vehicles.json"
        assert not known_file.exists()
        eva.enroll(_sample_alert(), "Test Truck", {}, known_file=str(known_file))
        assert known_file.exists()
        raw = json.loads(known_file.read_text())
        assert raw["version"] == 1
        assert len(raw["vehicles"]) == 1

    def test_parent_directory_created_if_missing(self, tmp_path):
        """KnownVehicleStore.to_file does not mkdir. enroll() must."""
        from scripts import enroll_vehicle_from_alert as eva
        known_file = tmp_path / "deep" / "nested" / "vehicles" / "known.json"
        assert not known_file.parent.exists()
        eva.enroll(_sample_alert(), "Test", {}, known_file=str(known_file))
        assert known_file.exists()

    def test_duplicate_label_raises_value_error(self, tmp_path):
        from scripts import enroll_vehicle_from_alert as eva
        known_file = tmp_path / "vehicles" / "known_vehicles.json"
        eva.enroll(_sample_alert(), "Test Truck", {}, known_file=str(known_file))
        with pytest.raises(ValueError, match="duplicate entry"):
            eva.enroll(_sample_alert("OTHER"), "Test Truck", {},
                       known_file=str(known_file))

    def test_duplicate_id_raises_value_error(self, tmp_path):
        """Two labels that normalize to the same id should collide."""
        from scripts import enroll_vehicle_from_alert as eva
        known_file = tmp_path / "vehicles" / "known_vehicles.json"
        eva.enroll(_sample_alert(), "Test Truck", {}, known_file=str(known_file))
        with pytest.raises(ValueError, match="duplicate entry"):
            eva.enroll(_sample_alert("OTHER"), "Test Truck!!", {},
                       known_file=str(known_file))

    def test_dry_run_does_not_write(self, tmp_path):
        from scripts import enroll_vehicle_from_alert as eva
        known_file = tmp_path / "vehicles" / "known_vehicles.json"
        entry = eva.enroll(_sample_alert(), "Dry Run Test", {},
                           dry_run=True, known_file=str(known_file))
        assert not known_file.exists()
        # The entry is still returned so the caller can see what would have been written
        assert entry["id"] == "v_dry_run_test"
        assert entry["label"] == "Dry Run Test"

    def test_overrides_win_in_live_write(self, tmp_path):
        from known_vehicles.store import KnownVehicleStore
        from scripts import enroll_vehicle_from_alert as eva
        known_file = tmp_path / "vehicles" / "known_vehicles.json"
        eva.enroll(_sample_alert(), "Test Truck",
                   {"color": "black", "type": "sedan", "make": "Toyota"},
                   known_file=str(known_file))
        store = KnownVehicleStore.from_file(known_file)
        v = store.get_by_id("v_test_truck")
        assert v["color"] == "black"
        assert v["type"] == "sedan"
        assert v["make"] == "Toyota"

    def test_appending_preserves_existing_entries(self, tmp_path):
        """Adding a second vehicle must not clobber the first."""
        from known_vehicles.store import KnownVehicleStore
        from scripts import enroll_vehicle_from_alert as eva
        known_file = tmp_path / "vehicles" / "known_vehicles.json"
        eva.enroll(_sample_alert("A-001"), "First Truck", {},
                   known_file=str(known_file))
        eva.enroll(_sample_alert("A-002"), "Second Truck", {},
                   known_file=str(known_file))
        store = KnownVehicleStore.from_file(known_file)
        assert len(store) == 2
        assert store.get_by_id("v_first_truck") is not None
        assert store.get_by_id("v_second_truck") is not None


# ===========================================================================
# main() — exit codes + CLI end-to-end
# ===========================================================================


class TestMainExitCodes:
    """main() returns the documented exit codes:
      0 success (including dry-run)
      2 alert not found / not an unknown-vehicle alert
      3 label collision / validation failure
    """

    def test_missing_alert_id_exits_2(self, tmp_path, capsys):
        from scripts import enroll_vehicle_from_alert as eva
        # alerts dir exists but no matching alert
        _make_alerts_dir(tmp_path, _sample_alert("OTHER"))
        with mock.patch.object(sys, "argv",
                               [str(SCRIPT),
                                "--alert-id", "MISSING",
                                "--label", "L",
                                "--alerts-dir", str(tmp_path / "alerts")]):
            assert eva.main() == 2

    def test_missing_alerts_dir_exits_2(self, tmp_path):
        from scripts import enroll_vehicle_from_alert as eva
        with mock.patch.object(sys, "argv",
                               [str(SCRIPT),
                                "--alert-id", "X",
                                "--label", "L",
                                "--alerts-dir", str(tmp_path / "no")]):
            assert eva.main() == 2

    def test_alert_without_vehicle_attributes_exits_2(self, tmp_path):
        from scripts import enroll_vehicle_from_alert as eva
        alerts_dir = _make_alerts_dir(tmp_path, _sample_alert(""))
        # Write a separate alert with same id but no vehicle_attributes
        with (tmp_path / "alerts" / "2026-08-30.jsonl").open("a") as f:
            f.write(json.dumps({"alert_id": "NO-VA", "motion": True}) + "\n")
        # The default find_alert behavior: returns first match even
        # without vehicle_attributes? No — per the docstring, alerts
        # without vehicle_attributes are SKIPPED. So a lookup where
        # only NO-VA matches would return None. Verify:
        with mock.patch.object(sys, "argv",
                               [str(SCRIPT),
                                "--alert-id", "NO-VA",
                                "--label", "L",
                                "--alerts-dir", str(alerts_dir)]):
            assert eva.main() == 2

    def test_duplicate_label_exits_3(self, tmp_path):
        from scripts import enroll_vehicle_from_alert as eva
        known_file = tmp_path / "vehicles" / "known_vehicles.json"
        eva.enroll(_sample_alert("A-001"), "Test Truck", {},
                   known_file=str(known_file))
        alerts_dir = _make_alerts_dir(tmp_path, _sample_alert("A-002"))
        with mock.patch.object(sys, "argv",
                               [str(SCRIPT),
                                "--alert-id", "A-002",
                                "--label", "Test Truck",
                                "--alerts-dir", str(alerts_dir),
                                "--known-file", str(known_file)]):
            assert eva.main() == 3

    def test_missing_required_fields_exits_3(self, tmp_path):
        from scripts import enroll_vehicle_from_alert as eva
        alert = _sample_alert()
        alert["vehicle_attributes"] = {"make": "Ford"}  # no color/type
        alerts_dir = _make_alerts_dir(tmp_path, alert)
        with mock.patch.object(sys, "argv",
                               [str(SCRIPT),
                                "--alert-id", "TEST-ALERT-001",
                                "--label", "Test",
                                "--alerts-dir", str(alerts_dir),
                                "--known-file", str(tmp_path / "vehicles" / "known.json")]):
            assert eva.main() == 3

    def test_successful_enroll_exits_0(self, tmp_path, capsys):
        from scripts import enroll_vehicle_from_alert as eva
        alerts_dir = _make_alerts_dir(tmp_path, _sample_alert("OK-001"))
        known_file = tmp_path / "vehicles" / "known_vehicles.json"
        with mock.patch.object(sys, "argv",
                               [str(SCRIPT),
                                "--alert-id", "OK-001",
                                "--label", "Test Truck",
                                "--alerts-dir", str(alerts_dir),
                                "--known-file", str(known_file)]):
            assert eva.main() == 0
        # Stdout should have the JSON entry
        out = capsys.readouterr().out
        assert '"id": "v_test_truck"' in out
        # File written
        assert known_file.exists()

    def test_dry_run_exits_0_without_writing(self, tmp_path, capsys):
        from scripts import enroll_vehicle_from_alert as eva
        alerts_dir = _make_alerts_dir(tmp_path, _sample_alert("DR-001"))
        known_file = tmp_path / "vehicles" / "known_vehicles.json"
        with mock.patch.object(sys, "argv",
                               [str(SCRIPT),
                                "--alert-id", "DR-001",
                                "--label", "Dry Run Test",
                                "--dry-run",
                                "--alerts-dir", str(alerts_dir),
                                "--known-file", str(known_file)]):
            assert eva.main() == 0
        assert not known_file.exists()


# ===========================================================================
# Path parameterization
# ===========================================================================


class TestPathParameterization:
    """The script must NOT have hardcoded paths. Defaults must come from
    infra.paths, and CLI overrides must take precedence."""

    def test_default_alerts_dir_documents_infra_paths(self):
        """--alerts-dir default is None; the module falls back to
        infra.paths.ALERTS_DIR at call time. The parser help-text must
        advertise the infra.paths value so operators can see it."""
        import scripts.enroll_vehicle_from_alert as eva_mod

        # The argparse default is None (resolved inside find_alert),
        # but the help string surfaces the infra.paths value so operators
        # see where alerts come from by default. argparse wraps long
        # paths across lines, so we check the trailing parts.
        from infra.paths import ALERTS_DIR, VEHICLE_KNOWN_FILE
        from scripts import enroll_vehicle_from_alert as eva
        help_text = eva._build_parser().format_help()
        assert "data/alerts" in help_text
        assert "known_vehicles.json" in help_text
        # And the module imports both constants — verify it exposes them
        assert eva_mod.ALERTS_DIR == ALERTS_DIR
        assert eva_mod.VEHICLE_KNOWN_FILE == VEHICLE_KNOWN_FILE

    def test_default_known_file_comes_from_infra_paths(self):
        """Module imports VEHICLE_KNOWN_FILE from infra.paths."""
        import scripts.enroll_vehicle_from_alert as eva_mod
        from infra.paths import VEHICLE_KNOWN_FILE as paths_kv
        assert eva_mod.VEHICLE_KNOWN_FILE == paths_kv

    def test_alerts_dir_override_takes_precedence(self, tmp_path):
        """--alerts-dir should bypass infra.paths.ALERTS_DIR default."""
        from scripts import enroll_vehicle_from_alert as eva
        # Set up alert in tmp dir, NOT in default location
        custom = tmp_path / "custom_alerts"
        custom.mkdir()
        alert = _sample_alert("CUSTOM-001")
        (custom / "2026-08-30.jsonl").write_text(json.dumps(alert) + "\n")
        known_file = tmp_path / "known.json"
        with mock.patch.object(sys, "argv",
                               [str(SCRIPT),
                                "--alert-id", "CUSTOM-001",
                                "--label", "L",
                                "--alerts-dir", str(custom),
                                "--known-file", str(known_file)]):
            assert eva.main() == 0
        assert known_file.exists()

    def test_known_file_override_takes_precedence(self, tmp_path):
        """--known-file should write to the specified path, NOT infra.paths."""
        from infra.paths import VEHICLE_KNOWN_FILE
        from scripts import enroll_vehicle_from_alert as eva
        prod_path = Path(VEHICLE_KNOWN_FILE)
        # Public-repo skip: if the production file doesn't exist or is empty,
        # the test has no baseline to compare against.
        if not prod_path.exists() or prod_path.stat().st_size == 0:
            pytest.skip(
                "Public repo: requires a populated known_vehicles.json at "
                + str(prod_path)
                + " (copy data/vehicles/known_vehicles.example.json first)"
            )
        alerts_dir = _make_alerts_dir(tmp_path, _sample_alert("K-001"))
        custom_kv = tmp_path / "custom_kv.json"
        with mock.patch.object(sys, "argv",
                               [str(SCRIPT),
                                "--alert-id", "K-001",
                                "--label", "L",
                                "--alerts-dir", str(alerts_dir),
                                "--known-file", str(custom_kv)]):
            assert eva.main() == 0
        assert custom_kv.exists()
        # Production file at infra.paths.VEHICLE_KNOWN_FILE should NOT have this entry
        prod = json.loads(prod_path.read_text())
        custom = json.loads(custom_kv.read_text())
        prod_ids = {v["id"] for v in prod["vehicles"]}
        custom_ids = {v["id"] for v in custom["vehicles"]}
        assert custom_ids - prod_ids  # at least one new id in custom
