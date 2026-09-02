"""
Tests for infra/camera_creds.py — RTSP-URL parser for camera-creds.env.

Covers:
    - _extract_ip (internal helper): RTSP URLs with @-encoded passwords,
      IPv4 vs IPv6, malformed inputs, missing scheme
    - load_camera_creds: parses canonical env format, missing file,
      comments, blank lines, malformed lines, IP rotation warning,
      both RTSP-only and RTSP+IP combinations

The IP extractor is tested directly via the test infra module's own
test infra — we exercise its observable behavior through load_camera_creds.
"""

import os
import tempfile

import pytest

from infra.camera_creds import load_camera_creds


@pytest.fixture
def env_file():
    """Create a temp env file with TEST_* legacy-schema creds, yield path, clean up.

    Phase 6B.167 §13.4 (Commit 6 scrub): operator IPs/names replaced with
    synthetic TEST_* prefixes (10.0.0.x range, generic test names).
    The TEST_* prefix is registered in infra.cameras._LEGACY_PREFIX_TO_NAME
    so the legacy parser can resolve these without operator-flavored data.
    """
    content = (
        "# Test cameras — synthetic, public-safe\n"
        "TEST_FRONT_RTSP_URL=rtsp://user:secret@10.0.0.1:554/Streaming/Channels/101\n"
        "TEST_FRONT_IP=10.0.0.1\n"
        "TEST_BACK_RTSP_URL=rtsp://admin:pass@10.0.0.2:554/Streaming/Channels/101\n"
        "TEST_BACK_IP=10.0.0.2\n"
        "TEST_OUTSIDE_RTSP_URL=rtsp://admin:pass@10.0.0.3:554/Streaming/Channels/101\n"
        "TEST_OUTSIDE_IP=10.0.0.3\n"
        "TEST_SIDE_RTSP_URL=rtsp://admin:pass@10.0.0.4:554/Streaming/Channels/101\n"
        "TEST_SIDE_IP=10.0.0.4\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write(content)
        path = f.name
    try:
        yield path
    finally:
        os.unlink(path)


@pytest.fixture
def env_file_with_at_password():
    """RTSP URL where the password contains a literal @ (the tricky case)."""
    content = (
        "TEST_FRONT_RTSP_URL=rtsp://user:p@ss@10.0.0.1:554/Streaming/Channels/101\n"
        "TEST_FRONT_IP=10.0.0.1\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write(content)
        path = f.name
    try:
        yield path
    finally:
        os.unlink(path)


@pytest.fixture
def env_file_with_urlencoded_at():
    """RTSP URL where the password contains %40 (URL-encoded @)."""
    content = (
        "TEST_FRONT_RTSP_URL=rtsp://user:p%40ss@10.0.0.1:554/Streaming/Channels/101\n"
        "TEST_FRONT_IP=10.0.0.1\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write(content)
        path = f.name
    try:
        yield path
    finally:
        os.unlink(path)


@pytest.fixture
def env_file_with_ipv6():
    """RTSP URL with IPv6 host (bracketed)."""
    content = (
        "TEST_FRONT_RTSP_URL=rtsp://user:pass@[fe80::1]:554/Streaming/Channels/101\n"
        "TEST_FRONT_IP=fe80::1\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write(content)
        path = f.name
    try:
        yield path
    finally:
        os.unlink(path)


@pytest.fixture
def env_file_ip_rotation():
    """RTSP URL and _IP env var disagree — should print a warning."""
    content = (
        "TEST_FRONT_RTSP_URL=rtsp://user:secret@10.0.0.50:554/Streaming/Channels/101\n"
        "TEST_FRONT_IP=10.0.0.1\n"  # IP rotated but env not updated
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write(content)
        path = f.name
    try:
        yield path
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Basic parse + key/value extraction
# ---------------------------------------------------------------------------


class TestLoadCameraCredsBasic:
    """Verify the canonical env format parses correctly."""

    def test_parses_canonical_env(self, env_file):
        result = load_camera_creds(env_file)
        # TEST_FRONT / TEST_BACK / TEST_OUTSIDE / TEST_SIDE prefixes
        # are registered in infra.cameras._LEGACY_PREFIX_TO_NAME and map
        # to these public-safe display names.
        assert "Test Front" in result
        assert "Test Back" in result
        assert "Test Outside" in result
        assert "Test Side" in result

    def test_rtsp_url_extracted(self, env_file):
        result = load_camera_creds(env_file)
        assert result["Test Front"]["rtsp_url"] == (
            "rtsp://user:secret@10.0.0.1:554/Streaming/Channels/101"
        )

    def test_ip_extracted_from_rtsp_url(self, env_file):
        # When only RTSP_URL is given, IP is extracted from the URL.
        result = load_camera_creds(env_file)
        assert result["Test Front"]["ip"] == "10.0.0.1"

    def test_explicit_ip_env_overrides(self, env_file):
        # The env file has both _RTSP_URL and _IP for TEST_FRONT.
        # IP should match both (no rotation).
        result = load_camera_creds(env_file)
        assert result["Test Front"]["ip"] == "10.0.0.1"


# ---------------------------------------------------------------------------
# Edge cases: missing file, malformed lines, comments
# ---------------------------------------------------------------------------


class TestLoadCameraCredsEdgeCases:
    """Verify graceful handling of bad inputs."""

    def test_missing_file_returns_empty_dict(self):
        result = load_camera_creds("/nonexistent/path/camera-creds.env")
        assert result == {}

    def test_empty_file_returns_empty_dict(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("")
            path = f.name
        try:
            result = load_camera_creds(path)
            assert result == {}
        finally:
            os.unlink(path)

    def test_comments_and_blank_lines_ignored(self):
        content = (
            "# This is a comment\n"
            "\n"
            "  # Indented comment\n"
            "TEST_FRONT_RTSP_URL=rtsp://u:p@1.2.3.4:554/path\n"
            "\n"
            "TEST_BACK_RTSP_URL=rtsp://u:p@5.6.7.8:554/path\n"
            "\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = load_camera_creds(path)
            assert len(result) == 2
        finally:
            os.unlink(path)

    def test_lines_without_equals_ignored(self):
        content = (
            "this is not a valid env line\n"
            "TEST_FRONT_RTSP_URL=rtsp://u:p@1.2.3.4:554/path\n"
            "another invalid line\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = load_camera_creds(path)
            # Only the valid line should produce an entry.
            assert len(result) == 1
            assert "Test Front" in result
        finally:
            os.unlink(path)

    def test_unknown_prefix_ignored(self):
        # Lines that don't match any camera_map prefix are silently skipped.
        content = (
            "TEST_FRONT_RTSP_URL=rtsp://u:p@1.2.3.4:554/path\n"
            "UNKNOWN_CAMERA_RTSP_URL=rtsp://u:p@9.9.9.9:554/path\n"
            "TEST_FRONT_PORT=554\n"  # not a recognized suffix
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = load_camera_creds(path)
            assert len(result) == 1
            assert "UNKNOWN_CAMERA" not in str(result)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# IP extraction: tricky cases
# ---------------------------------------------------------------------------


class TestExtractIp:
    """Verify _extract_ip handles @ in passwords and IPv6 correctly."""

    def test_ipv4_standard(self, env_file):
        result = load_camera_creds(env_file)
        assert result["Test Front"]["ip"] == "10.0.0.1"

    def test_at_in_password(self, env_file_with_at_password):
        # "p@ss" in the password — anchor on last @ works because
        # rfind walks from the end. The IP is correctly extracted as
        # the part AFTER the last @.
        result = load_camera_creds(env_file_with_at_password)
        assert result["Test Front"]["ip"] == "10.0.0.1"

    def test_urlencoded_at_in_password(self, env_file_with_urlencoded_at):
        # "%40" is the URL-encoded @. Parser doesn't decode it, but
        # rfind still finds the LAST @ (between %40ss and host).
        result = load_camera_creds(env_file_with_urlencoded_at)
        assert result["Test Front"]["ip"] == "10.0.0.1"

    def test_ipv6_host(self, env_file_with_ipv6):
        # IPv6 hosts are bracketed [fe80::1]:554. IP extracted from
        # between the brackets.
        result = load_camera_creds(env_file_with_ipv6)
        assert result["Test Front"]["ip"] == "fe80::1"


# ---------------------------------------------------------------------------
# IP rotation warning
# ---------------------------------------------------------------------------


class TestIpRotationWarning:
    """Verify IP rotation detection (RTSP URL host != _IP env var)."""

    def test_ip_rotation_prints_warning(self, env_file_ip_rotation, capsys):
        # When _IP env disagrees with RTSP URL host, a warning prints.
        result = load_camera_creds(env_file_ip_rotation)
        captured = capsys.readouterr()
        assert "IP rotation detected" in captured.out
        assert "10.0.0.1" in captured.out
        assert "10.0.0.50" in captured.out
        # Result still includes both IPs as parsed — explicit IP wins
        # (overrides RTSP-derived), so we get the explicit one.
        assert result["Test Front"]["ip"] == "10.0.0.1"

    def test_no_warning_when_ips_match(self, env_file, capsys):
        # When _IP matches RTSP URL host, no warning.
        load_camera_creds(env_file)
        captured = capsys.readouterr()
        assert "IP rotation" not in captured.out


# ---------------------------------------------------------------------------
# Camera mapping coverage
# ---------------------------------------------------------------------------


class TestCameraMap:
    """Verify the camera_map covers the documented fleet."""

    def test_all_documented_cameras_mappable(self, env_file):
        result = load_camera_creds(env_file)
        # The env file uses TEST_FRONT/TEST_BACK/TEST_OUTSIDE/TEST_SIDE
        # prefixes — verify each maps to its canonical synthetic name.
        # The new infra.cameras._LEGACY_PREFIX_TO_NAME map resolves
        # these prefixes to public-safe display names.
        expected_canonicals = {
            "Test Front",
            "Test Back",
            "Test Outside",
            "Test Side",
        }
        assert set(result.keys()) == expected_canonicals

    def test_ip_only_no_rtsp_url(self):
        # Camera with _IP but no _RTSP_URL — still gets an entry (just no rtsp_url).
        content = "TEST_FRONT_IP=10.0.0.1\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = load_camera_creds(path)
            assert "Test Front" in result
            assert result["Test Front"]["ip"] == "10.0.0.1"
            assert "rtsp_url" not in result["Test Front"]
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# IP→HTTP_USER / IP→HTTP_PASS lookup helpers (Phase 6B.166 §11.87.2)
# ---------------------------------------------------------------------------
#
# cam_browser.py needs to look up the HTTP username + password for a camera
# given its IP. The old hardcoded FRONT_HTTP_PASS / BACK_HTTP_PASS branching
# broke for every camera except the 833A pair. These tests cover the new
# IP→cred helpers added in 2026-08-30.


class TestGetHttpCreds:
    """Verify IP→HTTP_USER / HTTP_PASS lookup via env file."""

    @pytest.fixture
    def env_file_full_fleet(self):
        """Mimic a 6-camera env file with TEST_* prefixes, all with HTTP_USER/HTTP_PASS.

        Phase 6B.167 §13.4 (Commit 6 scrub): operator IPs/names replaced with
        synthetic TEST_* prefixes (10.0.0.x range, generic test names).
        """
        content = (
            "# Test cameras — synthetic, public-safe\n"
            "TEST_FRONT_IP=10.0.0.1\n"
            "TEST_FRONT_HTTP_USER=admin\n"
            "TEST_FRONT_HTTP_PASS=frontpw\n"
            "TEST_BACK_IP=10.0.0.2\n"
            "TEST_BACK_HTTP_USER=admin\n"
            "TEST_BACK_HTTP_PASS=backpw\n"
            "TEST_OUTSIDE_IP=10.0.0.3\n"
            "TEST_OUTSIDE_HTTP_USER=admin\n"
            "TEST_OUTSIDE_HTTP_PASS=outsidepw\n"
            "TEST_SIDE_IP=10.0.0.4\n"
            "TEST_SIDE_HTTP_USER=admin\n"
            "TEST_SIDE_HTTP_PASS=sidepw\n"
            "TEST_FRONT_GARAGE_IP=10.0.0.5\n"
            "TEST_FRONT_GARAGE_HTTP_USER=admin\n"
            "TEST_FRONT_GARAGE_HTTP_PASS=garagepw\n"
            "TEST_FRONT_POWER_IP=10.0.0.6\n"
            "TEST_FRONT_POWER_HTTP_USER=admin\n"
            "TEST_FRONT_POWER_HTTP_PASS=powerpw\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            yield path
        finally:
            os.unlink(path)

    def test_get_http_user_known_ip(self, env_file_full_fleet):
        from infra.camera_creds import get_http_user
        assert get_http_user("10.0.0.1", env_file_full_fleet) == "admin"
        assert get_http_user("10.0.0.3", env_file_full_fleet) == "admin"

    def test_get_http_password_known_ip(self, env_file_full_fleet):
        from infra.camera_creds import get_http_password
        assert get_http_password("10.0.0.1", env_file_full_fleet) == "frontpw"
        assert get_http_password("10.0.0.2", env_file_full_fleet) == "backpw"
        assert get_http_password("10.0.0.3", env_file_full_fleet) == "outsidepw"
        assert get_http_password("10.0.0.4", env_file_full_fleet) == "sidepw"
        assert get_http_password("10.0.0.5", env_file_full_fleet) == "garagepw"
        assert get_http_password("10.0.0.6", env_file_full_fleet) == "powerpw"

    def test_get_http_user_unknown_ip_returns_none(self, env_file_full_fleet):
        from infra.camera_creds import get_http_user
        assert get_http_user("10.99.99.99", env_file_full_fleet) is None
        assert get_http_user("10.0.0.99", env_file_full_fleet) is None

    def test_get_http_password_unknown_ip_returns_none(self, env_file_full_fleet):
        from infra.camera_creds import get_http_password
        assert get_http_password("10.99.99.99", env_file_full_fleet) is None

    def test_get_http_user_default_when_user_field_missing(self):
        # If _IP exists but _HTTP_USER is missing, default to "admin"
        # per Reolink convention.
        content = "TEST_FRONT_IP=10.0.0.1\nTEST_FRONT_HTTP_PASS=frontpw\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            from infra.camera_creds import get_http_user
            assert get_http_user("10.0.0.1", path) == "admin"
        finally:
            os.unlink(path)

    def test_get_http_password_missing_pass_returns_none(self):
        # If _IP exists but _HTTP_PASS is missing, password is None
        # (caller treats None as "cannot log in").
        content = "TEST_FRONT_IP=10.0.0.1\nTEST_FRONT_HTTP_USER=admin\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            from infra.camera_creds import get_http_password
            assert get_http_password("10.0.0.1", path) is None
        finally:
            os.unlink(path)

    def test_get_http_password_empty_pass_returns_none(self):
        # _HTTP_PASS=  (empty value) is treated as missing.
        content = "TEST_FRONT_IP=10.0.0.1\nTEST_FRONT_HTTP_USER=admin\nTEST_FRONT_HTTP_PASS=\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            from infra.camera_creds import get_http_password
            assert get_http_password("10.0.0.1", path) is None
        finally:
            os.unlink(path)

    def test_get_http_creds_missing_env_file(self):
        # Missing env file → both helpers return None (not raise).
        from infra.camera_creds import get_http_password, get_http_user
        assert get_http_user("10.0.0.1", "/nonexistent/path/.env") is None
        assert get_http_password("10.0.0.1", "/nonexistent/path/.env") is None

    def test_get_http_creds_skip_comments_and_blank_lines(self):
        # Comments (#) and blank lines don't trip up the parser.
        content = (
            "# This is a comment\n"
            "\n"
            "TEST_FRONT_IP=10.0.0.1\n"
            "# Another comment\n"
            "TEST_FRONT_HTTP_USER=admin\n"
            "TEST_FRONT_HTTP_PASS=frontpw\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            from infra.camera_creds import get_http_password, get_http_user
            assert get_http_user("10.0.0.1", path) == "admin"
            assert get_http_password("10.0.0.1", path) == "frontpw"
        finally:
            os.unlink(path)

    def test_get_http_creds_default_env_path_uses_camera_creds_file(self, monkeypatch):
        # When env_path is None AND no cameras.env is present, the
        # helper falls through to infra.paths.CAMERA_CREDS_FILE. The
        # Phase 6B.167 §13.4 C18 chain is:
        #   $FARMSURV_CAMERAS_ENV → CAMERAS_ENV_FILE → CAMERA_CREDS_FILE.
        # Mock both upper-tier paths to non-existent temp files so the
        # fallback reaches CAMERA_CREDS_FILE.
        content = "TEST_FRONT_IP=10.0.0.1\nTEST_FRONT_HTTP_USER=admin\nTEST_FRONT_HTTP_PASS=frontpw\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(content)
            cam_creds = f.name
        # Point CAMERAS_ENV_FILE at a path that does not exist so the
        # _default_env_path() chain falls through to CAMERA_CREDS_FILE.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=True) as g:
            nonexistent_env = g.name
        try:
            monkeypatch.setattr("infra.paths.CAMERAS_ENV_FILE", nonexistent_env)
            monkeypatch.setattr("infra.paths.CAMERA_CREDS_FILE", cam_creds)
            monkeypatch.delenv("FARMSURV_CAMERAS_ENV", raising=False)
            from infra.camera_creds import get_http_password, get_http_user
            assert get_http_user("10.0.0.1") == "admin"
            assert get_http_password("10.0.0.1") == "frontpw"
        finally:
            os.unlink(cam_creds)


# ---------------------------------------------------------------------------
# browser_chrome_path env var (Phase 6B.166 §11.87.2)
# ---------------------------------------------------------------------------


class TestBrowserChromePath:
    """Verify BROWSER_CHROME_PATH env override works."""

    def test_default_is_system_chrome(self, monkeypatch):
        # With BROWSER_CHROME_PATH unset, fallback to macOS system path.
        monkeypatch.delenv("BROWSER_CHROME_PATH", raising=False)
        # Force reimport
        import importlib

        import infra.paths
        importlib.reload(infra.paths)
        assert infra.paths.BROWSER_CHROME_PATH.endswith("Google Chrome")

    def test_env_override_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("BROWSER_CHROME_PATH", "/usr/bin/fake-chrome")
        import importlib

        import infra.paths
        importlib.reload(infra.paths)
        assert infra.paths.BROWSER_CHROME_PATH == "/usr/bin/fake-chrome"
