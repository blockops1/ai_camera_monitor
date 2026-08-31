"""
Probe script: verify Phase.124 — Telegram outbound audit is now
emitted by the centralized transport (infra.send_telegram), NOT
required from each caller.

Tests 3 invariants:
1. Every send function in send_telegram emits exactly one
   OUTBOUND_TELEGRAM log line per call (success + failure paths).
2. The audit entry has alert_id/channel/event from the new kwargs.
3. image_paths is recorded when a frame is sent.
"""
import io
import logging

# Ensure repo root is importable
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, '<install-path>/ai_camera_monitor')
os.chdir('<install-path>/ai_camera_monitor')

from infra.send_telegram import (
    send_message,
    send_photo,
    send_photo_with_caption,
)

# Capture audit log lines
audit_lines: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record):
        if 'OUTBOUND_TELEGRAM' in record.getMessage():
            audit_lines.append(record.getMessage())


cap = _Capture()
audit_log = logging.getLogger('audit_telegram')
audit_log.addHandler(cap)
audit_log.setLevel(logging.INFO)


def reset():
    audit_lines.clear()


def _resp(ok=True, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {"ok": ok} if ok else {"ok": False, "description": "x"}
    r.return_value = r
    return r


def test_1_send_message_emits_audit_on_success():
    reset()
    with patch('infra.send_telegram.httpx.post', return_value=_resp(ok=True)):
        result = send_message(
            "tok", "123", "hello",
            alert_id="A1", channel="test_ch", event="test_evt",
        )
    assert result is True
    assert len(audit_lines) == 1, f"expected 1 audit line, got {len(audit_lines)}: {audit_lines}"
    line = audit_lines[0]
    assert 'alert_id=A1' in line, f"missing alert_id: {line}"
    assert 'channel=test_ch' in line, f"missing channel: {line}"
    assert 'event=test_evt' in line, f"missing event: {line}"
    assert "sent=True" in line, f"missing sent=True: {line}"
    assert "body='hello'" in line or "'hello'" in line, f"missing body: {line}"
    print("[OK] test_1: send_message success → 1 audit line with all fields")


def test_2_send_message_emits_audit_on_failure():
    reset()
    with patch('infra.send_telegram.httpx.post', return_value=_resp(ok=False, status=400)):
        result = send_message(
            "tok", "123", "hello",
            alert_id="A2", channel="test_ch", event="test_evt",
        )
    assert result is False
    assert len(audit_lines) == 1, f"expected 1 audit line on failure, got {len(audit_lines)}"
    assert "sent=False" in audit_lines[0]
    print("[OK] test_2: send_message failure → 1 audit line with sent=False")


def test_3_send_photo_records_image_path():
    reset()
    with patch('infra.send_telegram.httpx.post', return_value=_resp(ok=True)), \
         patch('builtins.open', MagicMock()):
        # Need to also patch os.path.isfile if applicable, but send_photo
        # uses open() directly. For the probe, just mock the file open.
        open_mock = MagicMock(return_value=io.BytesIO(b"\xff\xd8\xff\xe0fake"))
        with patch('builtins.open', open_mock):
            result = send_photo(
                "tok", "123", "/tmp/x.jpg",
                alert_id="A3", channel="test_ch", event="test_evt",
            )
    assert result is True
    assert len(audit_lines) == 1
    assert 'image_paths=[/tmp/x.jpg]' in audit_lines[0], \
        f"missing image_paths: {audit_lines[0]}"
    print("[OK] test_3: send_photo → audit entry records image_paths=[/tmp/x.jpg]")


def test_4_send_photo_with_caption_emits_one_audit_per_call():
    reset()
    # Mock both sendPhoto (photo leg) and sendMessage (fallback not used here)
    with patch('infra.send_telegram.httpx.post', return_value=_resp(ok=True)), \
         patch('builtins.open', MagicMock(return_value=io.BytesIO(b"\xff\xd8\xff\xe0fake"))):
        result = send_photo_with_caption(
            "tok", "123", "/tmp/x.jpg", "short caption",
            alert_id="A4", channel="test_ch", event="test_evt",
        )
    assert result is True
    # Critical: exactly ONE audit line per call (not duplicated by the
    # internal send_message fallback — that path wasn't taken here).
    assert len(audit_lines) == 1, \
        f"expected 1 audit line, got {len(audit_lines)}: {audit_lines}"
    assert "sent=True" in audit_lines[0]
    print("[OK] test_4: send_photo_with_caption → exactly 1 audit line")


def test_5_missing_kwargs_raises_typeerror():
    """Phase.124 invariant: alert_id/channel/event are keyword-only required.
    Forgetting them is a bug — we want a fast failure, not silent empty audit."""
    reset()
    try:
        # missing alert_id
        send_message("tok", "123", "hello")  # type: ignore[call-arg]
        raise AssertionError("should have raised TypeError")
    except TypeError as e:
        assert 'alert_id' in str(e), f"unexpected TypeError: {e}"
        print("[OK] test_5: send_message without alert_id/channel/event → TypeError")


def test_6_no_audit_if_audit_telegram_raises():
    """Invariant: the send path never breaks because of an audit failure.
    _audit() wraps log_outbound_telegram in try/except — so if the
    underlying logger raises, the send still completes."""
    reset()
    with patch('infra.send_telegram.httpx.post', return_value=_resp(ok=True)), \
         patch('infra.send_telegram.log_outbound_telegram',
               side_effect=RuntimeError("audit blew up")):
        result = send_message(
            "tok", "123", "hello",
            alert_id="A6", channel="test_ch", event="test_evt",
        )
    assert result is True, "send path was broken by audit failure"
    print("[OK] test_6: audit failure does NOT break the send path")


if __name__ == "__main__":
    tests = [test_1_send_message_emits_audit_on_success,
             test_2_send_message_emits_audit_on_failure,
             test_3_send_photo_records_image_path,
             test_4_send_photo_with_caption_emits_one_audit_per_call,
             test_5_missing_kwargs_raises_typeerror,
             test_6_no_audit_if_audit_telegram_raises]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"[FAIL] {t.__name__}: {e!r}")
            failed += 1
    if failed:
        print(f"\n{failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    print(f"\n{len(tests)}/{len(tests)} tests passed — Phase.124 audit invariant verified")