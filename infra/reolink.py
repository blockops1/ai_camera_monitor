"""ReolinkClient -- typed facade over reolink-cli subprocess.

Standalone module. Zero imports from listener/, infra/frame_capture.py,
infra/gate.py, or any other farm-surveillance submodule.

Usage::

    from infra.reolink import ReolinkClient

    client = ReolinkClient(gateway_addr="<gateway-addr>")
    info = client.info(camera="FRONT")
    ai_cfg = client.detect_ai_get(camera="FRONT", atype="person")
    client.detect_ai_set(camera="FRONT", atype="person", sensitivity=50)
    client.detect_ai_apply(camera="FRONT", atype="person", sensitivity=50)
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
# _run_json normalizes all responses into one of these shapes, so
# every _parse_* helper must accept both.

JsonDict = dict[str, Any]
JsonList = list[Any]
JsonValue = dict[str, Any] | list[Any]


def _to_dict(v: JsonValue) -> JsonDict:
    """Return *v* as a dict, wrapping in a one-element list if it's a list."""
    if isinstance(v, dict):
        return v
    return {"_wrapped_list": v}


def _to_list(v: JsonValue) -> list[Any]:
    """Return *v* as a list, wrapping a dict in a one-element list."""
    if isinstance(v, list):
        return v
    return [v]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ReolinkCommandError(Exception):
    """Non-zero exit from reolink-cli."""
    def __init__(self, exit_code: int, stdout: str, stderr: str, command: list[str]) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.command = command
        super().__init__(
            f"reolink-cli exited {exit_code}: {' '.join(command)}\n"
            f"stdout: {stdout[:500]}\nstderr: {stderr[:500]}"
        )


class ReolinkAuthError(Exception):
    """Authentication failure from reolink-cli."""
    def __init__(self, message: str = "Authentication failed") -> None:
        super().__init__(message)


class ReolinkTimeoutError(Exception):
    """Subprocess timed out."""
    def __init__(self, seconds: float, command: list[str]) -> None:
        self.seconds = seconds
        self.command = command
        super().__init__(f"reolink-cli timed out after {seconds}s: {' '.join(command)}")


# ---------------------------------------------------------------------------
# Dataclass return types
# ---------------------------------------------------------------------------

@dataclass
class DetectionConfig:
    """Result of detect ai get / detect motion get."""
    enabled: bool = False
    sensitivity: int = 50
    person_stay_time: int = 0
    vehicle_stay_time: int = 0
    dog_cat_stay_time: int = 0
    motion_area: str = ""  # JSON mask area
    alarm_type: str = ""
    delay_time: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EncodeConfig:
    """Result of encode get."""
    resolution: str = ""
    frame_rate: int = 0
    bit_rate: int = 0
    codec: str = ""
    max_bit_rate: int = 0
    min_bit_rate: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AudioConfig:
    """Result of audio config get."""
    volume: int = 50
    alarm_mute: bool = False
    siren_enabled: bool = False
    quick_replies: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LightConfig:
    """Result of light get."""
    ir_enabled: bool = False
    status_led_enabled: bool = False
    spotlight_enabled: bool = False
    whiteled_alarm: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImageConfig:
    """Result of image get."""
    flip: bool = False
    mirror: bool = False
    brightness: int = 50
    contrast: int = 50
    saturation: int = 50
    hue: int = 50
    sharpness: int = 50
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PrivacyMask:
    """A single privacy mask region."""
    x: int = 0
    y: int = 0
    width: int = 100
    height: int = 100
    enabled: bool = True


@dataclass
class UserEntry:
    """A device user account."""
    username: str = ""
    user_type: str = "admin"
    online: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OsdConfig:
    """Result of osd get."""
    channel_name: str = ""
    time_overlay: bool = True
    logo_overlay: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecordSchedule:
    """Result of record schedule get."""
    continuous: bool = True
    motion_detect: bool = False
    ai_detect: bool = False
    pre_record_sec: int = 5
    post_record_sec: int = 5
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecordConfig:
    """Result of record config get."""
    cycle_record: bool = True
    pre_record_sec: int = 5
    post_record_sec: int = 5
    package_sec: int = 60
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventEntry:
    """A single event from events query."""
    event_id: str = ""
    event_type: str = ""
    channel: int = 0
    start_time: str = ""
    end_time: str = ""
    thumbnail: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class VodEntry:
    """A single VOD recording entry."""
    file_name: str = ""
    start_time: str = ""
    end_time: str = ""
    channel: int = 0
    event_type: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeviceInfo:
    """Result of info / login."""
    model: str = ""
    firmware: str = ""
    serial: str = ""
    online: bool = False
    resolution: str = ""
    channels: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CapabilitiesResult:
    """Result of capabilities."""
    features: list[str] = field(default_factory=list)
    ptz_supported: bool = False
    ai_detection: bool = False
    motion_detection: bool = False
    two_way_audio: bool = False
    spotlight: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PingResult:
    """Result of ping."""
    reachable: bool = False
    rtt_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeviceListEntry:
    """A single entry from device list."""
    alias: str = ""
    host: str = ""
    channel: int = 0
    model: str = ""
    serial: str = ""
    online: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PTZPreset:
    """A PTZ preset."""
    name: str = ""
    index: int = 0


@dataclass
class PTZPatrol:
    """A PTZ patrol route."""
    name: str = ""
    presets: list[str] = field(default_factory=list)


@dataclass
class PTZGuard:
    """A PTZ guard position."""
    enabled: bool = False
    preset_index: int = 0


@dataclass
class PTZAutotrack:
    """AI auto-tracking state."""
    enabled: bool = False
    target_type: str = ""


@dataclass
class DeviceInventory:
    """Result of device inventory."""
    entries: list[DeviceListEntry] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ReolinkClient
# ---------------------------------------------------------------------------

class ReolinkClient:
    """Typed facade over reolink-cli subprocess calls.

    All methods shell out to `reolink-cli` with ``--output json``,
    parse the result, and return a typed dataclass.

    Credentials flow through reolink-cli's own ``--host``/``--camera``
    resolution; the client never carries a password.
    """

    def __init__(
        self,
        gateway_addr: str | None = None,
        binary_path: str | None = None,
        timeout_sec: float | None = None,
    ) -> None:
        self._binary = binary_path or os.environ.get(
            "FARM_REOLINK_CLI_BIN", "reolink-cli"
        )
        self._gateway = gateway_addr or os.environ.get(
            "FARM_REOLINK_GATEWAY_ADDR", ""
        )
        self._timeout = timeout_sec or float(
            os.environ.get("FARM_REOLINK_TIMEOUT_SEC", "30")
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_args(
        self,
        *subcommands: str,
        camera: str | None = None,
        host: str | None = None,
        **kw: Any,
    ) -> list[str]:
        """Build the full reolink-cli command list.

        Args:
            *subcommands: One or more positional subcommand tokens
                (e.g. ``"detect"``, ``"ai"``, ``"get"``).
            camera: Camera alias name.
            host: Device IP/host (used when no gateway is configured).
            **kw: Optional flags (e.g. ``sensitivity=50``, ``type="person"``).
        """
        args = [self._binary, "--output", "json"]
        if self._gateway:
            args += ["--gateway-addr", self._gateway]
        if camera:
            args += ["--camera", camera]
        if host:
            args += ["--host", host]
        args.extend(subcommands)
        for k, v in kw.items():
            if v is not None:
                args += ["--" + k.replace("_", "-"), str(v)]
        return args

    def _run_json(
        self,
        *subcommands: str,
        camera: str | None = None,
        host: str | None = None,
        expect_list: bool = False,
        **kw: Any,
    ) -> list[Any] | dict[str, Any]:
        args = self._build_args(*subcommands, camera=camera, host=host, **kw)
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ReolinkTimeoutError(self._timeout, args)

        if proc.returncode != 0:
            stderr_lower = proc.stderr.lower()
            if any(
                kw_ in stderr_lower
                for kw_ in ("auth", "401", "unauthorized", "login failed")
            ):
                raise ReolinkAuthError(proc.stderr.strip())
            raise ReolinkCommandError(
                proc.returncode, proc.stdout, proc.stderr, args
            )

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"_raw_text": proc.stdout}

        # Normalize: some commands return {"code": 0, "data": {...}}
        if isinstance(data, dict) and "data" in data and not expect_list:
            return data["data"]
        if isinstance(data, dict) and "data" in data and expect_list:
            d = data["data"]
            if isinstance(d, list):
                return d
            return [d]
        if isinstance(data, list):
            return data
        return data

    def _run_no_output(
        self,
        *subcommands: str,
        camera: str | None = None,
        host: str | None = None,
        **kw: Any,
    ) -> None:
        args = self._build_args(*subcommands, camera=camera, host=host, **kw)
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ReolinkTimeoutError(self._timeout, args)

        if proc.returncode != 0:
            stderr_lower = proc.stderr.lower()
            if any(
                kw_ in stderr_lower
                for kw_ in ("auth", "401", "unauthorized", "login failed")
            ):
                raise ReolinkAuthError(proc.stderr.strip())
            raise ReolinkCommandError(
                proc.returncode, proc.stdout, proc.stderr, args
            )

    # ------------------------------------------------------------------
    # detect_ai_*
    # ------------------------------------------------------------------

    def detect_ai_get(
        self,
        atype: str,
        camera: str | None = None,
        host: str | None = None,
    ) -> DetectionConfig:
        """Get AI detection config for a type (person, vehicle, dog_cat)."""
        raw = self._run_json("detect", "ai", "get", camera=camera, host=host, type=atype)
        return self._parse_detection_config(raw)

    def detect_ai_set(
        self,
        atype: str,
        camera: str | None = None,
        host: str | None = None,
        sensitivity: int | None = None,
        stay_time: int | None = None,
        **extra: Any,
    ) -> None:
        """Set AI detection config."""
        self._run_no_output(
            "detect", "ai", "set",
            camera=camera, host=host, type=atype,
            sensitivity=sensitivity,
            **extra,
        )

    def detect_ai_apply(
        self,
        atype: str,
        camera: str | None = None,
        host: str | None = None,
        sensitivity: int | None = None,
        stay_time: int | None = None,
        **extra: Any,
    ) -> DetectionConfig:
        """Idempotent apply: get -> skip if same, else set -> verify."""
        raw = self._run_json(
            "detect", "ai", "apply",
            camera=camera, host=host, type=atype,
            sensitivity=sensitivity,
            **extra,
        )
        return self._parse_detection_config(raw)

    # ------------------------------------------------------------------
    # detect_motion_*
    # ------------------------------------------------------------------

    def detect_motion_get(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> DetectionConfig:
        """Get motion detection config."""
        raw = self._run_json("detect", "motion", "get", camera=camera, host=host)
        return self._parse_detection_config(raw)

    def detect_motion_set(
        self,
        camera: str | None = None,
        host: str | None = None,
        sensitivity: int | None = None,
        enabled: bool | None = None,
        **extra: Any,
    ) -> None:
        """Set motion detection config."""
        self._run_no_output(
            "detect", "motion", "set",
            camera=camera, host=host,
            sensitivity=sensitivity,
            enabled=enabled,
            **extra,
        )

    def detect_motion_apply(
        self,
        camera: str | None = None,
        host: str | None = None,
        sensitivity: int | None = None,
        enabled: bool | None = None,
        **extra: Any,
    ) -> DetectionConfig:
        """Idempotent apply for motion detection."""
        raw = self._run_json(
            "detect", "motion", "apply",
            camera=camera, host=host,
            sensitivity=sensitivity,
            **extra,
        )
        return self._parse_detection_config(raw)

    # ------------------------------------------------------------------
    # encode_*
    # ------------------------------------------------------------------

    def encode_get(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> EncodeConfig:
        """Get encoder settings."""
        raw = self._run_json("encode", "get", camera=camera, host=host)
        return self._parse_encode_config(raw)

    def encode_set(
        self,
        camera: str | None = None,
        host: str | None = None,
        resolution: str | None = None,
        frame_rate: int | None = None,
        bit_rate: int | None = None,
        codec: str | None = None,
        **extra: Any,
    ) -> None:
        """Set encoder settings."""
        self._run_no_output(
            "encode", "set",
            camera=camera, host=host,
            resolution=resolution,
            frame_rate=frame_rate,
            bit_rate=bit_rate,
            codec=codec,
            **extra,
        )

    # ------------------------------------------------------------------
    # audio_*
    # ------------------------------------------------------------------

    def audio_get(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> AudioConfig:
        """Get audio configuration."""
        raw = self._run_json("audio", "config", "get", camera=camera, host=host)
        return self._parse_audio_config(raw)

    def audio_set(
        self,
        camera: str | None = None,
        host: str | None = None,
        volume: int | None = None,
        mute: bool | None = None,
        unmute: bool | None = None,
        **extra: Any,
    ) -> None:
        """Set audio configuration."""
        if mute:
            self._run_no_output("audio", "mute", camera=camera, host=host, **extra)
        elif unmute:
            self._run_no_output("audio", "unmute", camera=camera, host=host, **extra)
        else:
            self._run_no_output(
                "audio", "config", "set",
                camera=camera, host=host, volume=volume, **extra,
            )

    # ------------------------------------------------------------------
    # light_*
    # ------------------------------------------------------------------

    def light_get(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> LightConfig:
        """Get LED/spotlight state."""
        raw = self._run_json("light", "ir", "get", camera=camera, host=host)
        return self._parse_light_config(raw)

    def light_set(
        self,
        camera: str | None = None,
        host: str | None = None,
        ir_enabled: bool | None = None,
        status_led: bool | None = None,
        spotlight: bool | None = None,
        whiteled: bool | None = None,
        **extra: Any,
    ) -> None:
        """Set LED/spotlight state."""
        if ir_enabled is not None:
            self._run_no_output("light", "ir", "set", camera=camera, host=host,
                                enabled=ir_enabled, **extra)
        if status_led is not None:
            self._run_no_output("light", "statusled", "set", camera=camera, host=host,
                                enabled=status_led, **extra)
        if spotlight is not None:
            self._run_no_output("light", "spotlight", camera=camera, host=host,
                                enabled=spotlight, **extra)
        if whiteled is not None:
            self._run_no_output("light", "whiteled", "set", camera=camera, host=host,
                                enabled=whiteled, **extra)

    # ------------------------------------------------------------------
    # image_*
    # ------------------------------------------------------------------

    def image_get(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> ImageConfig:
        """Get image orientation and tuning."""
        raw = self._run_json("image", "flip", "get", camera=camera, host=host)
        return self._parse_image_config(raw)

    def image_set(
        self,
        camera: str | None = None,
        host: str | None = None,
        flip: bool | None = None,
        mirror: bool | None = None,
        brightness: int | None = None,
        contrast: int | None = None,
        saturation: int | None = None,
        hue: int | None = None,
        sharpness: int | None = None,
        **extra: Any,
    ) -> None:
        """Set image orientation/tuning."""
        if flip is not None or mirror is not None:
            self._run_no_output(
                "image", "flip", "set",
                camera=camera, host=host,
                flip=flip, mirror=mirror,
                **extra,
            )
        if any(v is not None for v in (brightness, contrast, saturation, hue, sharpness)):
            self._run_no_output(
                "image", "tune", "set",
                camera=camera, host=host,
                brightness=brightness, contrast=contrast,
                saturation=saturation, hue=hue, sharpness=sharpness,
                **extra,
            )

    # ------------------------------------------------------------------
    # privacy_*
    # ------------------------------------------------------------------

    def privacy_get(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> list[PrivacyMask]:
        """Get privacy mask regions."""
        raw = self._run_json("privacy", "mask", "get", camera=camera, host=host,
                             expect_list=True)
        return self._parse_privacy_masks(raw)

    def privacy_list(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> list[PrivacyMask]:
        """Alias for privacy_get."""
        return self.privacy_get(camera=camera, host=host)

    def privacy_set(
        self,
        camera: str | None = None,
        host: str | None = None,
        x: int = 0,
        y: int = 0,
        width: int = 100,
        height: int = 100,
        enabled: bool = True,
        **extra: Any,
    ) -> None:
        """Add/update a privacy mask region."""
        self._run_no_output(
            "privacy", "mask", "set",
            camera=camera, host=host,
            x=x, y=y, width=width, height=height,
            enabled=enabled,
            **extra,
        )

    def privacy_delete(
        self,
        index: int,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Delete a privacy mask by index."""
        self._run_no_output(
            "privacy", "mask", "delete",
            camera=camera, host=host,
            index=index,
        )

    # ------------------------------------------------------------------
    # ptz_*
    # ------------------------------------------------------------------

    def ptz_move(
        self,
        direction: str,
        speed: int | None = None,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Move PTZ camera."""
        self._run_no_output("ptz", "move", camera=camera, host=host,
                            direction=direction, speed=speed)

    def ptz_stop(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Stop PTZ movement."""
        self._run_no_output("ptz", "stop", camera=camera, host=host)

    def ptz_preset_list(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> list[PTZPreset]:
        """List PTZ presets."""
        raw = self._run_json("ptz", "preset", "list", camera=camera, host=host,
                             expect_list=True)
        return self._parse_ptz_presets(raw)

    def ptz_preset_save(
        self,
        name: str,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Save current position as a preset."""
        self._run_no_output("ptz", "preset", "save", camera=camera, host=host,
                            name=name)

    def ptz_preset_go(
        self,
        name: str,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Go to a preset position."""
        self._run_no_output("ptz", "preset", "go", camera=camera, host=host,
                            name=name)

    def ptz_preset_delete(
        self,
        name: str,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Delete a preset."""
        self._run_no_output("ptz", "preset", "delete", camera=camera, host=host,
                            name=name)

    def ptz_zoom_get(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> dict[str, Any]:
        """Get current zoom position."""
        raw = self._run_json("ptz", "zoom", "get", camera=camera, host=host)
        return raw if isinstance(raw, dict) else {}

    def ptz_zoom_set(
        self,
        position: int,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Set zoom position."""
        self._run_no_output("ptz", "zoom", "set", camera=camera, host=host,
                            position=position)

    def ptz_focus_get(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> dict[str, Any]:
        """Get focus position."""
        raw = self._run_json("ptz", "focus", "get", camera=camera, host=host)
        return raw if isinstance(raw, dict) else {}

    def ptz_focus_set(
        self,
        position: int | None = None,
        autofocus: bool | None = None,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Set focus position or enable autofocus."""
        self._run_no_output("ptz", "focus", "set", camera=camera, host=host,
                            position=position, autofocus=autofocus)

    def ptz_patrol_list(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> list[PTZPatrol]:
        """List PTZ patrol routes."""
        raw = self._run_json("ptz", "patrol", "list", camera=camera, host=host,
                             expect_list=True)
        return self._parse_ptz_patrols(raw)

    def ptz_patrol_control(
        self,
        action: str,
        name: str | None = None,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Start/stop a patrol route."""
        self._run_no_output("ptz", "patrol", action, camera=camera, host=host,
                            name=name)

    def ptz_guard_get(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> PTZGuard:
        """Get PTZ guard position."""
        raw = self._run_json("ptz", "guard", "get", camera=camera, host=host)
        return self._parse_ptz_guard(raw)

    def ptz_guard_set(
        self,
        enabled: bool = True,
        preset_index: int | None = None,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Set PTZ guard position."""
        self._run_no_output("ptz", "guard", "set", camera=camera, host=host,
                            enabled=enabled, preset=preset_index)

    def ptz_autotrack_get(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> PTZAutotrack:
        """Get AI auto-tracking state."""
        raw = self._run_json("ptz", "autotrack", "get", camera=camera, host=host)
        return self._parse_ptz_autotrack(raw)

    def ptz_autotrack_set(
        self,
        enabled: bool,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Enable/disable AI auto-tracking."""
        self._run_no_output("ptz", "autotrack", "set", camera=camera, host=host,
                            enabled=enabled)

    # ------------------------------------------------------------------
    # users_*
    # ------------------------------------------------------------------

    def users_list(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> list[UserEntry]:
        """List device users."""
        raw = self._run_json("users", "list", camera=camera, host=host,
                             expect_list=True)
        return self._parse_user_list(raw)

    def users_add(
        self,
        username: str,
        password: str,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Add a new device user."""
        self._run_no_output("users", "add", camera=camera, host=host,
                            username=username, password=password)

    def users_remove(
        self,
        username: str,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Remove a device user."""
        self._run_no_output("users", "remove", camera=camera, host=host,
                            username=username)

    def users_change_password(
        self,
        username: str,
        new_password: str,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Change a user's password."""
        self._run_no_output("users", "passwd", camera=camera, host=host,
                            username=username, password=new_password)

    # ------------------------------------------------------------------
    # osd_*
    # ------------------------------------------------------------------

    def osd_get(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> OsdConfig:
        """Get OSD settings."""
        raw = self._run_json("osd", "get", camera=camera, host=host)
        return self._parse_osd_config(raw)

    def osd_set(
        self,
        camera: str | None = None,
        host: str | None = None,
        channel_name: str | None = None,
        time_overlay: bool | None = None,
        logo_overlay: bool | None = None,
        **extra: Any,
    ) -> None:
        """Set OSD settings."""
        self._run_no_output("osd", "set", camera=camera, host=host,
                            channel_name=channel_name,
                            time_overlay=time_overlay,
                            logo_overlay=logo_overlay,
                            **extra)

    # ------------------------------------------------------------------
    # record_*
    # ------------------------------------------------------------------

    def record_get(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> RecordSchedule:
        """Get recording schedule."""
        raw = self._run_json("record", "schedule", "get", camera=camera, host=host)
        return self._parse_record_schedule(raw)

    def record_set(
        self,
        camera: str | None = None,
        host: str | None = None,
        continuous: bool | None = None,
        motion_detect: bool | None = None,
        ai_detect: bool | None = None,
        pre_record_sec: int | None = None,
        post_record_sec: int | None = None,
        **extra: Any,
    ) -> None:
        """Set recording schedule."""
        self._run_no_output("record", "schedule", "set", camera=camera, host=host,
                            continuous=continuous,
                            motion_detect=motion_detect,
                            ai_detect=ai_detect,
                            pre_record_sec=pre_record_sec,
                            post_record_sec=post_record_sec,
                            **extra)

    # ------------------------------------------------------------------
    # events_*
    # ------------------------------------------------------------------

    def events_recent(
        self,
        limit: int = 10,
        camera: str | None = None,
        host: str | None = None,
    ) -> list[EventEntry]:
        """Get recent events from the gateway buffer."""
        raw = self._run_json("events", "query", camera=camera, host=host,
                             limit=limit, expect_list=True)
        return self._parse_event_list(raw)

    def events_stream(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> subprocess.Popen:
        """Stream real-time events as NDJSON.

        Returns a Popen object. Caller should iterate lines and close.
        """
        args = self._build_args("events", "stream", camera=camera, host=host)
        return subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    # ------------------------------------------------------------------
    # vod_*
    # ------------------------------------------------------------------

    def vod_search(
        self,
        start_time: str,
        end_time: str,
        camera: str | None = None,
        host: str | None = None,
    ) -> list[VodEntry]:
        """Search recordings within a time range."""
        raw = self._run_json("vod", "search", camera=camera, host=host,
                             start_time=start_time, end_time=end_time,
                             expect_list=True)
        return self._parse_vod_list(raw)

    def vod_download(
        self,
        file_name: str,
        output_path: str,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Download a recording file."""
        self._run_no_output("vod", "download", camera=camera, host=host,
                            file=file_name, file_path=output_path)

    # ------------------------------------------------------------------
    # snapshot
    # ------------------------------------------------------------------

    def snapshot(
        self,
        output_path: str | None = None,
        stream: str = "sub",
        camera: str | None = None,
        host: str | None = None,
    ) -> bytes | None:
        """Capture a JPEG snapshot.

        If output_path is None, returns raw bytes.
        If output_path is set, writes to file and returns None.
        """
        args = self._build_args("snapshot", camera=camera, host=host)
        if output_path:
            args += ["-o", output_path]
        else:
            args += ["--output", "text"]
        args += ["--stream", stream]

        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=self._timeout,
            check=False,
        )
        if proc.returncode != 0:
            stderr_text = proc.stderr.decode(errors="replace").lower()
            if any(kw in stderr_text for kw in ("auth", "401", "unauthorized")):
                raise ReolinkAuthError(
                    proc.stderr.decode(errors="replace").strip()
                )
            raise ReolinkCommandError(
                proc.returncode,
                proc.stdout.decode(errors="replace"),
                proc.stderr.decode(errors="replace"),
                args,
            )
        if output_path:
            return None
        return proc.stdout

    # ------------------------------------------------------------------
    # info / capabilities / ping / device_list / login
    # ------------------------------------------------------------------

    def info(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> DeviceInfo:
        """Get device model, firmware, serial."""
        raw = self._run_json("info", camera=camera, host=host)
        return self._parse_device_info(raw)

    def capabilities(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> CapabilitiesResult:
        """Get device capabilities."""
        raw = self._run_json("capabilities", camera=camera, host=host)
        return self._parse_capabilities(raw)

    def ping(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> PingResult:
        """Check device connectivity."""
        raw = self._run_json("ping", camera=camera, host=host)
        return self._parse_ping_result(raw)

    def device_list(
        self,
    ) -> list[DeviceListEntry]:
        """List registered camera aliases."""
        raw = self._run_json("device", "list", expect_list=True)
        return self._parse_device_list(raw)

    def login(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> DeviceInfo:
        """Login and print device info (same dataclass as info)."""
        raw = self._run_json("login", camera=camera, host=host)
        return self._parse_device_info(raw)

    def system_reboot(
        self,
        camera: str | None = None,
        host: str | None = None,
    ) -> None:
        """Reboot the device."""
        self._run_no_output("system", "reboot", camera=camera, host=host)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_detection_config(raw: JsonValue) -> DetectionConfig:
        d = _to_dict(raw)
        if not d:
            return DetectionConfig()
        return DetectionConfig(
            enabled=bool(d.get("enable") or d.get("enabled")),
            sensitivity=int(d.get("sensitivity", 50)),
            person_stay_time=int(d.get("person_stay_time", 0)),
            vehicle_stay_time=int(d.get("vehicle_stay_time", 0)),
            dog_cat_stay_time=int(d.get("dog_cat_stay_time", 0)),
            motion_area=d.get("motion_area", ""),
            alarm_type=d.get("alarm_type", ""),
            delay_time=int(d.get("delay_time", 0)),
            raw=d,
        )

    @staticmethod
    def _parse_encode_config(raw: JsonValue) -> EncodeConfig:
        d = _to_dict(raw)
        if not d:
            return EncodeConfig()
        return EncodeConfig(
            resolution=str(d.get("resolution", "")),
            frame_rate=int(d.get("frame_rate", 0)),
            bit_rate=int(d.get("bit_rate", 0)),
            codec=str(d.get("codec", "")),
            max_bit_rate=int(d.get("max_bit_rate", 0)),
            min_bit_rate=int(d.get("min_bit_rate", 0)),
            raw=d,
        )

    @staticmethod
    def _parse_audio_config(raw: JsonValue) -> AudioConfig:
        d = _to_dict(raw)
        if not d:
            return AudioConfig()
        return AudioConfig(
            volume=int(d.get("volume", 50)),
            alarm_mute=bool(d.get("alarm_mute")),
            siren_enabled=bool(d.get("siren_enabled")),
            quick_replies=d.get("quick_replies", []),
            raw=d,
        )

    @staticmethod
    def _parse_light_config(raw: JsonValue) -> LightConfig:
        d = _to_dict(raw)
        if not d:
            return LightConfig()
        return LightConfig(
            ir_enabled=bool(d.get("enabled") or d.get("ir_enabled")),
            status_led_enabled=bool(d.get("status_led_enabled")),
            spotlight_enabled=bool(d.get("spotlight_enabled")),
            whiteled_alarm=bool(d.get("whiteled_alarm")),
            raw=d,
        )

    @staticmethod
    def _parse_image_config(raw: JsonValue) -> ImageConfig:
        d = _to_dict(raw)
        if not d:
            return ImageConfig()
        return ImageConfig(
            flip=bool(d.get("flip")),
            mirror=bool(d.get("mirror")),
            brightness=int(d.get("brightness", 50)),
            contrast=int(d.get("contrast", 50)),
            saturation=int(d.get("saturation", 50)),
            hue=int(d.get("hue", 50)),
            sharpness=int(d.get("sharpness", 50)),
            raw=d,
        )

    @staticmethod
    def _parse_privacy_masks(raw: JsonValue) -> list[PrivacyMask]:
        entries = _to_list(raw)
        masks = []
        for entry in entries:
            if isinstance(entry, dict):
                masks.append(PrivacyMask(
                    x=int(entry.get("x", 0)),
                    y=int(entry.get("y", 0)),
                    width=int(entry.get("width", 100)),
                    height=int(entry.get("height", 100)),
                    enabled=bool(entry.get("enabled")),
                ))
        return masks

    @staticmethod
    def _parse_user_list(raw: JsonValue) -> list[UserEntry]:
        entries = _to_list(raw)
        users = []
        for entry in entries:
            if isinstance(entry, dict):
                users.append(UserEntry(
                    username=str(entry.get("username", "")),
                    user_type=str(entry.get("user_type", "admin")),
                    online=bool(entry.get("online")),
                    raw=entry,
                ))
        return users

    @staticmethod
    def _parse_osd_config(raw: JsonValue) -> OsdConfig:
        d = _to_dict(raw)
        if not d:
            return OsdConfig()
        return OsdConfig(
            channel_name=str(d.get("channel_name", "")),
            time_overlay=bool(d.get("time_overlay", True)),
            logo_overlay=bool(d.get("logo_overlay", False)),
            raw=d,
        )

    @staticmethod
    def _parse_record_schedule(raw: JsonValue) -> RecordSchedule:
        d = _to_dict(raw)
        if not d:
            return RecordSchedule()
        return RecordSchedule(
            continuous=bool(d.get("continuous", True)),
            motion_detect=bool(d.get("motion_detect")),
            ai_detect=bool(d.get("ai_detect")),
            pre_record_sec=int(d.get("pre_record_sec", 5)),
            post_record_sec=int(d.get("post_record_sec", 5)),
            raw=d,
        )

    @staticmethod
    def _parse_event_list(raw: JsonValue) -> list[EventEntry]:
        entries = _to_list(raw)
        events = []
        for entry in entries:
            if isinstance(entry, dict):
                events.append(EventEntry(
                    event_id=str(entry.get("id", entry.get("event_id", ""))),
                    event_type=str(entry.get("type", entry.get("event_type", ""))),
                    channel=int(entry.get("channel", 0)),
                    start_time=str(entry.get("start_time", "")),
                    end_time=str(entry.get("end_time", "")),
                    thumbnail=str(entry.get("thumbnail", "")),
                    raw=entry,
                ))
        return events

    @staticmethod
    def _parse_vod_list(raw: JsonValue) -> list[VodEntry]:
        entries = _to_list(raw)
        result = []
        for entry in entries:
            if isinstance(entry, dict):
                result.append(VodEntry(
                    file_name=str(entry.get("file_name", "")),
                    start_time=str(entry.get("start_time", "")),
                    end_time=str(entry.get("end_time", "")),
                    channel=int(entry.get("channel", 0)),
                    event_type=str(entry.get("event_type", "")),
                    raw=entry,
                ))
        return result

    @staticmethod
    def _parse_device_info(raw: JsonValue) -> DeviceInfo:
        d = _to_dict(raw)
        if not d:
            return DeviceInfo()
        return DeviceInfo(
            model=str(d.get("model", "")),
            firmware=str(d.get("firmware", "")),
            serial=str(d.get("serial", "")),
            online=bool(d.get("online")),
            resolution=str(d.get("resolution", "")),
            channels=int(d.get("channels", 0)),
            raw=d,
        )

    @staticmethod
    def _parse_capabilities(raw: JsonValue) -> CapabilitiesResult:
        d = _to_dict(raw)
        if not d:
            return CapabilitiesResult()
        features = d.get("features", d.get("feature", []))
        if isinstance(features, str):
            features = [features]
        return CapabilitiesResult(
            features=[str(f) for f in features],
            ptz_supported=bool(d.get("ptz")),
            ai_detection=bool(d.get("ai_detection")),
            motion_detection=bool(d.get("motion_detection")),
            two_way_audio=bool(d.get("audio")),
            spotlight=bool(d.get("spotlight")),
            raw=d,
        )

    @staticmethod
    def _parse_ping_result(raw: JsonValue) -> PingResult:
        d = _to_dict(raw)
        if not d:
            return PingResult()
        return PingResult(
            reachable=bool(d.get("reachable")),
            rtt_ms=float(d.get("rtt_ms", 0)),
            raw=d,
        )

    @staticmethod
    def _parse_device_list(raw: JsonValue) -> list[DeviceListEntry]:
        entries = _to_list(raw)
        result = []
        for entry in entries:
            if isinstance(entry, dict):
                result.append(DeviceListEntry(
                    alias=str(entry.get("alias", "")),
                    host=str(entry.get("host", "")),
                    channel=int(entry.get("channel", 0)),
                    model=str(entry.get("model", "")),
                    serial=str(entry.get("serial", "")),
                    online=bool(entry.get("online")),
                    raw=entry,
                ))
        return result

    @staticmethod
    def _parse_ptz_presets(raw: JsonValue) -> list[PTZPreset]:
        entries = _to_list(raw)
        presets = []
        for entry in entries:
            if isinstance(entry, dict):
                presets.append(PTZPreset(
                    name=str(entry.get("name", "")),
                    index=int(entry.get("index", 0)),
                ))
        return presets

    @staticmethod
    def _parse_ptz_patrols(raw: JsonValue) -> list[PTZPatrol]:
        entries = _to_list(raw)
        patrols = []
        for entry in entries:
            if isinstance(entry, dict):
                presets = entry.get("presets", [])
                if isinstance(presets, str):
                    presets = [presets]
                patrols.append(PTZPatrol(
                    name=str(entry.get("name", "")),
                    presets=[str(p) for p in presets],
                ))
        return patrols

    @staticmethod
    def _parse_ptz_autotrack(raw: JsonValue) -> PTZAutotrack:
        d = _to_dict(raw)
        if not d:
            return PTZAutotrack()
        return PTZAutotrack(
            enabled=bool(d.get("enabled")),
            target_type=str(d.get("target_type", "")),
        )

    @staticmethod
    def _parse_ptz_guard(raw: JsonValue) -> PTZGuard:
        d = _to_dict(raw)
        if not d:
            return PTZGuard()
        return PTZGuard(
            enabled=bool(d.get("enabled")),
            preset_index=int(d.get("preset_index", d.get("preset", 0))),
        )
