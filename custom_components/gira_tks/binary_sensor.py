from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, sip_updated_signal
from .sip import SipClient


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    sip: SipClient | None = hass.data[DOMAIN][entry.entry_id].get("sip")
    async_add_entities([TksRingingBinarySensor(hass, entry, sip)])


class TksRingingBinarySensor(BinarySensorEntity):

    _attr_has_entity_name = True
    _attr_translation_key = "ring_active"
    _attr_icon = "mdi:bell-ring"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, sip: SipClient | None) -> None:
        self._entry_id = entry.entry_id
        self._sip = sip
        self._attr_unique_id = f"{entry.entry_id}_ringing"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Gira",
            model="TKS-IP Gateway",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, sip_updated_signal(self._entry_id), self._handle_sip_update
            )
        )

    @callback
    def _handle_sip_update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return bool(self._sip and self._sip.runtime.call_state == "ringing")

    @property
    def available(self) -> bool:
        return self._sip is not None and self._sip.runtime.status == "registered"
