"""
test_extract_ip_from_rtsp.py — E2E tests for _extract_ip_from_rtsp.

Covers the 4 cases from US-088:
1. Normal RTSP URL -> returns the IP.
2. Percent-encoded (rtsp%3A%2F%2F...) -> parsed IP, no crash.
3. Empty string -> returns "" (not empty), no crash.
4. URL with no host -> returns ""/None, no crash.

No mocks — pure function testing per V2-080 E2E-first rule.
All IPs use RFC 5737 TEST-NET-1 (192.0.2.0/24).
"""

from infra.camera_creds import _extract_ip_from_rtsp


class TestExtractIpFromRtspE2E:
    """E2E cases from US-088 task body."""

    def test_normal_rtsp_url(self):
        """Normal RTSP URL returns the extracted IP."""
        url = "rtsp://admin:pass@192.0.2.1:554/h264"
        assert _extract_ip_from_rtsp(url) == "192.0.2.1"

    def test_percent_encoded_url(self):
        """Percent-encoded scheme decodes and returns the IP — no crash."""
        url = "rtsp%3A%2F%2Fadmin:pass@192.0.2.1:554/h264"
        assert _extract_ip_from_rtsp(url) == "192.0.2.1"

    def test_empty_string(self):
        """Empty string returns '' (not empty) — no crash."""
        assert _extract_ip_from_rtsp("") == ""

    def test_url_with_no_host(self):
        """URL with no host (no @ for auth extraction) returns None — no crash."""
        assert _extract_ip_from_rtsp("rtsp://192.0.2.1:554/") is None
