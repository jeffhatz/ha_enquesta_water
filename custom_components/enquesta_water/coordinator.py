"""Data coordinator for Enquesta Water."""

from __future__ import annotations

import asyncio
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

from .api import EnquestaAuthError, EnquestaClient, EnquestaError, UsageSnapshot
from .const import (
    CONF_BASE_URL,
    CONF_METER_ID,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HISTORY_BACKFILL_DAYS,
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
        self._history_backfill_task: asyncio.Task[None] | None = None
        self.history_backfill_status: dict[str, Any] = {
            "running": False,
            "days_requested": HISTORY_BACKFILL_DAYS,
        }

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
        self._async_import_hourly_statistics(hourly)
        return updated

    async def _async_load_store(self) -> dict[str, Any]:
        """Load the persistent usage ledger."""
        if self._stored_usage is None:
            self._stored_usage = await self._store.async_load() or {}
        return self._stored_usage

    def async_schedule_initial_history_backfill(self) -> None:
        """Schedule the first missing-history backfill after setup."""
        if self._history_backfill_task and not self._history_backfill_task.done():
            return
        self._history_backfill_task = self.hass.async_create_task(
            self._async_initial_history_backfill()
        )

    def async_start_history_backfill(self) -> None:
        """Start a manual missing-history backfill."""
        if self._history_backfill_task and not self._history_backfill_task.done():
            return
        self._history_backfill_task = self.hass.async_create_task(
            self._async_backfill_history(force=False)
        )

    async def _async_initial_history_backfill(self) -> None:
        """Run a one-time missing-history backfill for this config entry."""
        stored = await self._async_load_store()
        account = stored.setdefault(self.config_entry.entry_id, {})
        history = account.setdefault("history_backfill", {})
        if history.get("initial_completed"):
            self.history_backfill_status = {
                **history,
                "running": False,
                "days_requested": HISTORY_BACKFILL_DAYS,
            }
            return
        await self._async_backfill_history(force=False, mark_initial_completed=True)

    async def _async_backfill_history(
        self,
        *,
        force: bool,
        mark_initial_completed: bool = False,
    ) -> None:
        """Fetch missing hourly usage history and import it as statistics."""
        if not self.data or not self.data.latest_day:
            return

        started_at = datetime.now(UTC)
        latest_day = self.data.latest_day
        meter_id = self.data.meter_id
        status: dict[str, Any] = {
            "running": True,
            "days_requested": HISTORY_BACKFILL_DAYS,
            "started_at": started_at.isoformat(),
            "latest_day": latest_day.isoformat(),
            "days_imported": 0,
            "days_skipped": 0,
            "stopped_at": None,
            "error": None,
        }
        self.history_backfill_status = status

        stored = await self._async_load_store()
        account = stored.setdefault(self.config_entry.entry_id, {})
        meters = account.setdefault("meters", {})
        meter = meters.setdefault(meter_id, {"daily": {}, "updated_at": None})
        hourly: dict[str, list[float]] = meter.setdefault("hourly", {})

        changed = False
        completed = False
        try:
            for day_offset in range(HISTORY_BACKFILL_DAYS):
                target_day = latest_day - timedelta(days=day_offset)
                day_key = target_day.isoformat()
                if day_key in hourly and not force:
                    status["days_skipped"] += 1
                    continue

                try:
                    readings = await self.client.async_get_hourly_usage_for_day(target_day)
                except EnquestaError as err:
                    status["stopped_at"] = day_key
                    status["error"] = str(err)
                    _LOGGER.info(
                        "Stopping Enquesta hourly history backfill at %s: %s",
                        day_key,
                        err,
                    )
                    break
                except Exception as err:
                    status["stopped_at"] = day_key
                    status["error"] = str(err)
                    _LOGGER.info(
                        "Stopping Enquesta hourly history backfill at %s",
                        day_key,
                        exc_info=True,
                    )
                    break

                hourly[day_key] = [round(reading.gallons, 3) for reading in readings]
                status["days_imported"] += 1
                changed = True
                await asyncio.sleep(0.05)
            completed = True
        except asyncio.CancelledError:
            status["error"] = "Backfill was cancelled"
            raise
        finally:
            status["running"] = False
            status["finished_at"] = datetime.now(UTC).isoformat()
            history = account.setdefault("history_backfill", {})
            history.update(status)
            if mark_initial_completed and completed:
                history["initial_completed"] = True
                status["initial_completed"] = True
            meter["updated_at"] = datetime.now(UTC).isoformat()
            await self._store.async_save(stored)
            if changed:
                self._async_import_hourly_statistics(hourly)

    def async_close(self) -> None:
        """Close owned resources."""
        if self._history_backfill_task and not self._history_backfill_task.done():
            self._history_backfill_task.cancel()
        self.session.detach()

    def _async_import_hourly_statistics(
        self,
        hourly: dict[str, list[float]],
    ) -> None:
        """Import stored hourly usage as external long-term statistics."""
        if not hourly:
            return

        statistics = _stored_hourly_statistics(hourly, self._timezone)
        if not statistics:
            return

        metadata: StatisticMetaData = {
            "has_sum": False,
            "mean_type": StatisticMeanType.ARITHMETIC,
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
    timezone: tzinfo,
) -> list[StatisticData]:
    """Build raw hourly usage statistics from Enquesta hourly buckets."""
    if len(hourly_values) != 24:
        return []

    start = datetime.combine(day, time.min, tzinfo=timezone)

    statistics: list[StatisticData] = []
    # Enquesta labels hourly water buckets by the ending hour, while HA
    # statistics render the row start time. Shift forward to match the portal.
    for hour_offset, gallons in enumerate(hourly_values, start=1):
        value = round(gallons, 3)
        statistics.append(
            {
                "start": start + timedelta(hours=hour_offset),
                "mean": value,
                "min": value,
                "max": value,
            }
        )
    return statistics


def _stored_hourly_statistics(
    hourly: dict[str, list[float]],
    timezone: tzinfo,
) -> list[StatisticData]:
    """Build raw hourly usage statistics for all stored days."""
    statistics: list[StatisticData] = []
    for day_key in sorted(hourly):
        try:
            day = date.fromisoformat(day_key)
        except ValueError:
            continue
        statistics.extend(_hourly_statistics(day, hourly[day_key], timezone))
    return statistics
