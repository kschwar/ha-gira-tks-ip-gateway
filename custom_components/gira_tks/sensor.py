from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_FRAME_TYPE,
    ATTR_RECEIVED_AT,
    ATTR_RECORD,
    DOMAIN,
    EVENT_FRAME_RECEIVED,
    sip_updated_signal,
)
from .server import GatewayListener
from .sip import SipClient

KEY_OPTIONS = ["light", "door_opener", "call_accepted", "call_ended"]
SIP_REGISTRATION_OPTIONS = ["disabled", "starting", "registered", "error", "stopped"]
SIP_CALL_OPTIONS = [
    "disabled",
    "idle",
    "ringing",
    "answered",
    "ended",
    "ended_local",
    "declined",
    "cancelled",
    "missed",
    "video_unavailable",
    "media_timeout",
    "preview_starting",
    "preview_active",
    "preview_ended",
    "preview_cancelled",
    "preview_failed",
    "preview_port_unavailable",
    "preview_interrupted_by_call",
]
LISTENER_OPTIONS = ["listening", "stopped"]


def _enum_value(value: str | None, options: list[str]) -> str | None:
    return value if value in options else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    listener: GatewayListener = runtime["listener"]
    sip: SipClient | None = runtime.get("sip")
    entities: list[SensorEntity] = [
        ListenerStatusSensor(entry, listener), LastFrameSensor(hass, entry, listener),
        KeyEventSensor(hass, entry, runtime), KeyLabelSensor(hass, entry, runtime),
        DoorCallSensor(hass, entry, runtime),
        SipRegistrationSensor(hass, entry, sip), SipCallSensor(hass, entry, sip),
        SipVideoPacketsSensor(hass, entry, sip), SipMessageSensor(hass, entry, sip),
    ]
    async_add_entities(entities)


class _RecordSensor(SensorEntity):

    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, runtime: dict, suffix: str) -> None:
        self._hass, self._runtime, self._entry_id = hass, runtime, entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=entry.title, manufacturer="Gira", model="TKS-IP Gateway")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._hass.bus.async_listen(EVENT_FRAME_RECEIVED, self._handle_event))

    @callback
    def _handle_event(self, event: Any) -> None:
        if event.data.get("entry_id") == self._entry_id:
            self.async_write_ha_state()


class _LastRecordSensor(_RecordSensor):

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _runtime_key = ""
    _fields: tuple[str, ...] = ()

    @property
    def native_value(self) -> datetime | None:
        record = self._runtime.get(self._runtime_key)
        return datetime.fromisoformat(record[ATTR_RECEIVED_AT]) if record else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        record = self._runtime.get(self._runtime_key)
        if not record:
            return None
        return {**{key: record.get(key) for key in self._fields}, ATTR_RECORD: record[ATTR_RECORD]}


class KeyEventSensor(_LastRecordSensor):

    _attr_translation_key = "last_key_event"
    _attr_icon = "mdi:gesture-tap-button"
    _runtime_key = "last_key_event"
    _fields = ("key_event", "communicator_number", "sender_type", "bus_address", "target_address")

    def __init__(self, hass, entry, runtime):
        super().__init__(hass, entry, runtime, "key_event")


class KeyLabelSensor(_RecordSensor):

    _attr_translation_key = "last_key"
    _attr_icon = "mdi:gesture-tap-button"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = KEY_OPTIONS

    def __init__(self, hass, entry, runtime):
        super().__init__(hass, entry, runtime, "key_label")

    @property
    def native_value(self) -> str | None:
        record = self._runtime.get("last_key_event")
        if not record:
            return None
        return _enum_value(record.get("key_event"), KEY_OPTIONS)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        record = self._runtime.get("last_key_event")
        if not record:
            return None
        return {
            "communicator_number": record.get("communicator_number"),
            ATTR_RECEIVED_AT: record.get(ATTR_RECEIVED_AT),
        }


class DoorCallSensor(_LastRecordSensor):

    _attr_translation_key = "last_door_call"
    _attr_icon = "mdi:doorbell"
    _runtime_key = "last_call"
    _fields = ("function", "bus_address", "aufsatz", "tastennummer", "sender_type")

    def __init__(self, hass, entry, runtime):
        super().__init__(hass, entry, runtime, "door_call")


class SipSensor(SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, sip: SipClient | None, suffix: str) -> None:
        self._hass, self._sip = hass, sip
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=entry.title, manufacturer="Gira", model="TKS-IP Gateway")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, sip_updated_signal(self._entry_id), self._handle_sip_update)
        )

    @callback
    def _handle_sip_update(self) -> None:
        self.async_write_ha_state()


class SipRegistrationSensor(SipSensor):
    _attr_translation_key = "sip_registration"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SIP_REGISTRATION_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:account-network"

    def __init__(self, hass, entry, sip):
        super().__init__(hass, entry, sip, "sip_registration")

    @property
    def native_value(self) -> str | None:
        value = self._sip.runtime.status if self._sip else "disabled"
        return _enum_value(value, SIP_REGISTRATION_OPTIONS)


class SipCallSensor(SipSensor):
    _attr_translation_key = "sip_call"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SIP_CALL_OPTIONS
    _attr_icon = "mdi:phone-incoming"

    def __init__(self, hass, entry, sip):
        super().__init__(hass, entry, sip, "sip_call")

    @property
    def native_value(self) -> str | None:
        value = self._sip.runtime.call_state if self._sip else "disabled"
        return _enum_value(value, SIP_CALL_OPTIONS)

    @property
    def extra_state_attributes(self):
        return {"caller": self._sip.runtime.caller, "video_active": self._sip.media_active} if self._sip else None


class SipVideoPacketsSensor(SipSensor):
    _attr_translation_key = "video_rtp_packets"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:video-wireless"

    def __init__(self, hass, entry, sip):
        super().__init__(hass, entry, sip, "sip_video_packets")

    @property
    def native_value(self):
        return self._sip.runtime.video_packets if self._sip else None

    @property
    def extra_state_attributes(self):
        if not self._sip:
            return None
        runtime = self._sip.runtime
        return {
            "rtp_sequence_gaps": runtime.video_sequence_gaps,
            "rtp_timestamp_changes": runtime.video_timestamp_changes,
            "sps_nals_seen": runtime.video_sps_count,
            "pps_nals_seen": runtime.video_pps_count,
            "idr_nals_seen": runtime.video_idr_count,
            "last_h264_nal_type": runtime.video_last_nal_type,
            "decoder_bootstrap_ready": self._sip.ts_channel.buffer_valid and bool(self._sip.sps_pps),
            "http_viewers": runtime.stream_clients,
            "http_requests_while_idle": runtime.stream_inactive_requests,
            "http_queue_drops": runtime.stream_queue_drops,
            "http_bytes_sent": runtime.stream_bytes_sent,
        }


class SipMessageSensor(SipSensor):
    _attr_translation_key = "last_sip_message"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:message-text-outline"

    def __init__(self, hass, entry, sip):
        super().__init__(hass, entry, sip, "sip_message")

    @property
    def native_value(self):
        if not self._sip or not self._sip.runtime.last_message:
            return None
        return self._sip.runtime.last_message.get("prefix") or "MESSAGE"

    @property
    def extra_state_attributes(self):
        return self._sip.runtime.last_message if self._sip else None


class ListenerStatusSensor(SensorEntity):

    _attr_has_entity_name = True
    _attr_translation_key = "tcp_listener"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = LISTENER_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:lan-connect"

    def __init__(self, entry: ConfigEntry, listener: GatewayListener) -> None:
        self._listener = listener
        self._attr_unique_id = f"{entry.entry_id}_listener"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Gira",
            model="TKS-IP Gateway",
        )

    @property
    def native_value(self) -> str:
        return "listening" if self._listener.is_running else "stopped"


class LastFrameSensor(SensorEntity):

    _attr_has_entity_name = True
    _attr_translation_key = "last_record"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:message-processing-outline"

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, listener: GatewayListener
    ) -> None:
        self._hass = hass
        self._listener = listener
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_last_frame"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Gira",
            model="TKS-IP Gateway",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._hass.bus.async_listen(EVENT_FRAME_RECEIVED, self._handle_event)
        )

    @callback
    def _handle_event(self, event: Any) -> None:
        if event.data.get("entry_id") == self._entry_id:
            self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        if self._listener.last_record is None:
            return None
        return f"type_{self._listener.last_record[ATTR_FRAME_TYPE]}"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        record = self._listener.last_record
        if record is None:
            return None
        return {
            **{k: v for k, v in record.items() if k not in ("record_hex", "frame_type")},
            "unrecognized_frames": self._listener.unrecognized_frames,
            "last_error": self._listener.last_error,
        }
