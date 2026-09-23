"""Tests for infra.farm_api.cooldowns.

STATUS: V2-068b4
RUN WITH: pytest tests/test_farm_api_cooldowns.py -v

Test strategy:
    - Import list_active_cooldowns and Cooldown from infra.farm_api.cooldowns.
    - Verify the function returns a list and that list is empty (pipeline
      state not yet exposed).
"""

import logging

from infra.farm_api.cooldowns import list_active_cooldowns


class TestListActiveCooldowns:
    """test_list_active_cooldowns_returns_list: list_active_cooldowns() returns
    a list (currently empty because pipeline state is not yet exposed)."""

    def test_list_active_cooldowns_returns_list(self, caplog):
        """list_active_cooldowns returns a list and logs a TODO.

        Verifies:
        - Return type is list
        - List is empty (pipeline state not yet exposed)
        - A TODO log message is emitted
        """
        with caplog.at_level(logging.INFO):
            result = list_active_cooldowns()

        assert isinstance(result, list)
        assert result == []
        assert any("TODO" in record.message for record in caplog.records)
