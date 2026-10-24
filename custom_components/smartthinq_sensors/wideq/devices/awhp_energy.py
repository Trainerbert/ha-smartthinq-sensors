"""Read LG AWHP monthly energy using the endpoint used by the ThinQ app.

This module has no Home Assistant dependency. API values are Wh; sensor values
are kWh. Missing data is deliberately different from a measured zero.
"""

from __future__ import annotations

import asyncio
import calendar
from datetime import date, datetime, tzinfo
from decimal import Decimal, InvalidOperation
import logging
from time import monotonic
from typing import Any
from urllib.parse import urlencode

_LOGGER = logging.getLogger(__name__)
POLL_INTERVAL = 30 * 60
REQUEST_TIMEOUT = 10


def _reading(value: Any) -> Decimal | None:
    """Read a nonnegative Wh measurement without converting missing data to 0."""
    if value is None or value == "NO_DATA":
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean energy reading")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid energy reading") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("Invalid energy reading")
    return number


def monthly_totals(result: Any, month: str) -> dict[str, float | None]:
    """Sum the app's daily heat/cool/hot-water electricity and calorie fields.

    Session.get2 has already removed the API's result envelope. An absent field
    is a schema mismatch; LG's explicit NO_DATA marker contributes no energy,
    just as in the app. If every entry is NO_DATA, the sensor remains unknown.
    """
    if not isinstance(result, dict) or not isinstance(result.get("item"), list):
        raise ValueError("Unexpected AWHP energy history response")

    electric = Decimal(0)
    heat = Decimal(0)
    electric_known = heat_known = False
    electric_missing = heat_missing = False
    seen: set[date] = set()
    for item in result["item"]:
        if not isinstance(item, dict) or not isinstance(item.get("usedDate"), str):
            raise ValueError("Invalid AWHP energy history date")
        day = date.fromisoformat(item["usedDate"])
        if day.strftime("%Y-%m") != month:
            continue
        if day in seen:
            raise ValueError("Duplicate AWHP energy history date")
        seen.add(day)

        for key in ("heatMode", "coolMode", "hotWaterMode"):
            if key not in item or item[key] is None:
                electric_missing = True
            value = _reading(item.get(key))
            if value is not None:
                electric += value
                electric_known = True
        if "calorie" not in item or item["calorie"] is None:
            heat_missing = True
        value = _reading(item.get("calorie"))
        if value is not None:
            heat += value
            heat_known = True

    return {
        "electricity": (
            float(electric / 1000) if electric_known and not electric_missing else None
        ),
        "heat": float(heat / 1000) if heat_known and not heat_missing else None,
    }


class AwhpEnergyHistory:
    """Cache one read-only monthly history request, isolated from climate control."""

    def __init__(self) -> None:
        self._timezone: tzinfo | None = None
        self._next_poll = 0.0
        self._month: str | None = None
        self._reset: str | None = None
        self._updated: str | None = None
        self._values: dict[str, float | None] = {}
        self._status = "waiting"

    def set_timezone(self, zone: tzinfo) -> None:
        """Use Home Assistant's configured timezone, including DST changes."""
        if zone != self._timezone:
            self._timezone = zone
            self._month = None
            self._next_poll = 0.0
            self._values = {}
            self._reset = self._updated = None
            self._status = "waiting"

    @property
    def data(self) -> dict[str, Any]:
        """Return sensor values and the period used for last_reset."""
        return {
            **self._values,
            "month": self._month,
            "reset": self._reset,
            "updated": self._updated,
            "status": self._status,
        }

    async def async_update(self, session: Any, device_id: str) -> None:
        """Fetch the current calendar month at most once every 30 minutes."""
        if self._timezone is None:
            return
        now = datetime.now(self._timezone)
        month = now.strftime("%Y-%m")
        if month != self._month:
            self._month = month
            self._reset = now.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            self._values = {}
            self._updated = None
            self._status = "waiting"
            self._next_poll = 0.0
        if monotonic() < self._next_poll:
            return
        self._next_poll = monotonic() + POLL_INTERVAL

        end_day = calendar.monthrange(now.year, now.month)[1]
        query = urlencode({
            "period": "day",
            "startDate": f"{month}-01",
            "endDate": f"{month}-{end_day:02d}",
        })
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                result = await session.get2(
                    f"service/aircon/{device_id}/awhp-energy-history?{query}"
                )
            values = monthly_totals(result, month)
        except Exception as exc:  # Energy errors must not fail the climate poll.
            self._values = {}
            self._status = type(exc).__name__
            # No response payload, access token, device ID or signed URL in this log.
            _LOGGER.warning(
                "AWHP energy history unavailable (%s); retry in 30 minutes",
                type(exc).__name__,
            )
            return

        # A request crossing midnight must not publish the old month as the new one.
        completed = datetime.now(self._timezone)
        if completed.strftime("%Y-%m") != month:
            self._values = {}
            self._next_poll = 0.0
            self._status = "waiting"
            return
        self._values = values
        self._updated = completed.isoformat()
        self._status = "ok" if any(v is not None for v in values.values()) else "no_data"
