from __future__ import annotations

import logging
import ipaddress
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUTO_ANSWER,
    CONF_GATEWAY_HOST,
    CONF_LISTEN_PORT,
    CONF_LOCAL_IP,
    CONF_MEDIA_PORT,
    CONF_PASSWORD,
    CONF_SIP_ENABLED,
    CONF_SIP_PASSWORD,
    CONF_SIP_PORT,
    CONF_SIP_USERNAME,
    CONF_SIP_SWITCHING,
    CONF_USERNAME,
    DEFAULT_MEDIA_PORT,
    DEFAULT_SIP_PORT,
    DOMAIN,
    EVENT_DOOR_CALL,
    EVENT_FRAME_RECEIVED,
    EVENT_KEY_EVENT,
    PLATFORMS,
)
from .server import GatewayListener
from .sip import LiveH264View, PreviewH264View, SipClient

_LOGGER = logging.getLogger(__name__)
GiraTksConfigEntry = ConfigEntry
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


_NOTIFICATION_TEXTS = {
    "de": {
        "labels": {"light": "Licht", "door_opener": "Türöffner"},
        "message": "{label}-Taste{where} gedrückt um {time}.",
        "where": " am Communicator {comm}",
    },
    "en": {
        "labels": {"light": "Light", "door_opener": "Door opener"},
        "message": "{label} key pressed{where} at {time}.",
        "where": " on Communicator {comm}",
    },
}


def _notification_texts(hass: HomeAssistant) -> dict[str, Any]:
    language = (getattr(hass.config, "language", None) or "").lower()
    return _NOTIFICATION_TEXTS["de" if language.startswith("de") else "en"]


def _notify_key_event(hass: HomeAssistant, entry: ConfigEntry, record: dict[str, Any]) -> None:
    texts = _notification_texts(hass)
    label = texts["labels"].get(record.get("key_event"))
    if label is None:
        return
    when = dt_util.parse_datetime(record.get("received_at") or "")
    local = dt_util.as_local(when) if when else dt_util.now()
    comm = record.get("communicator_number")
    where = texts["where"].format(comm=comm) if comm else ""
    persistent_notification.async_create(
        hass,
        texts["message"].format(label=label, where=where, time=f"{local:%H:%M:%S}"),
        title=f"Gira TKS: {label}",
        notification_id=f"gira_tks_{entry.entry_id}_{record.get('key_event')}",
    )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.http.register_view(LiveH264View)
    hass.http.register_view(PreviewH264View)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: GiraTksConfigEntry
) -> bool:

    def handle_record(record: dict[str, Any]) -> None:
        if record.get("function") == "key_event":
            runtime["last_key_event"] = record
            hass.bus.async_fire(EVENT_KEY_EVENT, {
                "entry_id": entry.entry_id,
                "key_event": record.get("key_event"),
                "communicator_number": record.get("communicator_number"),
                "sender_type": record.get("sender_type"),
                "bus_address": record.get("bus_address"),
                "target_address": record.get("target_address"),
                "received_at": record.get("received_at"),
            })
            _notify_key_event(hass, entry, record)
        elif record.get("function") in ("door_call", "internal_call", "floor_call"):
            runtime["last_call"] = record
            hass.bus.async_fire(EVENT_DOOR_CALL, {
                "entry_id": entry.entry_id,
                "call_type": record.get("function"),
                "bus_address": record.get("bus_address"),
                "aufsatz": record.get("aufsatz"),
                "tastennummer": record.get("tastennummer"),
                "sender_type": record.get("sender_type"),
                "received_at": record.get("received_at"),
            })
        hass.bus.async_fire(EVENT_FRAME_RECEIVED, {"entry_id": entry.entry_id, **record})

    settings = _connection_settings(entry)
    if settings is None:
        _LOGGER.error("Gira TKS entry has incomplete connection settings; remove and re-add the integration with the Gateway credentials")
        return False
    gateway_host, listen_port, username, password = settings
    listener = GatewayListener(
        username=username,
        password=password,
        gateway_host=gateway_host,
        port=listen_port,
        on_record=handle_record,
    )
    runtime: dict[str, Any] = {
        "listener": listener,
        "sip": None,
        "gateway_host": gateway_host,
        "listen_port": listen_port,
        "last_key_event": None,
        "last_call": None,
        "sip_switching": bool(entry.options.get(CONF_SIP_SWITCHING, False)),
    }
    try:
        await listener.start()
    except OSError as err:
        raise ConfigEntryNotReady(f"Gira TKS TCP listener unavailable: {err}") from err

    sip: SipClient | None = None
    try:
        hass.data[DOMAIN][entry.entry_id] = runtime
        if entry.options.get(CONF_SIP_ENABLED, False):
            sip_username = entry.options.get(CONF_SIP_USERNAME, "")
            sip_password = entry.options.get(CONF_SIP_PASSWORD, "")
            if sip_username and sip_password:
                try:
                    sip = SipClient(
                        hass, entry.entry_id, gateway_host, sip_username, sip_password,
                        int(entry.options.get(CONF_SIP_PORT, DEFAULT_SIP_PORT)),
                        int(entry.options.get(CONF_MEDIA_PORT, DEFAULT_MEDIA_PORT)),
                        bool(entry.options.get(CONF_AUTO_ANSWER, False)),
                        str(entry.options.get(CONF_LOCAL_IP, "")).strip() or None,
                    )
                except OSError as err:
                    raise ConfigEntryNotReady(f"No network route to the Gira TKS Gateway: {err}") from err
                runtime["sip"] = sip
                try:
                    await sip.start()
                except OSError as err:
                    _LOGGER.error("Could not start Gira TKS SIP client: %s", err)
                    sip.runtime.status = "error"

        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if sip:
            await sip.stop()
        await listener.stop()
        raise
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: GiraTksConfigEntry
) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime = hass.data[DOMAIN].pop(entry.entry_id)
    sip: SipClient | None = runtime.get("sip")
    if sip:
        await sip.stop()
    listener: GatewayListener = runtime["listener"]
    await listener.stop()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: GiraTksConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _connection_settings(entry: GiraTksConfigEntry) -> tuple[str, int, str, str] | None:
    data = entry.data
    if all(key in data for key in (CONF_GATEWAY_HOST, CONF_LISTEN_PORT, CONF_USERNAME, CONF_PASSWORD)):
        return (str(data[CONF_GATEWAY_HOST]), int(data[CONF_LISTEN_PORT]),
                str(data[CONF_USERNAME]), str(data[CONF_PASSWORD]))

    values = list(data.values())
    host = next((str(value) for value in values if _is_ipv4(str(value))), None)
    port = next((int(value) for value in values if str(value).isdigit() and 1 <= int(value) <= 65535), None)
    remaining = [str(value) for value in values if str(value) != host and str(value) != str(port)]
    if host and port and len(remaining) == 2:
        _LOGGER.warning("Recovered Gira TKS connection fields from an alternative entry layout")
        return host, port, remaining[0], remaining[1]
    return None


def _is_ipv4(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False
