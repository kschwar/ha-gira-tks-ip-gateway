from __future__ import annotations

import ipaddress
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_GATEWAY_HOST,
    CONF_LISTEN_PORT,
    CONF_LOCAL_IP,
    CONF_PASSWORD,
    CONF_AUTO_ANSWER,
    CONF_MEDIA_PORT,
    CONF_SIP_ENABLED,
    CONF_SIP_PASSWORD,
    CONF_SIP_PORT,
    CONF_SIP_USERNAME,
    CONF_SIP_SWITCHING,
    CONF_USERNAME,
    DEFAULT_MEDIA_PORT,
    DEFAULT_LISTEN_PORT,
    DEFAULT_LOCAL_IP,
    DEFAULT_SIP_PORT,
    DOMAIN,
)

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _latin1(value: str) -> bool:
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, str | int] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input[CONF_GATEWAY_HOST]).strip()
            port = int(user_input[CONF_LISTEN_PORT])
            try:
                host = str(ipaddress.IPv4Address(host))
            except ipaddress.AddressValueError:
                errors["base"] = "invalid_gateway_host"
            if not errors:
                unique_id = f"{host}:{port}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Gira TKS ({host})",
                    data={**user_input, CONF_GATEWAY_HOST: host},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_GATEWAY_HOST): str,
                vol.Required(CONF_LISTEN_PORT, default=DEFAULT_LISTEN_PORT): vol.All(
                    int, vol.Range(min=1, max=65535)
                ),
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> GiraTksOptionsFlow:
        return GiraTksOptionsFlow()


class GiraTksOptionsFlow(config_entries.OptionsFlow):

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            local_ip = str(user_input.get(CONF_LOCAL_IP, "")).strip()
            if local_ip:
                try:
                    local_ip = str(ipaddress.IPv4Address(local_ip))
                except ipaddress.AddressValueError:
                    errors["base"] = "invalid_local_ip"
            if (not errors and user_input[CONF_SIP_ENABLED]
                    and not _latin1(str(user_input.get(CONF_SIP_USERNAME, "")))):
                errors["base"] = "invalid_sip_username"
            if not errors and user_input[CONF_SIP_ENABLED] and not (
                user_input.get(CONF_SIP_USERNAME) and
                (user_input.get(CONF_SIP_PASSWORD) or self.config_entry.options.get(CONF_SIP_PASSWORD))
            ):
                errors["base"] = "sip_credentials_required"
            elif not errors:
                options = dict(self.config_entry.options)
                options.update(user_input)
                options[CONF_LOCAL_IP] = local_ip
                if not user_input.get(CONF_SIP_PASSWORD):
                    options[CONF_SIP_PASSWORD] = self.config_entry.options.get(CONF_SIP_PASSWORD, "")
                return self.async_create_entry(title="", data=options)

        current = self.config_entry.options
        schema = vol.Schema({
            vol.Required(CONF_SIP_ENABLED, default=current.get(CONF_SIP_ENABLED, False)): bool,
            vol.Optional(CONF_SIP_USERNAME, default=current.get(CONF_SIP_USERNAME, "")): str,
            vol.Optional(CONF_SIP_PASSWORD): _PASSWORD_SELECTOR,
            vol.Optional(CONF_LOCAL_IP, default=current.get(CONF_LOCAL_IP, DEFAULT_LOCAL_IP)): str,
            vol.Required(CONF_SIP_PORT, default=current.get(CONF_SIP_PORT, DEFAULT_SIP_PORT)):
                vol.All(int, vol.Range(min=1, max=65535)),
            vol.Required(CONF_MEDIA_PORT, default=current.get(CONF_MEDIA_PORT, DEFAULT_MEDIA_PORT)):
                vol.All(int, vol.Range(min=1024, max=65534)),
            vol.Required(CONF_AUTO_ANSWER, default=current.get(CONF_AUTO_ANSWER, False)): bool,
            vol.Required(CONF_SIP_SWITCHING, default=current.get(CONF_SIP_SWITCHING, False)): bool,
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

