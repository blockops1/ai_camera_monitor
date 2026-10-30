"""Regression tests: subjects allowlist 'animal' expansion (2026-10-24).

Root cause: config subjects=["person","animal"] was matched literally against
COCO class names; "dog"/"cat"/"bird" never equal "animal", so EVERY lone
animal was suppressed as top_class_<class>_not_in_subjects on FRONT/BACK/
GARAGE. Animal notifications were dead system-wide (found during operator
live dog test at the farm; bird @0.81 dropped at OUTSIDE_BACK_SOLAR).

Fix: infra.gate._subject_allowed expands 'animal' -> ANIMAL_CLASSES.
"""

from infra.gate import THRESHOLDS_BY_CLASS, _route_decision, _subject_allowed
from infra.quick_classifier import QuickVerdict


def _verdict(cls, conf):
    return QuickVerdict(top_class=cls, top_confidence=conf, decision="pass")


def _suppress(cls, conf, reason):
    return QuickVerdict(top_class=cls, top_confidence=conf,
                       decision="suppress", reason=reason)


FRONT_SUBJECTS = ["person", "animal", "vehicle", "bicycle"]


class TestSubjectAllowed:
    def test_animal_expands_to_coco_classes(self):
        for cls in ("dog", "cat", "horse", "sheep", "cow", "bear", "bird"):
            assert _subject_allowed(["person", "animal"], cls)

    def test_non_animal_not_expanded(self):
        assert not _subject_allowed(["person", "animal"], "car")
        assert not _subject_allowed(["person", "animal"], "bench")

    def test_literal_match_still_works(self):
        assert _subject_allowed(["person"], "person")

    def test_none_subjects_denies(self):
        # subjects None = caller decides separately (perimeter path)
        assert not _subject_allowed(None, "dog")


class TestRouteDecisionAnimalPass:
    def test_lone_dog_on_front_passes_as_animal(self):
        dec, label, conf, reason = _route_decision(
            _verdict("dog", 0.72), _verdict("dog", 0.68),
            THRESHOLDS_BY_CLASS, subjects=FRONT_SUBJECTS,
        )
        assert dec == "animal"
        assert label == "dog"
        assert reason == "subject_allowed"

    def test_dog_below_threshold_still_suppressed(self):
        # dog default threshold is 0.60 — the per-crop classifier already
        # suppressed these; routing must NOT resurrect them.
        a = _suppress("dog", 0.40, "class_below_threshold")
        b = _suppress("dog", 0.38, "class_below_threshold")
        dec, _, _, reason = _route_decision(a, b, THRESHOLDS_BY_CLASS,
                                           subjects=FRONT_SUBJECTS)
        assert dec == "none"
        assert reason == "class_below_threshold"

    def test_bird_on_perimeter_still_suppressed(self):
        # Perimeter cameras have no subjects key: behavior unchanged —
        # lone animals keep being suppressed there (operator intent:
        # perimeter = vehicles only).
        dec, _, _, reason = _route_decision(
            _verdict("bird", 0.81),
            _suppress("none", 0.0, "no_object_detected"),
            THRESHOLDS_BY_CLASS, subjects=None,
        )
        assert dec == "none"
        assert reason == "top_class_bird_not_in_subjects"

    def test_car_top_with_dog_on_perimeter_vehicle_wins(self):
        # Rule 1 unchanged on perimeter: car as top class -> vehicle.
        dec, label, _, reason = _route_decision(
            _verdict("car", 0.70), _verdict("dog", 0.65),
            THRESHOLDS_BY_CLASS, subjects=None,
        )
        assert dec == "vehicle"
        assert label == "car"
        assert reason == "high_conf_vehicle"

    def test_person_on_front_unchanged(self):
        dec, label, _, reason = _route_decision(
            _verdict("person", 0.66), _verdict("person", 0.61),
            THRESHOLDS_BY_CLASS, subjects=FRONT_SUBJECTS,
        )
        assert dec == "person"
        assert reason == "high_conf_person"

    def test_animal_top_excluded_vehicle_in_mix_routes_animal(self):
        # Camera whose subjects exclude vehicle (e.g. hypothetical
        # person+animal config): dog on top with car in mix -> animal
        # via subject_allowed, not suppression.
        dec, label, _, reason = _route_decision(
            _verdict("dog", 0.85), _verdict("car", 0.75),
            THRESHOLDS_BY_CLASS, subjects=["person", "animal"],
        )
        assert dec == "animal"
        assert label == "dog"
        assert reason == "subject_allowed"


class TestAllCamerasHaveAnimals:
    """Operator directive 2026-10-24: animals on ALL cameras.

    Locks config/motion_gate_thresholds.json: every camera's subjects list
    must include "animal" (expanded to COCO classes by _subject_allowed).
    """

    def test_all_cameras_subjects_include_animal(self):
        import json
        import pathlib
        from infra.paths import PROJECT_ROOT
        cfg = json.loads(
            (pathlib.Path(str(PROJECT_ROOT)) / "config"
             / "motion_gate_thresholds.json").read_text()
        )
        cams = [k for k in cfg if not k.startswith("_")]
        assert len(cams) >= 6
        for cam in cams:
            subjects = cfg[cam].get("subjects")
            assert subjects and "animal" in subjects, f"{cam} missing animal"
