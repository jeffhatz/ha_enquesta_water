"""Data coordinator for Enquesta Water."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
import logging
from typing import Any

import aiohttp

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData, StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfVolume, VOLUME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EnquestaAuthError, EnquestaClient, UsageSnapshot
from .const import (
    CONF_BASE_URL,
    CONF_METER_ID,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HOURLY_STATISTIC_ID,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class EnquestaWaterCoordinator(DataUpdateCoordinator[UsageSnapshot]):
    """Coordinate Enquesta water usage updates."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        self.session = async_create_clientsession(
            hass,
            auto_cleanup=False,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
        self.client = EnquestaClient(
            self.session,
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            base_url=entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
            meter_id=entry.data.get(CONF_METER_ID),
        )
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._stored_usage: dict[str, Any] | None = None
        self._timezone = dt_util.get_time_zone(hass.config.time_zone) or UTC

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
        hourly: dict[str, list[float]] = meter.setdefault("hourly", {})

        changed = False
        for reading in snapshot.daily_usage:
            current = float(daily.get(reading.bucket, 0.0))
            # Use max to keep the exposed counter monotonic if Enquesta revises a recent bucket.
            gallons = max(current, reading.gallons)
            if gallons != current:
                daily[reading.bucket] = round(gallons, 3)
                changed = True

        if snapshot.latest_day and snapshot.hourly_usage:
            day_key = snapshot.latest_day.isoformat()
            hourly_values = [round(reading.gallons, 3) for reading in snapshot.hourly_usage]
            if hourly.get(day_key) != hourly_values:
                hourly[day_key] = hourly_values
                changed = True

        if changed:
            meter["updated_at"] = datetime.now(UTC).isoformat()
            await self._store.async_save(stored)

        total = round(sum(float(value) for value in daily.values()), 3)
        updated = UsageSnapshot(
            meter_id=snapshot.meter_id,
            daily_usage=snapshot.daily_usage,
            hourly_usage=snapshot.hourly_usage,
            latest_day=snapshot.latest_day,
            latest_day_gallons=snapshot.latest_day_gallons,
            total_consumption_gallons=total,
            daily_from=snapshot.daily_from,
            daily_to=snapshot.daily_to,
        )
        self._async_import_hourly_statistics(hourly, daily)
        return updated

    async def _async_load_store(self) -> dict[str, Any]:
        """Load the persistent usage ledger."""
        if self._stored_usage is None:
            self._stored_usage = await self._store.async_load() or {}
        return self._stored_usage

    def async_close(self) -> None:
        """Close owned resources."""
        self.session.detach()

    def _async_import_hourly_statistics(
        self,
        hourly: dict[str, list[float]],
        daily: dict[str, float],
    ) -> None:
        """Import stored hourly usage as external long-term statistics."""
        if not hourly:
            return

        statistics = _stored_hourly_statistics(hourly, daily, self._timezone)
        if not statistics:
            return

        metadata: StatisticMetaData = {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": "Enquesta Water Hourly Usage",
            "source": DOMAIN,
            "statistic_id": HOURLY_STATISTIC_ID,
            "unit_class": VOLUME,
            "unit_of_measurement": UnitOfVolume.GALLONS,
        }

        try:
            async_add_external_statistics(self.hass, metadata, statistics)
        except Exception:
            _LOGGER.exception("Failed to import Enquesta hourly usage statistics")


def _hourly_statistics(
    day: date,
    hourly_values: list[float],
    daily: dict[str, float],
    timezone: tzinfo,
) -> list[StatisticData]:
    """Build cumulative hourly statistics from Enquesta hourly buckets."""
    if len(hourly_values) != 24:
        return []

    baseline = sum(
        gallons
        for bucket, gallons in daily.items()
        if bucket < day.isoformat()
    )
    start = datetime.combine(day, time.min, tzinfo=timezone)

    statistics: list[StatisticData] = [
        {
            "start": start,
            "state": round(baseline, 3),
            "sum": round(baseline, 3),
        }
    ]
    running = baseline
    for hour_offset, gallons in enumerate(hourly_values, start=1):
        running += gallons
        statistics.append(
            {
                "start": start + timedelta(hours=hour_offset),
                "state": round(running, 3),
                "sum": round(running, 3),
            }
        )
    return statistics


def _stored_hourly_statistics(
    hourly: dict[str, list[float]],
    daily: dict[str, float],
    timezone: tzinfo,
) -> list[StatisticData]:
    """Build cumulative hourly statistics for all stored days."""
    statistics: list[StatisticData] = []
    for day_key in sorted(hourly):
        try:
            day = date.fromisoformat(day_key)
        except ValueError:
            continue
        statistics.extend(_hourly_statistics(day, hourly[day_key], daily, timezone))
    return statistics
