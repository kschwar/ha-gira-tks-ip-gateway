from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DOMAIN, sip_updated_signal
from .sip import SipClient


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    sip: SipClient | None = hass.data[DOMAIN][entry.entry_id].get("sip")
    if sip is None:
        return
    async_add_entities([GiraTksCamera(hass, entry, sip), SipPreviewCamera(hass, entry, sip)])


class _SipCamera(Camera):

    _attr_has_entity_name = True
    _attr_supported_features = CameraEntityFeature.STREAM
    content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, sip: SipClient, suffix: str) -> None:
        super().__init__()
        self.hass = hass
        self.entry = entry
        self.sip = sip
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title,
            manufacturer="Gira", model="TKS-IP Gateway",
        )
        self.stream_options = {"use_wallclock_as_timestamps": True}

    @property
    def available(self) -> bool:
        return self.sip.runtime.status == "registered"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, sip_updated_signal(self.entry.entry_id), self._handle_sip_update
            )
        )

    @callback
    def _handle_sip_update(self) -> None:
        self.async_write_ha_state()

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return None


class GiraTksCamera(_SipCamera):

    _attr_translation_key = "door_station"
    _attr_icon = "mdi:doorbell-video"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, sip: SipClient) -> None:
        super().__init__(hass, entry, sip, "camera")

    @property
    def use_stream_for_stills(self) -> bool:
        return True

    @property
    def is_streaming(self) -> bool:
        return self.sip.media_active

    async def stream_source(self) -> str | None:
        return _local_stream_url(self.hass, self.entry.entry_id, self.sip.stream_token, "live.h264")


class SipPreviewCamera(_SipCamera):

    _attr_translation_key = "camera_preview"
    _attr_icon = "mdi:camera-wireless"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, sip: SipClient) -> None:
        super().__init__(hass, entry, sip, "sip_preview_camera")

    @property
    def is_streaming(self) -> bool:
        return self.sip.runtime.call_state == "preview_active" and self.sip.media_active

    async def stream_source(self) -> str | None:
        return _local_stream_url(self.hass, self.entry.entry_id, self.sip.stream_token, "preview.h264")


def _local_stream_url(hass: HomeAssistant, entry_id: str, token: str, name: str) -> str:
    try:
        base = get_url(hass, allow_internal=True, prefer_external=False)
    except NoURLAvailableError:
        http = getattr(hass, "http", None)
        port = getattr(http, "server_port", None) or 8123
        scheme = "https" if getattr(http, "ssl_certificate", None) else "http"
        base = f"{scheme}://127.0.0.1:{port}"
    return f"{base.rstrip('/')}/api/gira_tks/{entry_id}/{token}/{name}"
