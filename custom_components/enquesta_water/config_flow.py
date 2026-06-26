"""Config flow for Enquesta Water."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EnquestaAuthError, EnquestaClient, EnquestaError
from .const import CONF_BASE_URL, CONF_METER_ID, DEFAULT_BASE_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
        vol.Optional(CONF_METER_ID): str,
    }
)


class EnquestaWaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle an Enquesta Water config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            base_url = user_input.get(CONF_BASE_URL, DEFAULT_BASE_URL).rstrip("/")
            meter_id = user_input.get(CONF_METER_ID) or None

            await self.async_set_unique_id(f"{base_url}:{username.lower()}:{meter_id or 'default'}")
            self._abort_if_unique_id_configured()

            client = EnquestaClient(
                async_get_clientsession(self.hass),
                username=username,
                password=user_input[CONF_PASSWORD],
                base_url=base_url,
                meter_id=meter_id,
            )

            try:
                snapshot = await client.async_get_usage()
            except EnquestaAuthError:
                errors["base"] = "invalid_auth"
            except EnquestaError as err:
                _LOGGER.warning("Enquesta setup failed: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during Enquesta setup")
                errors["base"] = "unknown"
            else:
                data = dict(user_input)
                data[CONF_BASE_URL] = base_url
                if meter_id is None:
                    data[CONF_METER_ID] = snapshot.meter_id
                return self.async_create_entry(title=f"Enquesta Water {snapshot.meter_id}", data=data)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)
