"""Sensors for Enquesta Water."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, HOURLY_STATISTIC_ID
from .coordinator import EnquestaWaterCoordinator


@dataclass(frozen=True, kw_only=True)
class EnquestaSensorDescription(SensorEntityDescription):
    """Enquesta sensor description."""


WATER_TOTAL = EnquestaSensorDescription(
    key="water_total",
    translation_key="water_total",
    device_class=SensorDeviceClass.WATER,
    native_unit_of_measurement=UnitOfVolume.GALLONS,
    state_class=SensorStateClass.TOTAL_INCREASING,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Enquesta Water sensors."""
    coordinator: EnquestaWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EnquestaWaterSensor(coordinator, entry, WATER_TOTAL)])


class EnquestaWaterSensor(CoordinatorEntity[EnquestaWaterCoordinator], SensorEntity):
    """Enquesta synthetic water total sensor."""

    entity_description: EnquestaSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EnquestaWaterCoordinator,
        entry: ConfigEntry,
        description: EnquestaSensorDescription,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Enquesta Water",
            "manufacturer": "SilverBlaze Enquesta",
        }

    @property
    def native_value(self) -> float | None:
        """Return the synthetic total water consumption."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.total_consumption_gallons

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return useful parsed source attributes."""
        data = self.coordinator.data
        if not data:
            return None
        return {
            "meter_id": data.meter_id,
            "latest_day": _date_iso(data.latest_day),
            "latest_day_gallons": data.latest_day_gallons,
            "daily_from": _date_iso(data.daily_from),
            "daily_to": _date_iso(data.daily_to),
            "hourly_statistic_id": HOURLY_STATISTIC_ID,
            "daily_usage": _readings(data.daily_usage),
            "hourly_usage": _readings(data.hourly_usage),
        }


def _date_iso(value: date | None) -> str | None:
    """Return ISO date string."""
    return value.isoformat() if value else None


def _readings(readings: list[Any]) -> list[dict[str, Any]]:
    """Serialize usage readings for attributes."""
    return [{"bucket": reading.bucket, "gallons": reading.gallons} for reading in readings]
