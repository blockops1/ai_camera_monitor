"""Tests for infra/event_record.py -- EventRecord dataclass + atomic write."""
from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from infra.event_record import EventRecord, write_event_record


class TestDataclass:
    """AC: EventRecord is a dataclass with all expected fields."""

    def test_dataclass_fields_exist(self):
        """All 10 fields are present on the dataclass."""
        rec = EventRecord(
            alert_id="test-1",
            camera_id="OFS",
            box_id="farm1",
            started_at="2026-09-22T12:00:00Z",
            finished_at="2026-09-22T12:00:01Z",
            gate={"classification": "person", "class_label": "person",
                  "confidence": 0.95, "reason": "yolo_pass"},
            vm1={"class": "person", "subclass": "adult", "confidence": 0.91},
            vm2={"class": "person", "make": None, "model": None,
                 "color": "black", "body_style_hint": None},
            match={"matched": True, "id": "person_1", "score": 5},
            dropped=None,
            frame_paths=["frame_001.png", "frame_002.png"],
            artifact_dir="/data/frames/OFS/test-1/composite.jpg",
        )
        assert rec.alert_id == "test-1"
        assert rec.camera_id == "OFS"
        assert rec.dropped is None
        assert len(rec.frame_paths) == 2

    def test_dataclass_fields_json_serializable(self):
        """All fields round-trip through dataclasses.asdict + json."""
        from dataclasses import asdict
        rec = EventRecord(
            alert_id="test-1",
            camera_id="OFS",
            box_id="farm1",
            started_at="2026-09-22T12:00:00Z",
            finished_at="2026-09-22T12:00:01Z",
            gate={"classification": "person", "class_label": "person",
                  "confidence": 0.95, "reason": "yolo_pass"},
            vm1={"class": "person", "subclass": "adult", "confidence": 0.91},
            vm2={"class": "person", "make": None, "model": None,
                 "color": "black", "body_style_hint": None},
            match={"matched": True, "id": "person_1", "score": 5},
            dropped=None,
            frame_paths=["frame_001.png", "frame_002.png"],
            artifact_dir="composite.jpg",
        )
        # This should not raise -- all values are JSON-serializable.
        d = asdict(rec)
        import json
        json.dumps(d)

    def test_asdict_serializes_all_fields(self):
        """asdict produces a flat dict with all 10 keys."""
        rec = EventRecord(
            alert_id="x", camera_id="c", box_id="b",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            gate={"a": 1}, vm1={"b": 2}, vm2={"c": 3}, match={"d": 4},
            dropped=None, frame_paths=[], artifact_dir="",
        )
        from dataclasses import asdict
        d = asdict(rec)
        assert set(d.keys()) == {
            "alert_id", "camera_id", "box_id",
            "started_at", "finished_at",
            "gate", "vm1", "vm2", "match",
            "dropped", "frame_paths", "artifact_dir",
        }

    def test_none_fields_serialize_to_null(self):
        """Dropped=None -> JSON null; frames can be empty list."""
        from dataclasses import asdict
        rec = EventRecord(
            alert_id="x", camera_id="c", box_id="b",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            gate=None, vm1=None, vm2=None, match=None,
            dropped="gate_dropped", frame_paths=[], artifact_dir="",
        )
        d = asdict(rec)
        assert d["dropped"] == "gate_dropped"
        assert d["gate"] is None
        assert d["vm1"] is None
        assert d["frame_paths"] == []


class TestWriteEventRecord:
    """AC: write_event_record writes a single event.json atomically."""

    def test_writes_event_json(self):
        """event.json is created with the serialized dataclass."""
        with TemporaryDirectory() as td:
            # The function creates <event_dir>/<alert_id>/event.json.
            alert_dir = Path(td) / "alert-1"

            write_event_record(
                alert_id="alert-1",
                camera_id="OFS",
                box_id="farm1",
                started_at="2026-09-22T12:00:00Z",
                finished_at="2026-09-22T12:00:01Z",
                gate={"classification": "person"},
                vm1=None, vm2=None, match=None,
                dropped=None,
                frame_paths=["frame_001.png"],
                artifact_dir="composite.jpg",
                event_dir=str(td),
            )

            json_path = alert_dir / "event.json"
            assert json_path.is_file()

            data = json.loads(json_path.read_text())
            assert data["alert_id"] == "alert-1"
            assert data["camera_id"] == "OFS"
            assert data["box_id"] == "farm1"
            assert data["gate"]["classification"] == "person"
            assert data["dropped"] is None

    def test_dropped_path_records_reason(self):
        """Dropped alert records the reason in the event."""
        with TemporaryDirectory() as td:
            alert_dir = Path(td) / "alert-drop"

            write_event_record(
                alert_id="alert-drop",
                camera_id="OFS",
                box_id="farm1",
                started_at="2026-09-22T12:00:00Z",
                finished_at="2026-09-22T12:00:00.5Z",
                gate={"classification": "car"},
                vm1={"class": "car", "subclass": "sedan"},
                vm2=None, match=None,
                dropped="cooldown_dropped",
                frame_paths=[],
                artifact_dir="",
                event_dir=str(td),
            )

            data = json.loads((alert_dir / "event.json").read_text())
            assert data["dropped"] == "cooldown_dropped"

    def test_atomic_write_tmp_then_rename(self):
        """The file lands as .tmp first, then renames -- no partial writes."""
        with TemporaryDirectory() as td:
            alert_dir = Path(td) / "alert-2"

            # Write a valid record.
            write_event_record(
                alert_id="alert-2",
                camera_id="OFS",
                box_id="farm1",
                started_at="2026-09-22T12:00:00Z",
                finished_at="2026-09-22T12:00:01Z",
                gate=None, vm1=None, vm2=None, match=None,
                dropped=None,
                frame_paths=[],
                artifact_dir="",
                event_dir=str(td),
            )

            # After write, no .tmp files should remain.
            tmpls = list(alert_dir.glob("*.tmp"))
            assert len(tmpls) == 0, "Leftover .tmp file after write"

            # event.json should be valid JSON.
            data = json.loads((alert_dir / "event.json").read_text())
            assert data["alert_id"] == "alert-2"

    def test_creates_directory_if_missing(self):
        """write_event_record creates the per-alert dir if it doesn't exist."""
        with TemporaryDirectory() as td:
            alert_dir = Path(td) / "nonexistent-alert"
            # Don't create alert_dir -- let write_event_record do it.
            write_event_record(
                alert_id="nonexistent-alert",
                camera_id="OFS",
                box_id="farm1",
                started_at="2026-09-22T12:00:00Z",
                finished_at="2026-09-22T12:00:01Z",
                gate=None, vm1=None, vm2=None, match=None,
                dropped=None,
                frame_paths=[],
                artifact_dir="",
                event_dir=str(td),
            )
            assert (alert_dir / "event.json").is_file()

    def test_json_is_valid_with_indent(self):
        """The JSON is valid and formatted with indent=2 for readability."""
        with TemporaryDirectory() as td:
            alert_dir = Path(td) / "alert-line"

            write_event_record(
                alert_id="alert-line",
                camera_id="OFS",
                box_id="farm1",
                started_at="2026-09-22T12:00:00Z",
                finished_at="2026-09-22T12:00:01Z",
                gate={"classification": "person"},
                vm1=None, vm2=None, match=None,
                dropped=None,
                frame_paths=["f1.png", "f2.png"],
                artifact_dir="comp.jpg",
                event_dir=str(td),
            )

            data = json.loads((alert_dir / "event.json").read_text())
            assert data["alert_id"] == "alert-line"
            assert len(data["frame_paths"]) == 2
            assert len((alert_dir / "event.json").read_text().splitlines()) > 1  # indented


class TestIntegrationWithDaemon:
    """Verify that daemon.py imports and references event_record correctly."""

    def test_daemon_imports_write_event_record(self):
        """daemon.py has 'from infra.event_record import write_event_record'."""
        from listener import daemon

        # The import should have placed write_event_record in the module namespace.
        assert hasattr(daemon, 'write_event_record')

    def test_box_id_helper_returns_non_empty(self):
        """_box_id returns a non-empty string."""
        from listener.daemon import _box_id
        box = _box_id()
        assert isinstance(box, str)
        assert len(box) > 0

    def test_box_id_prefers_env(self):
        """_box_id prefers FARMSURV_BOX_ID env var over hostname."""
        from listener.daemon import _box_id
        with patch.dict(os.environ, {"FARMSURV_BOX_ID": "test-box-42"}):
            assert _box_id() == "test-box-42"

    def test_box_id_fallback_to_hostname(self):
        """_box_id falls back to os.environ['HOSTNAME'] when env var missing."""
        from listener.daemon import _box_id
        # Clear the env var if set, use HOSTNAME as fallback.
        env = {k: v for k, v in os.environ.items()
               if k not in ("FARMSURV_BOX_ID", "HOSTNAME")}
        env["HOSTNAME"] = "myhost"
        with patch.dict(os.environ, env, clear=False):
            # Remove the key from os.environ explicitly.
            os.environ.pop("FARMSURV_BOX_ID", None)
            assert _box_id() == "myhost"

    def test_box_id_ultimate_fallback(self):
        """_box_id returns 'unknown' when neither env nor hostname available."""
        from listener.daemon import _box_id
        env = {k: v for k, v in os.environ.items()
               if k not in ("FARMSURV_BOX_ID", "HOSTNAME")}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("FARMSURV_BOX_ID", None)
            os.environ.pop("HOSTNAME", None)
            assert _box_id() == "unknown"
