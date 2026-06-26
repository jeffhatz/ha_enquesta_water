"""Data coordinator for Enquesta Water."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EnquestaAuthError, EnquestaClient, UsageSnapshot
from .const import (
    CONF_BASE_URL,
    CONF_METER_ID,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class EnquestaWaterCoordinator(DataUpdateCoordinator[UsageSnapshot]):
    """Coordinate Enquesta water usage updates."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        self.client = EnquestaClient(
            async_get_clientsession(hass),
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            base_url=entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
            meter_id=entry.data.get(CONF_METER_ID),
        )
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._stored_usage: dict[str, Any] | None = None

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> UsageSnapshot:
        """Fetch water usage data."""
        try:
            snapshot = await self.client.async_get_usage()
            return await self._async_apply_usage_ledger(snapshot)
        except EnquestaAuthError as err:
            raise UpdateFailed("Invalid Enquesta credentials") from err
        except Exception as err:
            raise UpdateFailed(str(err)) from err

    async def _async_apply_usage_ledger(self, snapshot: UsageSnapshot) -> UsageSnapshot:
        """Persist usage by day and expose a monotonic synthetic total."""
        stored = await self._async_load_store()
        account = stored.setdefault(self.config_entry.entry_id, {})
        meters = account.setdefault("meters", {})
        meter = meters.setdefault(snapshot.meter_id, {"daily": {}, "updated_at": None})
        daily: dict[str, float] = meter.setdefault("daily", {})

        changed = False
        for reading in snapshot.daily_usage:
            current = float(daily.get(reading.bucket, 0.0))
            # Use max to keep the exposed counter monotonic if Enquesta revises a recent bucket.
            gallons = max(current, reading.gallons)
            if gallons != current:
                daily[reading.bucket] = round(gallons, 3)
                changed = True

        if changed:
            meter["updated_at"] = datetime.now(UTC).isoformat()
            await self._store.async_save(stored)

        total = round(sum(float(value) for value in daily.values()), 3)
        return UsageSnapshot(
            meter_id=snapshot.meter_id,
            daily_usage=snapshot.daily_usage,
            hourly_usage=snapshot.hourly_usage,
            latest_day=snapshot.latest_day,
            latest_day_gallons=snapshot.latest_day_gallons,
            total_consumption_gallons=total,
            daily_from=snapshot.daily_from,
            daily_to=snapshot.daily_to,
        )

    async def _async_load_store(self) -> dict[str, Any]:
        """Load the persistent usage ledger."""
        if self._stored_usage is None:
            self._stored_usage = await self._store.async_load() or {}
        return self._stored_usage
