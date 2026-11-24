import asyncio
import importlib.util
import sys
import types
from datetime import timedelta
from pathlib import Path
from unittest import mock

from aiohttp import web
import pytest

from test_sip_tks_handshake import sip


@pytest.fixture
def camera(monkeypatch):
    class Camera:
        stream = None

        def __init__(self):
            self.remove_callbacks = []

        async def async_added_to_hass(self):
            pass

        def async_on_remove(self, callback):
            self.remove_callbacks.append(callback)

    class NoURLAvailableError(Exception):
        pass

    exports = {
        "homeassistant": {},
        "homeassistant.components": {},
        "homeassistant.components.camera": {
            "Camera": Camera,
            "CameraEntityFeature": types.SimpleNamespace(STREAM=2),
        },
        "homeassistant.components.http": {},
        "homeassistant.components.http.auth": {
            "async_sign_path": mock.Mock(
                side_effect=lambda hass, path, expiration, **kwargs: path + "?authSig=synthetic"
            ),
        },
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.core": {"HomeAssistant": object, "callback": lambda fn: fn},
        "homeassistant.helpers": {},
        "homeassistant.helpers.device_registry": {"DeviceInfo": dict},
        "homeassistant.helpers.dispatcher": {"async_dispatcher_connect": mock.Mock()},
        "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
        "homeassistant.helpers.event": {"async_track_time_interval": mock.Mock()},
        "homeassistant.helpers.network": {
            "NoURLAvailableError": NoURLAvailableError,
            "get_url": mock.Mock(return_value="http://ha.example:8123/"),
        },
    }
    for name, attributes in exports.items():
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)

    path = Path(__file__).resolve().parents[1] / "custom_components/gira_tks/camera.py"
    spec = importlib.util.spec_from_file_location("gira_tks_test.camera", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name,filename", [
    ("GiraTksCamera", "live.ts"),
    ("SipPreviewCamera", "preview.ts"),
])
def test_camera_source_is_signed_and_expires(camera, name, filename):
    hass = mock.Mock()
    entry = types.SimpleNamespace(entry_id="entry", title="Gateway")
    entity = getattr(camera, name)(hass, entry, mock.Mock())
    url = asyncio.run(entity.stream_source())
    assert url == f"http://ha.example:8123/api/gira_tks/entry/{filename}?authSig=synthetic"
    camera.async_sign_path.assert_called_once_with(
        hass, f"/api/gira_tks/entry/{filename}", timedelta(minutes=5),
        use_content_user=True,
    )


@pytest.mark.parametrize("ssl,port,expected", [
    (None, 8123, "http://127.0.0.1:8123"),
    ("certificate.pem", 8443, "https://127.0.0.1:8443"),
])
def test_fallback_url_is_also_signed(camera, ssl, port, expected):
    camera.get_url.side_effect = camera.NoURLAvailableError
    hass = types.SimpleNamespace(http=types.SimpleNamespace(server_port=port, ssl_certificate=ssl))
    assert camera._local_stream_url(hass, "entry", "live.ts") == (
        expected + "/api/gira_tks/entry/live.ts?authSig=synthetic"
    )
    assert camera.async_sign_path.call_args.args[2] == timedelta(minutes=5)


@pytest.mark.parametrize("name,filename", [
    ("GiraTksCamera", "live.ts"),
    ("SipPreviewCamera", "preview.ts"),
])
def test_cached_source_is_renewed_before_expiration_and_timer_removed(camera, name, filename):
    entry = types.SimpleNamespace(entry_id="entry", title="Gateway")
    entity = getattr(camera, name)(mock.Mock(), entry, mock.Mock())
    cancel_timer = mock.Mock()
    camera.async_track_time_interval.return_value = cancel_timer
    asyncio.run(entity.async_added_to_hass())
    camera.async_track_time_interval.assert_called_once_with(
        entity.hass, entity._refresh_stream_url, timedelta(minutes=4)
    )
    entity._refresh_stream_url(None)
    camera.async_sign_path.assert_not_called()
    entity.stream = mock.Mock()
    camera.async_sign_path.side_effect = [
        f"/api/gira_tks/entry/{filename}?authSig=first",
        f"/api/gira_tks/entry/{filename}?authSig=second",
    ]
    entity._refresh_stream_url(None)
    entity._refresh_stream_url(None)
    assert entity.stream.source == f"http://ha.example:8123/api/gira_tks/entry/{filename}?authSig=second"
    entity.stream.update_source.assert_not_called()  # would restart a running stream
    for callback in entity.remove_callbacks:
        if callable(callback):
            callback()
    cancel_timer.assert_called_once()


@pytest.mark.parametrize("view,filename", [
    (sip.LiveTsView, "live.ts"),
    (sip.PreviewTsView, "preview.ts"),
])
def test_both_routes_require_ha_authentication(view, filename):
    assert view.requires_auth is True
    assert view.url == f"/api/gira_tks/{{entry_id}}/{filename}"


def test_missing_gateway_returns_not_found(monkeypatch):
    monkeypatch.setattr(sip, "web", web)
    request = types.SimpleNamespace(app={"hass": types.SimpleNamespace(data={})})
    with pytest.raises(web.HTTPNotFound):
        sip._stream_manager(request, "missing")


@pytest.mark.parametrize("view", [sip.LiveTsView, sip.PreviewTsView])
def test_inactive_stream_returns_service_unavailable(monkeypatch, view):
    monkeypatch.setattr(sip, "web", web)
    manager = mock.Mock(media_active=False)
    manager.runtime.stream_inactive_requests = 0
    manager.ensure_preview_for_viewer = mock.AsyncMock(return_value=False)
    request = types.SimpleNamespace(app={"hass": types.SimpleNamespace(
        data={sip.DOMAIN: {"entry": {"sip": manager}}}
    )})
    with pytest.raises(web.HTTPServiceUnavailable):
        asyncio.run(view().get(request, "entry"))
    assert manager.runtime.stream_inactive_requests == 1


@pytest.mark.parametrize("view", [sip.LiveTsView, sip.PreviewTsView])
def test_active_stream_reaches_existing_video_pipeline(monkeypatch, view):
    manager = mock.Mock(media_active=True)
    manager.ensure_preview_for_viewer = mock.AsyncMock(return_value=True)
    request = types.SimpleNamespace(app={"hass": types.SimpleNamespace(
        data={sip.DOMAIN: {"entry": {"sip": manager}}}
    )})
    serve = mock.AsyncMock(return_value=object())
    monkeypatch.setattr(sip, "_serve_ts", serve)
    assert asyncio.run(view().get(request, "entry")) is serve.return_value
    serve.assert_awaited_once_with(manager, request)
