from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, sip_updated_signal
from .sip import SipClient

_PRESS_ERRORS = (RuntimeError, TimeoutError, OSError, ValueError, IndexError)


def _action_failed(err: BaseException) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="action_failed",
        translation_placeholders={"error": str(err) or type(err).__name__},
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    sip: SipClient | None = runtime.get("sip")
    entities: list[ButtonEntity] = []
    if sip:
        entities += [
            AnswerCallButton(entry, sip),
            DeclineCallButton(entry, sip),
            StartCameraPreviewButton(entry, sip),
            StopCameraPreviewButton(entry, sip),
            RestartSipButton(entry, sip),
        ]
        if runtime.get("sip_switching"):
            entities += [SipDoorOpenerButton(entry, sip), SipLightButton(entry, sip), SipSwitchingActionButton(entry, sip)]
    async_add_entities(entities)


class CallButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, sip: SipClient, suffix: str) -> None:
        self.sip = sip
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=entry.title, manufacturer="Gira", model="TKS-IP Gateway")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, sip_updated_signal(self._entry_id), self._handle_sip_update
            )
        )

    @callback
    def _handle_sip_update(self) -> None:
        self.async_write_ha_state()

    async def _run_action(self, action) -> None:
        try:
            await action()
        except _PRESS_ERRORS as err:
            raise _action_failed(err) from err


class AnswerCallButton(CallButton):
    _attr_translation_key = "answer_call"
    _attr_icon = "mdi:phone-check"

    def __init__(self, entry, sip):
        super().__init__(entry, sip, "answer_call")

    @property
    def available(self) -> bool:
        return bool(
            self.sip.runtime.status == "registered"
            and self.sip.runtime.call_state == "ringing"
            and self.sip.call
            and self.sip.call[2] is not None
            and self.sip.media_active
        )

    async def async_press(self) -> None:
        await self._run_action(self.sip.answer)


class DeclineCallButton(CallButton):
    _attr_translation_key = "decline_call"
    _attr_icon = "mdi:phone-hangup"

    def __init__(self, entry, sip):
        super().__init__(entry, sip, "decline_call")

    async def async_press(self) -> None:
        await self._run_action(self.sip.decline)


class RestartSipButton(CallButton):
    _attr_translation_key = "restart_sip"
    _attr_icon = "mdi:restart"

    def __init__(self, entry, sip):
        super().__init__(entry, sip, "restart_sip")

    async def async_press(self) -> None:
        await self._run_action(self.sip.restart)


class StartCameraPreviewButton(CallButton):
    _attr_translation_key = "start_camera_preview"
    _attr_icon = "mdi:camera-wireless"

    def __init__(self, entry, sip):
        super().__init__(entry, sip, "start_camera_preview")

    async def async_press(self) -> None:
        await self._run_action(self.sip.start_camera_preview)


class StopCameraPreviewButton(CallButton):
    _attr_translation_key = "stop_camera_preview"
    _attr_icon = "mdi:camera-off"

    def __init__(self, entry, sip):
        super().__init__(entry, sip, "stop_camera_preview")

    async def async_press(self) -> None:
        await self._run_action(self.sip.stop_camera_preview)


class _SipSwitchButton(CallButton):

    @property
    def available(self) -> bool:
        return self.sip.runtime.status == "registered"

    async def _send(self) -> None:
        raise NotImplementedError

    async def async_press(self) -> None:
        try:
            await self._send()
        except Exception as err:
            raise _action_failed(err) from err


class SipDoorOpenerButton(_SipSwitchButton):
    _attr_translation_key = "door_opener"
    _attr_icon = "mdi:door-open"

    def __init__(self, entry, sip):
        super().__init__(entry, sip, "sip_door_opener")

    async def _send(self) -> None:
        await self.sip.send_tks_door_opener()


class SipLightButton(_SipSwitchButton):
    _attr_translation_key = "light"
    _attr_icon = "mdi:lightbulb-on"

    def __init__(self, entry, sip):
        super().__init__(entry, sip, "sip_light")

    async def _send(self) -> None:
        await self.sip.send_tks_light()


class SipSwitchingActionButton(_SipSwitchButton):
    _attr_translation_key = "switching_action"
    _attr_icon = "mdi:garage-variant"

    def __init__(self, entry, sip):
        super().__init__(entry, sip, "sip_switching_action")

    async def _send(self) -> None:
        await self.sip.send_tks_switching_action()
