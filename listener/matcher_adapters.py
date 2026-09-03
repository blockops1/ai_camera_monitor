"""
matcher_adapters.py — glue between single_pipeline.run() and the existing
class-specific pipelines.

§11.115 cutover: single_pipeline.run() expects matchers to expose:
    .vehicle(classify, call2_response, crop_a, crop_b, **kwargs) -> dict
    .person(classify, call2_response, crop_a, crop_b, **kwargs) -> dict
    .animal(classify, call2_response, crop_a, crop_b, **kwargs) -> dict

The existing pipelines (process_alert, process_person_event,
process_animal_event) take their own context objects. These adapters
build the right context, call the pipeline, and return the result dict.

For §11.115.6 cutover we DON'T yet thread `call2_response` into the
per-class pipelines — they continue to call Qwen internally for now.
Sub-tasks §11.115.9/10/11 will refactor each pipeline to consume the
shared call2_response and skip its own Qwen call. Until then, the
shared cascade's call2 result is captured in the PipelineResult log
but not consumed by the per-class matchers.

STATUS: provisional (Phase §11.115)
INPUTS: alert_frame_dir, camera_name, timestamp, rtsp_url, vision_api_url,
        bot_token, chat_id, gatekeeper_cameras, camera_code_lookup,
        known_vehicles
OUTPUTS: dict[str, Any] from each pipeline's result
DOES NOT DO: build crops (frame_diff_fn does that), call Qwen, send Telegram.
CALLED BY: listener.single_pipeline.run()
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class SinglePipelineMatchers:
    """Adapts class-specific pipelines to single_pipeline's matcher interface."""

    alert_frame_dir: str
    camera_name: str
    timestamp: str
    rtsp_url: str
    vision_api_url: str
    bot_token: str
    chat_id: str
    gatekeeper_cameras: frozenset
    camera_code_lookup: Callable[[str], str]
    known_vehicles: list | None = None  # default None; vehicle pipeline loads its own
    gate_verdict: Any | None = None  # gate verdict (when gate ran); per-class matchers reuse gate frames
    known_animals: list | None = None  # default None; animal matcher loads its own

    def _known_animals(self) -> list:
        """§11.115.10: load enrolled animals from data/animals/known_animals.json.

        Lazy + cached per-instance. Returns empty list if file missing
        or store class not importable (defensive — match_animal handles
        empty list by returning AnimalNoMatch).
        """
        if self.known_animals is not None:
            return self.known_animals
        try:
            from known_animals.store import load_known_animals
            return load_known_animals()
        except Exception:
            return []

    def _output_dir(self, alert_id: str) -> str:
        return os.path.join(self.alert_frame_dir, alert_id)

    # ------------------------------------------------------------------
    # vehicle
    # ------------------------------------------------------------------
    def vehicle(
        self,
        classify: Any,
        call2_response: dict | None,
        crop_a: str,
        crop_b: str,
        alert_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run the vehicle pipeline via process_alert(ctx).

        Phase §11.115.6: does NOT yet consume `call2_response` — the
        pipeline runs its own identify stage (calls Qwen internally).
        §11.115.11 will consolidate.
        """
        try:
            from listener.vehicle_pipeline import (
                AlertContext,
                process_alert,
            )
        except ImportError:
            from vehicle_pipeline import AlertContext, process_alert  # type: ignore

        camera_code = self.camera_code_lookup(self.camera_name)
        ctx = AlertContext(
            alert_id=alert_id,
            camera_name=self.camera_name,
            camera_code=camera_code,
            timestamp=self.timestamp,
            event_type="vehicle",
            rtsp_url=self.rtsp_url,
            output_dir=self._output_dir(alert_id),
            is_vehicle_event=True,
            known_vehicles=self.known_vehicles or [],
            bot_token=self.bot_token,
            chat_id=self.chat_id,
            api_url=self.vision_api_url,
            gatekeeper_cameras=self.gatekeeper_cameras,
            gate_verdict=self.gate_verdict,
            # §11.115.11: thread cascade's call2_response into the
            # vehicle AlertContext so identify_stage can skip its
            # internal identify_from_crops (Qwen) call when the
            # cascade already produced a matching schema.
            vision_result=call2_response or None,
        )
        try:
            result = process_alert(ctx)
            return dict(result) if result else {}
        except Exception as exc:  # pragma: no cover — defensive
            return {
                "alert_id": alert_id,
                "telegram_sent": False,
                "error": f"vehicle pipeline raised: {exc!r}",
            }

    # ------------------------------------------------------------------
    # person
    # ------------------------------------------------------------------
    def person(
        self,
        classify: Any,
        call2_response: dict | None,
        crop_a: str,
        crop_b: str,
        alert_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run the person pipeline via process_person_event(ctx)."""
        try:
            from listener.person_event_pipeline import (
                PersonContext,
                process_person_event,
            )
        except ImportError:
            from person_event_pipeline import (  # type: ignore
                PersonContext,
                process_person_event,
            )

        ctx = PersonContext(
            alert_id=alert_id,
            camera_name=self.camera_name,
            timestamp=self.timestamp,
            event_type="person",
            rtsp_url=self.rtsp_url,
            output_dir=self._output_dir(alert_id),
            bot_token=self.bot_token,
            chat_id=self.chat_id,
            api_url=self.vision_api_url,
            # §11.115.9: thread cascade result into PersonContext so
            # person_identify_stage consumes call2_response (no internal
            # Qwen call) and ArcFace runs on the chosen crop.
            classify=classify,
            call2_response=call2_response,
            crop_a_path=crop_a,
            crop_b_path=crop_b,
        )
        try:
            result = process_person_event(ctx)
            return dict(result) if result else {}
        except Exception as exc:  # pragma: no cover — defensive
            return {
                "alert_id": alert_id,
                "telegram_sent": False,
                "error": f"person pipeline raised: {exc!r}",
            }

    # ------------------------------------------------------------------
    # animal
    # ------------------------------------------------------------------
    def animal(
        self,
        classify: Any,
        call2_response: dict | None,
        crop_a: str,
        crop_b: str,
        alert_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run the animal pipeline via process_animal_event(ctx)."""
        try:
            from listener.animal_event_pipeline import (
                AnimalContext,
                process_animal_event,
            )
        except ImportError:
            from animal_event_pipeline import (  # type: ignore
                AnimalContext,
                process_animal_event,
            )

        ctx = AnimalContext(
            alert_id=alert_id,
            camera_name=self.camera_name,
            timestamp=self.timestamp,
            event_type="animal",
            rtsp_url=self.rtsp_url,
            output_dir=self._output_dir(alert_id),
            bot_token=self.bot_token,
            chat_id=self.chat_id,
            api_url=self.vision_api_url,
            # §11.115.10: thread cascade result + enrolled registry into
            # AnimalContext so animal_match consumes call2_response
            # directly (no internal Qwen call) and match_animal sees
            # the enroll list.
            classify=classify,
            call2_response=call2_response,
            crop_a_path=crop_a,
            crop_b_path=crop_b,
            known_animals=self._known_animals(),
        )
        try:
            result = process_animal_event(ctx)
            return dict(result) if result else {}
        except Exception as exc:  # pragma: no cover — defensive
            return {
                "alert_id": alert_id,
                "telegram_sent": False,
                "error": f"animal pipeline raised: {exc!r}",
            }
