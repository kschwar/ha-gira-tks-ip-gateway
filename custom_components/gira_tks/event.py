from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EVENT_DOOR_CALL, EVENT_KEY_EVENT

KEY_EVENT_ENTITIES = (
    ("light", "key_light", "light_key", "mdi:lightbulb-on-outline"),
    ("door_opener", "key_door_opener", "door_opener_key", "mdi:door-open"),
    ("call_accepted", "key_call_accepted", "call_accepted", "mdi:phone-in-talk"),
    ("call_ended", "key_call_ended", "call_ended", "mdi:phone-hangup"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entities: list[EventEntity] = [
        TksKeyEventEntity(entry, key_event, suffix, translation_key, icon)
        for key_event, suffix, translation_key, icon in KEY_EVENT_ENTITIES
    ]
    entities.append(TksDoorCallEventEntity(entry))
    async_add_entities(entities)


class _TksEventEntity(EventEntity):
    _attr_has_entity_name = True
    _bus_event = ""

    def __init__(self, entry: ConfigEntry, suffix: str) -> None:
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title,
            manufacturer="Gira", model="TKS-IP Gateway",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hass.bus.async_listen(self._bus_event, self._handle_bus_event))

    @callback
    def _handle_bus_event(self, event: Event) -> None:
        if event.data.get("entry_id") != self._entry_id:
            return
        result = self._event_for(event.data)
        if result is None:
            return
        event_type, attributes = result
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()

    def _event_for(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        raise NotImplementedError


class TksKeyEventEntity(_TksEventEntity):

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = ["pressed"]
    _bus_event = EVENT_KEY_EVENT

    def __init__(
        self, entry: ConfigEntry, key_event: str, suffix: str, translation_key: str, icon: str
    ) -> None:
        super().__init__(entry, suffix)
        self._key_event = key_event
        self._attr_translation_key = translation_key
        self._attr_icon = icon

    def _event_for(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        if data.get("key_event") != self._key_event:
            return None
        return "pressed", {
            "communicator_number": data.get("communicator_number"),
            "sender_type": data.get("sender_type"),
            "bus_address": data.get("bus_address"),
            "target_address": data.get("target_address"),
        }


class TksDoorCallEventEntity(_TksEventEntity):

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring", "internal_call", "floor_call"]
    _attr_translation_key = "door_call"
    _attr_icon = "mdi:doorbell"
    _bus_event = EVENT_DOOR_CALL
    _EVENT_TYPE_FOR_CALL = {
        "door_call": "ring",
        "internal_call": "internal_call",
        "floor_call": "floor_call",
    }

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "door_call_event")

    def _event_for(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        call_type = data.get("call_type")
        event_type = self._EVENT_TYPE_FOR_CALL.get(call_type)
        if event_type is None:
            return None
        return event_type, {
            "call_type": call_type,
            "bus_address": data.get("bus_address"),
            "aufsatz": data.get("aufsatz"),
            "tastennummer": data.get("tastennummer"),
            "sender_type": data.get("sender_type"),
        }
