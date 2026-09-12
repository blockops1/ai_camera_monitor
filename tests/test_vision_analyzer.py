"""
test_vision_analyzer.py — Tests for infra.vision_analyzer.

Tests the following behaviors:
  1. verify_class returns parsed dict on 200.
  2. verify_class raises VisionAnalyzerError on non-200.
  3. detail_class dispatch keys match mode names.
  4. detail_class raises on unknown mode.
  5. ALL call sites send response_format nested under json_schema with
     strict=True and the canonical SCHEMA_JSON dict from the prompt
     module. The NESTED shape (not flat) is what llama-server actually
     enforces — flat shape gets 200 OK but no schema enforcement
     (model emits bare text). Verified 2026-09-09 against
     qwen3-vl-8b on localhost:8080.
  6. ALL call sites use the canonical model name "qwen3-vl-8b".
  7. VM2 prompt schemas declare the keys downstream code reads.
  8. Schema-prompt contract: VM1 declares class+confidence; VM2 modes
     declare class_confirmed + detail keys; NO schema has threat fields.
"""

from unittest.mock import MagicMock, patch

from infra.animal_prompt import SCHEMA_JSON as ANIMAL_SCHEMA
from infra.person_prompt import SCHEMA_JSON as PERSON_SCHEMA
from infra.vehicle_prompt import SCHEMA_JSON as VEHICLE_SCHEMA
from infra.vision_analyzer import (
    DISPATCH,
    VisionAnalyzerError,
    detail_class,
    verify_class,
)
from infra.vm1_prompt import SCHEMA_JSON as VM1_SCHEMA


def _mock_b64(_path: str):
    """Return a dummy base64 image dict so _b64 never touches the filesystem."""
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,xx"}}


def _mock_httpx_post(status_code=200, json_content=None, text="ok"):
    """Build a mock response for patching httpx.post."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_content is not None:
        resp.json.return_value = json_content
    return resp


def _extract_payload(mock_post):
    """Pull the json= kwarg from a patched httpx.post call."""
    args, kwargs = mock_post.call_args
    return kwargs.get("json") or args[1]


class TestVerifyClass:
    """Tests for verify_class()."""

    def test_verify_class_returns_parsed_dict(self, sample_frames):
        """verify_class returns the parsed JSON dict on a 200 response."""
        mock_resp = _mock_httpx_post(
            200,
            {"choices": [{"message": {"content": '{"class": "vehicle"}'}}]},
        )
        with (
            patch("infra.vision_analyzer._b64", _mock_b64),
            patch("infra.vision_analyzer.httpx.post", return_value=mock_resp),
        ):
            result = verify_class(sample_frames[0], sample_frames[1])
        assert result == {"class": "vehicle"}

    def test_verify_class_raises_on_non_200(self, sample_frames):
        """verify_class raises VisionAnalyzerError on non-200 status."""
        mock_resp = _mock_httpx_post(500, text="internal error")
        with (
            patch("infra.vision_analyzer._b64", _mock_b64),
            patch("infra.vision_analyzer.httpx.post", return_value=mock_resp),
        ):
            try:
                verify_class(sample_frames[0], sample_frames[1])
                assert False, "should have raised"
            except VisionAnalyzerError:
                pass


class TestDetailClass:
    """Tests for detail_class()."""

    def test_detail_class_dispatch_keys_match_mode_names(self):
        """DISPATCH keys are exactly 'vehicle', 'person', 'animal'."""
        assert sorted(DISPATCH.keys()) == ["animal", "person", "vehicle"]

    def test_detail_class_raises_on_unknown_mode(self, sample_frames):
        """detail_class raises VisionAnalyzerError on an unknown mode."""
        try:
            detail_class("unknown", sample_frames[0], sample_frames[1])
            assert False, "should have raised"
        except VisionAnalyzerError:
            pass


class TestLlamaServerPayload:
    """Regression tests for the llama-server payload shape.

    llama-server (qwen3-vl-8b backend) enforces strict-mode schema
    ONLY when response_format is shaped:
        {
          "type": "json_schema",
          "json_schema": {
            "name": "<schema-name>",
            "strict": True,
            "schema": <dict>
          }
        }
    The flat shape (`type/strict/schema` at top level) returns 200 OK
    but the model emits bare text — silent failure mode.

    `strict: True` enforces key names + required fields + enums
    server-side. Without it, downstream key lookups can miss and
    crash the pipeline.

    These tests assert both the wire format AND that the nested
    envelope is used (so a regression that flattened the shape gets
    caught here, not from camera traffic).
    """

    def test_verify_class_payload_uses_nested_strict_json_schema(
        self, sample_frames
    ):
        """verify_class nests response_format under json_schema with
        strict=True and the VM1 schema dict. Nested shape is what
        llama-server actually enforces."""
        mock_resp = _mock_httpx_post(
            200,
            {"choices": [{"message": {"content": '{"class": "vehicle"}'}}]},
        )
        with (
            patch("infra.vision_analyzer._b64", _mock_b64),
            patch(
                "infra.vision_analyzer.httpx.post", return_value=mock_resp
            ) as post,
        ):
            verify_class(sample_frames[0], sample_frames[1])
        payload = _extract_payload(post)
        rf = payload["response_format"]
        assert rf["type"] == "json_schema", (
            f"response_format.type must be 'json_schema', got {rf['type']!r}"
        )
        # Envelope is NESTED under json_schema, not flat.
        nested = rf["json_schema"]
        assert nested["strict"] is True, (
            f"nested.strict must be True, got {nested['strict']!r}"
        )
        assert nested["schema"] is VM1_SCHEMA, (
            "VM1 response_format must reference the canonical SCHEMA_JSON "
            "from infra.vm1_prompt"
        )
        assert payload["model"] == "qwen3-vl-8b"

    def test_detail_class_vehicle_payload_uses_nested_strict_json_schema(
        self, sample_frames
    ):
        """detail_class(vehicle) sends nested strict-json_schema envelope."""
        mock_resp = _mock_httpx_post(
            200,
            {"choices": [{"message": {"content": '{"class_confirmed": "vehicle"}'}}]},
        )
        with (
            patch("infra.vision_analyzer._b64", _mock_b64),
            patch(
                "infra.vision_analyzer.httpx.post", return_value=mock_resp
            ) as post,
        ):
            detail_class("vehicle", sample_frames[0], sample_frames[1])
        payload = _extract_payload(post)
        rf = payload["response_format"]
        nested = rf["json_schema"]
        assert rf["type"] == "json_schema"
        assert nested["strict"] is True
        assert nested["schema"] is VEHICLE_SCHEMA, (
            "vehicle response_format must reference the vehicle SCHEMA_JSON"
        )
        assert payload["model"] == "qwen3-vl-8b"

    def test_detail_class_person_payload_uses_nested_strict_json_schema(
        self, sample_frames
    ):
        """detail_class(person) sends nested strict-json_schema envelope."""
        mock_resp = _mock_httpx_post(
            200,
            {"choices": [{"message": {"content": '{"class_confirmed": "person"}'}}]},
        )
        with (
            patch("infra.vision_analyzer._b64", _mock_b64),
            patch(
                "infra.vision_analyzer.httpx.post", return_value=mock_resp
            ) as post,
        ):
            detail_class("person", sample_frames[0], sample_frames[1])
        payload = _extract_payload(post)
        rf = payload["response_format"]
        nested = rf["json_schema"]
        assert rf["type"] == "json_schema"
        assert nested["strict"] is True
        assert nested["schema"] is PERSON_SCHEMA
        assert payload["model"] == "qwen3-vl-8b"

    def test_detail_class_animal_payload_uses_nested_strict_json_schema(
        self, sample_frames
    ):
        """detail_class(animal) sends nested strict-json_schema envelope."""
        mock_resp = _mock_httpx_post(
            200,
            {"choices": [{"message": {"content": '{"class_confirmed": "animal"}'}}]},
        )
        with (
            patch("infra.vision_analyzer._b64", _mock_b64),
            patch(
                "infra.vision_analyzer.httpx.post", return_value=mock_resp
            ) as post,
        ):
            detail_class("animal", sample_frames[0], sample_frames[1])
        payload = _extract_payload(post)
        rf = payload["response_format"]
        nested = rf["json_schema"]
        assert rf["type"] == "json_schema"
        assert nested["strict"] is True
        assert nested["schema"] is ANIMAL_SCHEMA
        assert payload["model"] == "qwen3-vl-8b"


class TestSchemaContract:
    """Tests for the schema-vs-downstream contract.

    These tests catch the bug where VM2 prompt schemas declare key names
    that the downstream code (telegram_formatter/detail.py,
    vehicle_matcher/match.py) doesn't read -- e.g. schema had
    `plate_text`/`distinctive` while code read `license_plate`/
    `distinctive_features`. The pipeline crashed at the key lookup.
    """

    def test_vehicle_schema_vehicle_features_key_present(self):
        """vehicle SCHEMA_JSON declares 'vehicle_features' (downstream reads this)."""
        assert "vehicle_features" in VEHICLE_SCHEMA["properties"]

    def test_vehicle_schema_confidence_is_number(self):
        """vehicle SCHEMA_JSON 'confidence' is type number with 0-1 bounds."""
        conf = VEHICLE_SCHEMA["properties"]["confidence"]
        assert conf["type"] == "number"
        assert conf.get("minimum") == 0.0
        assert conf.get("maximum") == 1.0

    def test_vehicle_schema_no_invented_fields(self):
        """vehicle SCHEMA_JSON has no invented PII fields (v1 D1 parity)."""
        allowed = {
            "color", "body_style_hint", "make", "model",
            "vehicle_features", "description", "confidence", "notable_details",
        }
        found = set(VEHICLE_SCHEMA["properties"].keys())
        extra = found - allowed
        assert not extra, (
            f"Vehicle schema must not have extra fields beyond v1 shape. "
            f"Found: {extra}"
        )

    def test_vehicle_schema_no_threat_fields(self):
        """vehicle SCHEMA_JSON has NO threat-level fields (operator-locked)."""
        forbidden = {"threat_indicators", "threat_level", "threat_score"}
        assert not (forbidden & set(VEHICLE_SCHEMA["properties"].keys())), (
            f"Vehicle schema must not have threat fields. Found: "
            f"{forbidden & set(VEHICLE_SCHEMA['properties'].keys())}"
        )

    def test_person_schema_distinctive_features_key_present(self):
        """person SCHEMA_JSON declares 'distinctive_features' (downstream reads this)."""
        assert "distinctive_features" in PERSON_SCHEMA["properties"]

    def test_person_schema_no_threat_fields(self):
        """person SCHEMA_JSON has NO threat-level fields."""
        forbidden = {"threat_indicators", "threat_level", "threat_score"}
        assert not (forbidden & set(PERSON_SCHEMA["properties"].keys()))

    def test_animal_schema_distinctive_features_key_present(self):
        """animal SCHEMA_JSON declares 'distinctive_features'."""
        assert "distinctive_features" in ANIMAL_SCHEMA["properties"]

    def test_animal_schema_no_threat_fields(self):
        """animal SCHEMA_JSON has NO threat-level fields."""
        forbidden = {"threat_indicators", "threat_level", "threat_score"}
        assert not (forbidden & set(ANIMAL_SCHEMA["properties"].keys()))

    def test_vm1_schema_keys_are_class_and_confidence(self):
        """VM1 SCHEMA_JSON declares 'class' and 'confidence' (pipeline.py:150 reads 'class')."""
        keys = set(VM1_SCHEMA["properties"].keys())
        assert keys == {"class", "confidence"}
