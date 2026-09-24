"""test_farm_api_cooldowns_e2e — E2E tests for farm_api cooldowns endpoint.

E2E strategy:
    - Import list_active_cooldowns directly and verify:
      (1) return type is list,
      (2) list is empty (pipeline state not yet exposed in V2),
      (3) TODO log message is emitted when called.

Bug classes covered:
    - list_active_cooldowns returns wrong type (not list).
    - list_active_cooldowns crashes when pipeline state is absent.
"""

from __future__ import annotations

import logging

from infra.farm_api.cooldowns import list_active_cooldowns


class TestListActiveCooldownsE2E:
    """E2E: list_active_cooldowns() returns a list and logs a TODO."""

    def test_returns_list(self, caplog):
        with caplog.at_level(logging.INFO):
            result = list_active_cooldowns()

        assert isinstance(result, list)

    def test_list_is_empty(self, caplog):
        with caplog.at_level(logging.INFO):
            result = list_active_cooldowns()

        assert result == []

    def test_logs_todo(self, caplog):
        with caplog.at_level(logging.INFO):
            list_active_cooldowns()

        assert any("TODO" in record.message for record in caplog.records)

    def test_returns_empty_list_not_none(self):
        """Not None — empty list."""
        result = list_active_cooldowns()
        assert result is not None
        assert isinstance(result, list)
        assert len(result) == 0
