"""Config flow for the Skylink integration.

Single-step user flow: email + password. Validates credentials by
calling authenticate() against the cloud; refuses to persist an entry
if login fails. Reauth flow prompts for a new password when the stored
one stops working.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from ._client.client import OrbitClient
from ._client.errors import OrbitAuthError, OrbitConnectionError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


async def _probe_credentials(username: str, password: str) -> str | None:
    """Try to authenticate; return an error slug on failure, None on success.

    Keeps config_flow steps focused on their UX logic instead of
    re-implementing the same error translation three times.
    """
    client = OrbitClient()
    try:
        await client.authenticate(username, password)
        return None
    except OrbitAuthError:
        return "invalid_auth"
    except OrbitConnectionError:
        return "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error during Skylink credential probe")
        return "unknown"
    finally:
        await client.disconnect()


class SkylinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Skylink."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_username: str = ""

    # ------------------------------------------------------------------
    # Initial setup
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()

            err = await _probe_credentials(username, password)
            if err is not None:
                errors["base"] = err
            else:
                return self.async_create_entry(
                    title=f"Skylink ({username})",
                    data={CONF_USERNAME: username, CONF_PASSWORD: password},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_USER_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Reauth
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        self._reauth_username = entry_data[CONF_USERNAME]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            err = await _probe_credentials(self._reauth_username, password)
            if err is not None:
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={CONF_PASSWORD: password},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"username": self._reauth_username},
        )
