"""Tests for _class_to_bucket helper in infra/quick_classifier.py.

AC4: at least 6 cases covering person, vehicle, animal, unmapped,
out-of-range, and edge cases.
"""

from __future__ import annotations

# No ONNX model needed — _class_to_bucket is a pure function.
from infra.quick_classifier import (
    ANIMAL_CLASSES,
    COCO_NAMES,
    PERSON_CLASSES,
    VEHICLE_CLASSES,
    _class_to_bucket,
)


class TestClassToBucket:
    """Unit tests for _class_to_bucket class_id → bucket mapping."""

    def test_person_yolo_id_0(self):
        """Person class_id 0 (YOLO 'person') → 'person'."""
        assert COCO_NAMES[0] == "person"
        assert _class_to_bucket(0) == "person"

    def test_vehicle_yolo_car_id_2(self):
        """Car class_id 2 (YOLO 'car') → 'vehicle'."""
        assert COCO_NAMES[2] == "car"
        assert _class_to_bucket(2) == "vehicle"

    def test_animal_yolo_dog_id_16(self):
        """Dog class_id 16 (YOLO 'dog') → 'animal'."""
        assert COCO_NAMES[16] == "dog"
        assert _class_to_bucket(16) == "animal"

    def test_unmapped_yolo_bench_id_13(self):
        """Bench class_id 13 (YOLO 'bench') → 'none'."""
        assert COCO_NAMES[13] == "bench"
        assert _class_to_bucket(13) == "none"

    def test_out_of_range_yolo_id_200(self):
        """Out-of-range class_id 200 → 'none'."""
        assert _class_to_bucket(200) == "none"

    def test_negative_class_id(self):
        """Negative class_id → 'none' (defensive)."""
        assert _class_to_bucket(-1) == "none"

    def test_vehicle_motorcycle_id_3(self):
        """Motorcycle class_id 3 (YOLO 'motorcycle') → 'vehicle'."""
        assert COCO_NAMES[3] == "motorcycle"
        assert _class_to_bucket(3) == "vehicle"

    def test_animal_cat_id_15(self):
        """Cat class_id 15 (YOLO 'cat') → 'animal'."""
        assert COCO_NAMES[15] == "cat"
        assert _class_to_bucket(15) == "animal"

    def test_bucket_coverage_all_coco(self):
        """Every COCO class_id maps to exactly one of {vehicle, person, animal, none}."""
        for class_id in range(len(COCO_NAMES)):
            result = _class_to_bucket(class_id)
            assert result in {"vehicle", "person", "animal", "none"}, \
                f"class_id={class_id} ({COCO_NAMES[class_id]}) mapped to '{result}'"

    def test_supervised_class_coverage(self):
        """All PERSON_CLASSES, VEHICLE_CLASSES, ANIMAL_CLASSES resolve correctly."""
        for class_id, name in enumerate(COCO_NAMES):
            if name in PERSON_CLASSES:
                assert _class_to_bucket(class_id) == "person"
            elif name in VEHICLE_CLASSES:
                assert _class_to_bucket(class_id) == "vehicle"
            elif name in ANIMAL_CLASSES:
                assert _class_to_bucket(class_id) == "animal"
