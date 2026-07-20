"""Tests for legacy charging API fallback and safe error logging."""

import asyncio
from datetime import timedelta
import logging

from rivian.exceptions import RivianApiException

from custom_components.rivian.coordinator import ChargingCoordinator, _log_api_exception


def _removed_query_error() -> RivianApiException:
    """Build the response returned after removal of getLiveSessionData."""
    return RivianApiException(
        "Error occurred while reading the graphql response from Rivian.",
        400,
        {
            "errors": [
                {
                    "message": (
                        'Cannot query field "getLiveSessionData" on type "Query".'
                    ),
                    "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"},
                }
            ]
        },
        {"U-Sess": "synthetic-session-secret"},
        {
            "operationName": "getLiveSessionData",
            "variables": {"vehicleId": "synthetic-vehicle-id"},
        },
    )


class _RemovedQueryCoordinator(ChargingCoordinator):
    """Charging coordinator whose API always rejects the removed query."""

    def __init__(self) -> None:
        self.data = None
        self._error_count = 0
        self._live_session_query_unavailable = False
        self.update_interval = timedelta(seconds=30)

    async def _fetch_data(self):
        raise _removed_query_error()


def test_removed_legacy_charging_query_does_not_fail_setup(caplog) -> None:
    """A removed R1 charging query yields empty data and disables polling."""
    coordinator = _RemovedQueryCoordinator()

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(coordinator._async_update_data()) == {}

    assert coordinator._live_session_query_unavailable
    assert coordinator.update_interval is None
    assert "continuing without it" in caplog.text
    assert "synthetic-session-secret" not in caplog.text
    assert "synthetic-vehicle-id" not in caplog.text


def test_removed_query_polling_cannot_be_reenabled() -> None:
    """Vehicle-state updates do not restart a permanently rejected query."""
    coordinator = _RemovedQueryCoordinator()
    coordinator._live_session_query_unavailable = True
    coordinator._set_update_interval = lambda *_: (_ for _ in ()).throw(AssertionError)

    coordinator.adjust_update_interval(is_plugged_in=True)


def test_api_error_logging_excludes_headers_and_variables(caplog) -> None:
    """Structured diagnostics retain the API error without request secrets."""
    with caplog.at_level(logging.ERROR):
        _log_api_exception(_removed_query_error())

    assert "operation=getLiveSessionData" in caplog.text
    assert "status=400" in caplog.text
    assert "code=GRAPHQL_VALIDATION_FAILED" in caplog.text
    assert "synthetic-session-secret" not in caplog.text
    assert "synthetic-vehicle-id" not in caplog.text
