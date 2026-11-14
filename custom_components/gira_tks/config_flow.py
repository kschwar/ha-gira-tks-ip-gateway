from __future__ import annotations

import ipaddress
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.device_registry import format_mac
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
    CONF_MAC,
)
from .discovery import DiscoveredGateway, async_discover_gateways

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _latin1(value: str) -> bool:
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    VERSION = 1

    def __init__(self) -> None:
        self._found: list[DiscoveredGateway] | None = None
        self._suggested: DiscoveredGateway | None = None

    async def async_step_dhcp(self, discovery_info: Any) -> ConfigFlowResult:
        mac = format_mac(discovery_info.macaddress)
        entries = self._async_current_entries(include_ignore=False)
        if any(entry.data.get(CONF_MAC) == mac and entry.data.get(CONF_GATEWAY_HOST) == discovery_info.ip
               for entry in entries):
            return self.async_abort(reason="already_configured")
        gateways = await async_discover_gateways(stop_when=lambda g: g.mac == mac)
        gateway = next((g for g in gateways if g.mac == mac and g.host == discovery_info.ip), None)
        if gateway is None:
            return self.async_abort(reason="not_tks_gateway")
        for entry in entries:
            known_mac = entry.data.get(CONF_MAC)
            if known_mac != gateway.mac and (known_mac or entry.data.get(CONF_GATEWAY_HOST) != gateway.host):
                continue
            if entry.data.get(CONF_GATEWAY_HOST) != gateway.host:
                port = int(entry.data.get(CONF_LISTEN_PORT, DEFAULT_LISTEN_PORT))
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_GATEWAY_HOST: gateway.host},
                    unique_id=f"{gateway.host}:{port}",
                )
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
            elif not known_mac:
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MAC: gateway.mac})
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(f"{gateway.host}:{DEFAULT_LISTEN_PORT}")
        self._abort_if_unique_id_configured()
        self._suggested = gateway
        self.context["title_placeholders"] = {"name": gateway.name or "TKS-IP-Gateway", "host": gateway.host}
        return await self.async_step_user()

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
                data = {**user_input, CONF_GATEWAY_HOST: host}
                mac = self._mac_for(host)
                if mac:
                    data[CONF_MAC] = mac
                return self.async_create_entry(title=f"Gira TKS ({host})", data=data)
        elif self._suggested is None:
            if self._found is None:
                self._found = await async_discover_gateways()
            configured = {entry.data.get(CONF_GATEWAY_HOST) for entry in self._async_current_entries()}
            self._suggested = next((g for g in self._found if g.host not in configured), None)

        host_default = {"default": self._suggested.host} if self._suggested else {}
        schema = vol.Schema(
            {
                vol.Required(CONF_GATEWAY_HOST, **host_default): str,
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

    def _mac_for(self, host: str) -> str | None:
        candidates = [self._suggested, *(self._found or [])]
        return next((g.mac for g in candidates if g is not None and g.host == host), None)

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

