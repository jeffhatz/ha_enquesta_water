"""Buttons for Enquesta Water."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnquestaWaterCoordinator


@dataclass(frozen=True, kw_only=True)
class EnquestaButtonDescription(ButtonEntityDescription):
    """Enquesta button description."""


BACKFILL_HISTORY = EnquestaButtonDescription(
    key="backfill_history",
    translation_key="backfill_history",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Enquesta Water buttons."""
    coordinator: EnquestaWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EnquestaBackfillHistoryButton(coordinator, entry, BACKFILL_HISTORY)])


class EnquestaBackfillHistoryButton(CoordinatorEntity[EnquestaWaterCoordinator], ButtonEntity):
    """Button to backfill Enquesta hourly usage history."""

    entity_description: EnquestaButtonDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EnquestaWaterCoordinator,
        entry: ConfigEntry,
        description: EnquestaButtonDescription,
    ) -> None:
        """Initialize button."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Enquesta Water",
            "manufacturer": "SilverBlaze Enquesta",
        }

    async def async_press(self) -> None:
        """Start a missing hourly usage history backfill."""
        self.coordinator.async_start_history_backfill()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return backfill status attributes."""
        return self.coordinator.history_backfill_status
