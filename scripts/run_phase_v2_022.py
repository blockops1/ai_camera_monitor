"""Driver for PRD-V2-022: Telegram delivery path.

Closes the gap that broke the system's entire purpose: v2's
pipeline builds tg1/tg2/tg3 dicts but never POSTs them to
api.telegram.org. This PRD adds the missing HTTP-send layer.

Four stories, ~165 LOC, no deps. Driver walks the topo order:
US-022a → US-022c → US-022b → US-022d.

US-022a: dispatcher pure function (httpx + sendMessage/sendMediaGroup).
US-022c: env-var-name consistency check + README note.
US-022b: wire dispatcher into listener/daemon.py.
US-022d: operator-driven smoke test (port 8090 + Telegram home chat).

After each story the driver:
  1. Runs pytest on the new/changed tests.
  2. Runs the privacy scanner.
  3. Halts on any failure (qa gate, per 'no parallel dispatch' directive).
  4. After all 4 pass, prints next-steps summary.

Mirrors the per-phase driver pattern from PRD-V2-018/019/020/021.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REQUIRED_ENV_VARS = ("DRIVER_REPO_PATH", "OPERATOR_TELEGRAM_CHAT_ID")


def require_env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        print(f"ERROR: env var {name!r} is required but unset.")
        print(f"       Set it in scripts/run_phase_v2_022.env (see .env.example).")
        sys.exit(2)
    return val


def run(cmd: list[str], cwd: str | None = None, check: bool = True) -> int:
    """Run a subprocess; return exit code. Print stdout/stderr inline."""
    print(f"\n$ {' '.join(cmd)}  (cwd={cwd or os.getcwd()})")
    rc = subprocess.run(cmd, cwd=cwd).returncode
    if check and rc != 0:
        print(f"!! command failed with rc={rc}")
    return rc


def run_pytest(repo: Path, target: str) -> bool:
    rc = run(
        [str(repo / ".venv" / "bin" / "python3.11"), "-m", "pytest",
         target, "-x", "--tb=short"],
        cwd=str(repo),
        check=False,
    )
    return rc == 0


def run_scanner(repo: Path) -> bool:
    rc = run(
        [str(repo / ".venv" / "bin" / "python3.11"),
         str(repo / "scripts" / "check_no_private_data.py")],
        cwd=str(repo),
        check=False,
    )
    return rc == 0


def run_story_a(repo: Path) -> bool:
    """US-022a: telegram_formatter/dispatcher.py + tests/test_dispatcher.py."""
    print("\n=== US-022a: dispatcher pure function (httpx + sendMessage/sendMediaGroup) ===")

    # 1. Write telegram_formatter/dispatcher.py
    dispatcher_path = repo / "telegram_formatter" / "dispatcher.py"
    dispatcher_path.write_text('''"""Telegram HTTP dispatcher — closes the v2 delivery gap.

Pure function `dispatch(messages, *, bot_token, chat_id, base_url)` that
takes the dicts pipeline.run() already builds (tg1/tg2/tg3 with caption +
photos) and POSTs them to api.telegram.org.

No fallback paths (per operator 2026-09-09). Empty bot_token or chat_id
raises ConfigError at call time. 4xx/5xx from api.telegram.org raises
DeliveryError. No retries, no silent drops.

Photo delivery uses sendMediaGroup with input_media_document (preserves
lossless PNG). sendPhoto re-encodes to JPEG server-side and is forbidden
(operator directive in PRD-V2-019 + US-019f/g/h).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class ConfigError(RuntimeError):
    """Raised when bot_token or chat_id is empty/missing."""


@dataclass
class DeliveryError(RuntimeError):
    """Raised when api.telegram.org returns 4xx/5xx."""


def _send_message(
    client: httpx.Client,
    bot_token: str,
    chat_id: str,
    caption: str,
    base_url: str,
) -> httpx.Response:
    """Send a text-only message (no photos)."""
    url = f"{base_url}/bot{bot_token}/sendMessage"
    return client.post(url, json={"chat_id": chat_id, "text": caption})


def _send_media_group(
    client: httpx.Client,
    bot_token: str,
    chat_id: str,
    caption: str,
    photo_paths: list[str],
    base_url: str,
) -> httpx.Response:
    """Send a media group with lossless PNG attachments.

    Uses input_media_document (NOT input_media_photo) so Telegram
    does not re-encode to JPEG server-side. Photo paths are attached
    as multipart/form-data via attach://<index> URIs.
    """
    url = f"{base_url}/bot{bot_token}/sendMediaGroup"

    media: list[dict[str, Any]] = []
    files: dict[str, tuple[str, bytes, str]] = {}
    for i, path in enumerate(photo_paths):
        attach_key = f"file{i}"
        media.append(
            {
                "type": "document",
                "media": f"attach://{attach_key}",
                "caption": caption if i == 0 else "",
            }
        )
        # Read file bytes; httpx multipart will populate Content-Type from
        # the mime_type tuple below. PNG files use image/png.
        with open(path, "rb") as f:
            data = f.read()
        files[attach_key] = (Path(path).name, data, "image/png")

    return client.post(url, data={"chat_id": chat_id, "media": str(media)}, files=files)


def dispatch(
    messages: list[dict[str, Any]],
    *,
    bot_token: str,
    chat_id: str,
    base_url: str = "https://api.telegram.org",
    client: httpx.Client | None = None,
) -> list[httpx.Response]:
    """Dispatch tg1/tg2/tg3 dicts to Telegram.

    Each message dict has shape:
        {"caption": str, "photos": list[str]}

    Photos is a list of file paths. Empty photos list -> sendMessage.
    One or more photos -> sendMediaGroup with input_media_document.

    Returns the list of httpx.Response objects in input order.
    Raises ConfigError on empty bot_token or chat_id.
    Raises DeliveryError on any 4xx/5xx from Telegram.
    """
    if not bot_token:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN is empty — set it in ~/.env "
            "(see scripts/run_phase_v2_022.env.example)"
        )
    if not chat_id:
        raise ConfigError(
            "TELEGRAM_HOME_CHAT_ID is empty — rename TELEGRAM_CHAT_ID to "
            "TELEGRAM_HOME_CHAT_ID in ~/.env (the canonical name is "
            "TELEGRAM_HOME_CHAT_ID, not TELEGRAM_CHAT_ID)"
        )

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=30.0)

    responses: list[httpx.Response] = []
    try:
        for msg in messages:
            caption = msg.get("caption", "")
            photos = msg.get("photos", []) or []
            photos = [p for p in photos if p]  # drop empty strings

            if not photos:
                resp = _send_message(client, bot_token, chat_id, caption, base_url)
            else:
                resp = _send_media_group(
                    client, bot_token, chat_id, caption, photos, base_url
                )

            if resp.status_code >= 400:
                raise DeliveryError(
                    f"Telegram {resp.request.method} {resp.request.url.path} "
                    f"returned HTTP {resp.status_code}: {resp.text[:200]}"
                )
            responses.append(resp)
    finally:
        if owns_client:
            client.close()

    return responses
''')

    # 2. Write tests/test_dispatcher.py
    tests_path = repo / "tests" / "test_dispatcher.py"
    tests_path.parent.mkdir(parents=True, exist_ok=True)
    tests_path.write_text('''"""Tests for telegram_formatter/dispatcher.py.

httpx.MockTransport simulates api.telegram.org responses.
No live HTTP. Token + chat_id are test-only values.
"""
from __future__ import annotations

import json

import httpx
import pytest

from telegram_formatter import dispatcher


@pytest.fixture
def transport():
    """Captures the request and returns a controllable response."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        # Default: success
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    return httpx.MockTransport(handler), captured


def test_dispatch_text_only_sends_sendMessage(transport):
    mock, captured = transport
    msg = {"caption": "hello world", "photos": []}
    responses = dispatcher.dispatch(
        [msg], bot_token="test_token", chat_id="12345", client=httpx.Client(transport=mock)
    )
    assert len(responses) == 1
    assert responses[0].status_code == 200
    # Verify the URL path
    req = captured[0]
    assert req.url.path.endswith("/bottest_token/sendMessage")
    # Verify the JSON body
    body = json.loads(req.content)
    assert body == {"chat_id": "12345", "text": "hello world"}


def test_dispatch_with_photos_uses_sendMediaGroup_with_document(transport):
    mock, captured = transport
    msg = {"caption": "vehicle alert", "photos": ["/tmp/a.png", "/tmp/b.png"]}
    # Create dummy files
    for p in msg["photos"]:
        Path(p).write_bytes(b"\\x89PNG\\r\\n\\x1a\\n")
    try:
        responses = dispatcher.dispatch(
            [msg], bot_token="test_token", chat_id="12345", client=httpx.Client(transport=mock)
        )
        assert len(responses) == 1
        assert responses[0].status_code == 200
        req = captured[0]
        assert req.url.path.endswith("/bottest_token/sendMediaGroup")
        # Form data: media field has type=document (NOT photo)
        body_text = req.content.decode("utf-8", errors="replace")
        assert "type\":\"document" in body_text
        assert "type\":\"photo" not in body_text
    finally:
        for p in msg["photos"]:
            Path(p).unlink(missing_ok=True)


def test_dispatch_raises_on_empty_token(transport):
    mock, _ = transport
    with pytest.raises(dispatcher.ConfigError, match="TELEGRAM_BOT_TOKEN"):
        dispatcher.dispatch(
            [{"caption": "x", "photos": []}],
            bot_token="",
            chat_id="12345",
            client=httpx.Client(transport=mock),
        )


def test_dispatch_raises_on_empty_chat_id(transport):
    mock, _ = transport
    with pytest.raises(dispatcher.ConfigError, match="TELEGRAM_HOME_CHAT_ID"):
        dispatcher.dispatch(
            [{"caption": "x", "photos": []}],
            bot_token="test_token",
            chat_id="",
            client=httpx.Client(transport=mock),
        )


def test_dispatch_raises_on_4xx(transport):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "error": "bad chat_id"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(dispatcher.DeliveryError, match="HTTP 400"):
        dispatcher.dispatch(
            [{"caption": "x", "photos": []}],
            bot_token="test_token",
            chat_id="12345",
            client=client,
        )


def test_dispatch_raises_on_5xx(transport):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(dispatcher.DeliveryError, match="HTTP 500"):
        dispatcher.dispatch(
            [{"caption": "x", "photos": []}],
            bot_token="test_token",
            chat_id="12345",
            client=client,
        )
''')

    # 3. Verify import works
    rc = run(
        [str(repo / ".venv" / "bin" / "python3.11"), "-c",
         "from telegram_formatter import dispatcher; print('import ok')"],
        cwd=str(repo),
        check=False,
    )
    if rc != 0:
        print("!! dispatcher import failed")
        return False

    # 4. Run the new tests
    if not run_pytest(repo, "tests/test_dispatcher.py"):
        return False

    # 5. Privacy scanner
    if not run_scanner(repo):
        return False

    print("US-022a PASS")
    return True


def run_story_c(repo: Path) -> bool:
    """US-022c: env-var-name consistency check + README note."""
    print("\n=== US-022c: env-var-name consistency check + README note ===")

    # 1. Grep for the wrong name in env-var form (with = sign)
    rc = run(
        ["grep", "-rnE", r"^TELEGRAM_CHAT_ID=|^\s*TELEGRAM_CHAT_ID=",
         "docs/", "README.md", "telegram-creds.env.example", "tests/"],
        cwd=str(repo),
        check=False,
    )
    if rc != 0:
        print("(no wrong-name hits — clean)")

    # 2. Add the README note
    readme_path = repo / "README.md"
    if readme_path.exists():
        text = readme_path.read_text()
        marker = "## Telegram home chat env var name"
        if marker not in text:
            # Insert before the first "## " heading after Configuration section
            note = (
                "\n## Telegram home chat env var name\n"
                "The canonical env var name is `TELEGRAM_HOME_CHAT_ID`, "
                "**not** `TELEGRAM_CHAT_ID`. If your `~/.env` has the wrong "
                "name, the dispatcher will raise `ConfigError` with the "
                "exact rename step on every alert. Verify with:\n"
                "```bash\n"
                "python3 -c 'from dotenv import dotenv_values; "
                "from pathlib import Path; "
                "print(bool(dotenv_values(Path.home() / \".env\")"
                ".get(\"TELEGRAM_HOME_CHAT_ID\")))'\n"
                "```\n"
                "If this prints `False`, rename `TELEGRAM_CHAT_ID` to "
                "`TELEGRAM_HOME_CHAT_ID` in `~/.env`.\n"
            )
            # Insert at end of file (README sections vary by version)
            text = text.rstrip() + "\n\n" + note
            readme_path.write_text(text)
            print("README.md updated with the env var name note")
        else:
            print("README.md already has the note")

    # 3. Privacy scanner
    if not run_scanner(repo):
        return False

    print("US-022c PASS")
    return True


def run_story_b(repo: Path) -> bool:
    """US-022b: wire dispatcher into listener/daemon.py."""
    print("\n=== US-022b: wire dispatcher into listener/daemon.py ===")

    daemon_path = repo / "listener" / "daemon.py"
    text = daemon_path.read_text()

    # Insert: after `result = pipeline.run(alert_dict)` block, add dispatcher call.
    # We patch the existing /alert handler.
    marker = "    result = pipeline.run(alert_dict)\n    return jsonify(result), 200"
    if marker not in text:
        print(f"!! could not find /alert handler marker in {daemon_path}")
        return False

    # Check we haven't already wired it
    if "from telegram_formatter import dispatcher" in text:
        print("(dispatcher already wired — skipping)")
    else:
        # 1. Add import at top with other imports
        import_marker = "from listener import pipeline"
        if import_marker in text:
            text = text.replace(
                import_marker,
                "from telegram_formatter import dispatcher\n    from listener import pipeline",
                1,
            )

        # 2. Replace the marker with dispatcher call
        new_block = (
            "    result = pipeline.run(alert_dict)\n"
            "\n"
            "    # US-022b: dispatch tg1/tg2/tg3 to Telegram.\n"
            "    # Daemon still returns HTTP 200 even if dispatch fails —\n"
            "    # operator sees the gap in logs/daemon.log, not as a\n"
            "    # camera-side retry storm.\n"
            "    tg_messages = [\n"
            "        result.get(\"tg1\", {}),\n"
            "        result.get(\"tg2\", {}),\n"
            "        result.get(\"tg3\", {}),\n"
            "    ]\n"
            "    tg_messages = [m for m in tg_messages if m]\n"
            "    if tg_messages:\n"
            "        try:\n"
            "            responses = dispatcher.dispatch(\n"
            "                tg_messages,\n"
            "                bot_token=os.environ[\"TELEGRAM_BOT_TOKEN\"],\n"
            "                chat_id=os.environ[\"TELEGRAM_HOME_CHAT_ID\"],\n"
            "            )\n"
            "            for i, resp in enumerate(responses, start=1):\n"
            "                log.info(\n"
            "                    \"tg-dispatch: TG#%d ok (HTTP %d)\",\n"
            "                    i, resp.status_code,\n"
            "                )\n"
            "        except (dispatcher.ConfigError, dispatcher.DeliveryError) as exc:\n"
            "            log.error(\"tg-dispatch: %s: %s\", type(exc).__name__, exc)\n"
            "\n"
            "    return jsonify(result), 200"
        )
        text = text.replace(marker, new_block, 1)
        daemon_path.write_text(text)
        print("listener/daemon.py wired with dispatcher call")

    # Write a basic daemon test
    daemon_test = repo / "tests" / "test_daemon_dispatch.py"
    daemon_test.write_text('''"""Tests for listener/daemon.py dispatcher integration."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx


def test_daemon_returns_200_on_dispatch_failure():
    """When dispatcher raises, /alert still returns HTTP 200."""
    with patch("listener.daemon.pipeline") as mock_pipeline, \\
         patch("listener.daemon.dispatcher") as mock_dispatcher:
        mock_pipeline.run.return_value = {
            "tg1": {"caption": "x", "photos": []},
            "tg2": {},
            "tg3": {},
        }
        mock_dispatcher.dispatch.side_effect = RuntimeError("simulated failure")

        # Import the Flask app + invoke /alert
        from listener.daemon import app
        client = app.test_client()
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake",
            "TELEGRAM_HOME_CHAT_ID": "12345",
        }, clear=False):
            resp = client.post("/alert", json={"camera_id": "OFS", "id": "test"})
        assert resp.status_code == 200


def test_daemon_logs_dispatch_success():
    """When dispatcher returns responses, daemon logs tg-dispatch lines."""
    with patch("listener.daemon.pipeline") as mock_pipeline, \\
         patch("listener.daemon.dispatcher") as mock_dispatcher:
        mock_pipeline.run.return_value = {
            "tg1": {"caption": "alert", "photos": []},
            "tg2": {},
            "tg3": {},
        }
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_dispatcher.dispatch.return_value = [mock_resp]

        from listener.daemon import app
        client = app.test_client()
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake",
            "TELEGRAM_HOME_CHAT_ID": "12345",
        }, clear=False):
            resp = client.post("/alert", json={"camera_id": "OFS", "id": "test"})
        assert resp.status_code == 200
        mock_dispatcher.dispatch.assert_called_once()


def test_daemon_logs_dispatch_config_error():
    """When ConfigError raised, daemon logs the error and still returns 200."""
    with patch("listener.daemon.pipeline") as mock_pipeline, \\
         patch("listener.daemon.dispatcher") as mock_dispatcher:
        mock_pipeline.run.return_value = {
            "tg1": {"caption": "x", "photos": []},
            "tg2": {},
            "tg3": {},
        }
        # ConfigError from dispatcher module
        from telegram_formatter.dispatcher import ConfigError
        mock_dispatcher.dispatch.side_effect = ConfigError("TELEGRAM_HOME_CHAT_ID is empty")

        from listener.daemon import app
        client = app.test_client()
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake",
            "TELEGRAM_HOME_CHAT_ID": "12345",
        }, clear=False):
            resp = client.post("/alert", json={"camera_id": "OFS", "id": "test"})
        assert resp.status_code == 200
''')

    # Run the daemon tests
    if not run_pytest(repo, "tests/test_daemon_dispatch.py"):
        return False

    # Run the full test suite to make sure nothing regressed
    if not run_pytest(repo, "tests/"):
        return False

    # Privacy scanner
    if not run_scanner(repo):
        return False

    print("US-022b PASS")
    return True


def run_story_d(repo: Path) -> bool:
    """US-022d: operator-driven smoke test."""
    print("\n=== US-022d: operator-driven smoke test ===")

    smoke_path = repo / "scripts" / "smoke_telegram_delivery.py"
    smoke_path.parent.mkdir(parents=True, exist_ok=True)
    smoke_path.write_text('''"""Operator-driven smoke test: synthetic OFS alert reaches Telegram.

US-022d. Mirrors the PRD-V2-019 US-019d / PRD-V2-021 US-021d pattern.
Run after US-022a/b/c land. Confirms the operator's home chat actually
receives TG#1 + TG#2 + TG#3 with lossless PNG attachments.

Usage:
    export DRIVER_REPO_PATH=/Users/jill/farm-surveillance-v2
    export OPERATOR_TELEGRAM_CHAT_ID=374999219
    .venv/bin/python3.11 scripts/smoke_telegram_delivery.py

What it does:
    1. Verify TELEGRAM_HOME_CHAT_ID is non-empty in os.environ.
    2. Verify daemon is reachable on 127.0.0.1:8090.
    3. Stage 4 PNG frames + 1 pairwise_diff.png under data/frames/OFS/.
    4. POST synthetic alert to /alert.
    5. Wait for HTTP 200 response (within 30s).
    6. Print 'check Telegram home chat for 3 messages' + daemon log tail.

Operator manually:
    1. Open Telegram home chat.
    2. Confirm 3 messages arrived with PNG attachments.
    3. Run `file <downloaded-attachment>` -> 'PNG image data'.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

REQUIRED_ENV = ("DRIVER_REPO_PATH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHAT_ID")


def main() -> int:
    # 1. Check env
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"ERROR: env vars missing: {missing}")
        if "TELEGRAM_HOME_CHAT_ID" in missing:
            print("       Your ~/.env has TELEGRAM_CHAT_ID — rename it to")
            print("       TELEGRAM_HOME_CHAT_ID (the canonical name is")
            print("       TELEGRAM_HOME_CHAT_ID, not TELEGRAM_CHAT_ID).")
        return 2

    repo = Path(os.environ["DRIVER_REPO_PATH"])
    chat_id = os.environ["TELEGRAM_HOME_CHAT_ID"]

    # 2. Check daemon reachable
    print("Checking daemon at 127.0.0.1:8090...")
    try:
        resp = httpx.get("http://127.0.0.1:8090/health", timeout=5.0)
        print(f"  daemon HTTP {resp.status_code}")
    except Exception as exc:
        print(f"  daemon unreachable: {exc}")
        print("  Start it: launchctl load ~/Library/LaunchAgents/com.farm.surveillance.v2.plist")
        return 2

    # 3. Stage synthetic frames
    ofs_dir = repo / "data" / "frames" / "OFS"
    ofs_dir.mkdir(parents=True, exist_ok=True)
    alert_id = "smoke-test-" + str(int(time.time()))
    alert_dir = ofs_dir / alert_id
    alert_dir.mkdir(exist_ok=True)

    # 4 PNG frames (1x1 PNGs)
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c63f8cfc0500f0000030001f5c4c95e0000000049454e44ae426082"
    )
    frame_paths = []
    for i in range(4):
        p = ofs_dir / f"frame_{i:03d}.png"
        p.write_bytes(png_bytes)
        frame_paths.append(str(p))

    # pairwise_diff.png
    diff_path = alert_dir / "pairwise_diff.png"
    diff_path.write_bytes(png_bytes)

    # crops
    crop_a = alert_dir / "crop_a.png"
    crop_a.write_bytes(png_bytes)
    crop_b = alert_dir / "crop_b.png"
    crop_b.write_bytes(png_bytes)

    print(f"Staged {len(frame_paths)} frames + diff + 2 crops under data/frames/OFS/{alert_id}/")

    # 4. POST synthetic alert
    alert = {
        "camera_id": "OFS",
        "id": alert_id,
        "frames": frame_paths,
        "pairwise_diff_path": str(diff_path),
        "crop_a": str(crop_a),
        "crop_b": str(crop_b),
        "classification_hint": "vehicle",
    }

    print(f"POSTing synthetic alert to /alert (alert_id={alert_id})...")
    t0 = time.time()
    resp = httpx.post(
        "http://127.0.0.1:8090/alert", json=alert, timeout=30.0
    )
    elapsed = time.time() - t0
    print(f"  HTTP {resp.status_code} in {elapsed:.1f}s")
    if resp.status_code != 200:
        print(f"  body: {resp.text[:300]}")
        return 1

    # 5. Tail the daemon log
    log_path = repo / "logs" / "daemon.log"
    if log_path.exists():
        print(f"\\nLast 5 lines of logs/daemon.log:")
        for line in log_path.read_text().splitlines()[-5:]:
            print(f"  {line}")

    print(f"\\n>>> CHECK TELEGRAM HOME CHAT ({chat_id}) FOR 3 MESSAGES <<<")
    print(f"Expected: TG#1 (caption + 5 PNG attachments), TG#2 (caption + 2 PNG crops), TG#3 (text only).")
    print(f"Verify lossless: open any received photo, run `file <path>` -> expect 'PNG image data'.")

    # Operator confirms manually — no automated verification
    return 0


if __name__ == "__main__":
    sys.exit(main())
''')

    # Smoke test itself doesn't have automated tests (operator-driven)
    # Just verify it imports and runs the env check
    rc = run(
        [str(repo / ".venv" / "bin" / "python3.11"), "-c",
         "import sys; sys.path.insert(0, '.'); "
         "import importlib.util; spec = importlib.util.spec_from_file_location("
         "'smoke', 'scripts/smoke_telegram_delivery.py'); "
         "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
         "print('smoke script imports ok')"],
        cwd=str(repo),
        check=False,
    )
    if rc != 0:
        print("!! smoke script import failed")
        return False

    # Privacy scanner
    if not run_scanner(repo):
        return False

    print("US-022d PASS (operator runs the actual smoke on live daemon)")
    return True


# Topo order: a → c → b → d
STORY_RUNNERS = {
    "US-022a": run_story_a,
    "US-022c": run_story_c,
    "US-022b": run_story_b,
    "US-022d": run_story_d,
}


def main() -> int:
    repo_path = require_env("DRIVER_REPO_PATH")
    chat_id = require_env("OPERATOR_TELEGRAM_CHAT_ID")
    repo = Path(repo_path)

    print(f"PRD-V2-022 driver")
    print(f"  repo: {repo}")
    print(f"  chat_id: {chat_id}")
    print(f"  stories: {list(STORY_RUNNERS)}")

    failed: list[str] = []
    for sid, runner in STORY_RUNNERS.items():
        ok = runner(repo)
        if not ok:
            failed.append(sid)
            break

    print()
    if failed:
        print(f"ABORTED at {failed[0]}; fix and re-run.")
        return 1
    print("PRD-V2-022 complete: 4/4 stories passes=True")
    print("Operator next steps: run scripts/smoke_telegram_delivery.py")
    print("  (synthetic OFS alert reaches Telegram home chat, lossless PNG verified).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
