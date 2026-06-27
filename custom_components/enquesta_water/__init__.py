"""Enquesta Water integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DAYS,
    DOMAIN,
    HISTORY_BACKFILL_DAYS,
    PLATFORMS,
    SERVICE_BACKFILL_HOURLY_HISTORY,
)
from .coordinator import EnquestaWaterCoordinator

BACKFILL_HISTORY_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DAYS, default=HISTORY_BACKFILL_DAYS): vol.All(
            vol.Coerce(int),
            vol.Range(min=1),
        ),
        vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
    }
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up Enquesta Water services."""
    hass.data.setdefault(DOMAIN, {})

    async def async_backfill_hourly_history(call: ServiceCall) -> None:
        """Handle the backfill hourly history service."""
        days = call.data[ATTR_DAYS]
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        coordinators = _service_coordinators(hass, entry_id)
        if not coordinators:
            raise HomeAssistantError("No loaded Enquesta Water config entries found")
        for coordinator in coordinators:
            if not coordinator.async_start_history_backfill(days):
                raise HomeAssistantError("Enquesta Water history backfill is already running")

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL_HOURLY_HISTORY,
        async_backfill_hourly_history,
        schema=BACKFILL_HISTORY_SERVICE_SCHEMA,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Enquesta Water from a config entry."""
    coordinator = EnquestaWaterCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    try:
        await coordinator.async_config_entry_first_refresh()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        coordinator.async_schedule_initial_history_backfill()
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        coordinator.async_close()
        raise
    else:
        return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: EnquestaWaterCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.async_close()
    return unload_ok


def _service_coordinators(
    hass: HomeAssistant,
    entry_id: str | None,
) -> list[EnquestaWaterCoordinator]:
    """Return coordinators targeted by a service call."""
    data = hass.data.get(DOMAIN, {})
    if entry_id:
        coordinator = data.get(entry_id)
        if coordinator is None:
            raise HomeAssistantError(f"Enquesta Water config entry {entry_id!r} is not loaded")
        return [coordinator]
    return [
        coordinator
        for coordinator in data.values()
        if isinstance(coordinator, EnquestaWaterCoordinator)
    ]
