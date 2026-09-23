"""test_daemon_mcp_spawn_disabled -- Verify MCP server does NOT spawn when disabled.

AC5: Same setup as test_daemon_mcp_spawn but FARMSURV_MCP_ENABLED=0.
After 5s, :8091 is closed (connection refused). Listener logs include
mcp_server_disabled.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

_WORKSPACE = os.path.join(os.path.dirname(__file__), "..")
_LOG_FILE = os.path.join(_WORKSPACE, "logs", "daemon.log")


def _build_disabled_env():
    """Build an env dict with MCP disabled."""
    env = os.environ.copy()
    env["FARMSURV_MCP_ENABLED"] = "0"
    env["FARMSURV_BOX_ID"] = "test-box-disabled"
    env["LISTEN_HOST"] = "127.0.0.1"
    env["LISTEN_PORT"] = "0"
    # Prevent camera/telegram creds from being needed
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("TELEGRAM_HOME_CHAT_ID", None)
    env.pop("VISION_LLM_URL", None)
    return env


class TestDaemonMCPDisabled:
    """AC5: MCP server not reachable when FARMSURV_MCP_ENABLED=0."""

    def test_mcp_disabled_no_listener(self):
        """MCP server is not listening when FARMSURV_MCP_ENABLED=0."""
        proc = subprocess.Popen(
            [sys.executable, "-m", "listener.daemon"],
            cwd=_WORKSPACE,
            env=_build_disabled_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            # Wait for the daemon to fully start (RTSP readers + cleanup)
            time.sleep(6)

            # Port 8091 should NOT be listening
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex(("127.0.0.1", 8091))
            sock.close()
            assert result != 0, (
                "Port 8091 is open but MCP should be disabled "
                "(FARMSURV_MCP_ENABLED=0)"
            )

            # Check logs include mcp_server_disabled
            proc.terminate()
            proc.wait(timeout=5)

            # The daemon's logging goes to logs/daemon.log via RotatingFileHandler,
            # not stdout. Read the log file for the disabled message.
            if os.path.exists(_LOG_FILE):
                with open(_LOG_FILE, "r") as f:
                    log_output = f.read()
            else:
                log_output = ""

            assert "mcp_server_disabled" in log_output, (
                f"Expected 'mcp_server_disabled' in daemon log. "
                f"Log contents:\n{log_output[:1000]}"
            )
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
