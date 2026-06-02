# -*- coding: utf-8 -*-
"""Tests for the UVC <-> libcamera control bridge (no hardware required)."""

from meltingplot.rpi_camera import uvc_controls as uc
from meltingplot.rpi_camera.uvc_controls import UvcControlBridge


class FakeController:
    """Minimal stand-in for CameraController used by the bridge."""

    def __init__(self, bounds, state=None):
        """Seed sensor bounds and an optional initial applied-state dict."""
        self._bounds = bounds
        self._state = dict(state or {})
        self.applied = []

    def bounds(self, name):
        """Return the canned (min, max, default) for a control, or None."""
        return self._bounds.get(name)

    def current(self, name, fallback=None):
        """Return the current applied value, or the fallback."""
        return self._state.get(name, fallback)

    def apply(self, partial):
        """Record and merge an applied control dict."""
        self._state.update(partial)
        self.applied.append(partial)
        return dict(self._state)


BOUNDS = {
    'Brightness': (-1.0, 1.0, 0.0),
    'Contrast': (0.0, 2.0, 1.0),
    'AnalogueGain': (1.0, 16.0, 1.0),
    'ExposureTime': (100, 1_000_000, 20_000),
    'LensPosition': (0.0, 10.0, 1.0),
}


def _unpack(b, signed=False):
    return int.from_bytes(b, 'little', signed=signed)


def test_brightness_get_min_max_def():
    """Brightness advertises [0, 1000] with the libcamera default at the middle."""
    bridge = UvcControlBridge(FakeController(BOUNDS))
    key = (uc.PROCESSING_UNIT_ID, uc.PU_BRIGHTNESS_CONTROL)
    assert _unpack(bridge.get(*key, uc.GET_MIN), signed=True) == 0
    assert _unpack(bridge.get(*key, uc.GET_MAX), signed=True) == 1000
    assert _unpack(bridge.get(*key, uc.GET_DEF), signed=True) == 500


def test_brightness_set_roundtrip():
    """SET_CUR maps the UVC range back onto the sensor's [min, max]."""
    fc = FakeController(BOUNDS)
    bridge = UvcControlBridge(fc)
    key = (uc.PROCESSING_UNIT_ID, uc.PU_BRIGHTNESS_CONTROL)
    bridge.set_cur(*key, (1000).to_bytes(2, 'little'))
    assert fc._state['Brightness'] == 1.0
    bridge.set_cur(*key, (0).to_bytes(2, 'little'))
    assert fc._state['Brightness'] == -1.0
    bridge.set_cur(*key, (500).to_bytes(2, 'little'))
    assert abs(fc._state['Brightness']) < 1e-9


def test_exposure_uses_100us_units():
    """Exposure time is a 4-byte control in 100 us units."""
    fc = FakeController(BOUNDS)
    bridge = UvcControlBridge(fc)
    key = (uc.CAMERA_TERMINAL_ID, uc.CT_EXPOSURE_TIME_ABSOLUTE_CONTROL)
    snap = bridge.get(*key, uc.GET_DEF)
    assert len(snap) == 4
    assert _unpack(snap) == 200  # 20000 us / 100
    bridge.set_cur(*key, (300).to_bytes(4, 'little'))
    assert fc._state['ExposureTime'] == 30_000


def test_ae_mode_maps_to_aeenable():
    """AE-mode bitmap 1=manual / 2=auto maps to the AeEnable bool."""
    fc = FakeController(BOUNDS, {'AeEnable': True})
    bridge = UvcControlBridge(fc)
    key = (uc.CAMERA_TERMINAL_ID, uc.CT_AE_MODE_CONTROL)
    assert _unpack(bridge.get(*key, uc.GET_CUR)) == 2
    bridge.set_cur(*key, b'\x01')
    assert fc._state['AeEnable'] is False
    bridge.set_cur(*key, b'\x02')
    assert fc._state['AeEnable'] is True


def test_focus_auto_maps_to_afmode():
    """Focus-auto bool maps to AfMode Continuous/Manual."""
    fc = FakeController(BOUNDS, {'AfMode': 'Continuous'})
    bridge = UvcControlBridge(fc)
    key = (uc.CAMERA_TERMINAL_ID, uc.CT_FOCUS_AUTO_CONTROL)
    assert _unpack(bridge.get(*key, uc.GET_CUR)) == 1
    bridge.set_cur(*key, b'\x00')
    assert fc._state['AfMode'] == 'Manual'


def test_wb_auto_maps_to_awbenable():
    """White-balance-temperature-auto bool maps to AwbEnable."""
    fc = FakeController(BOUNDS, {'AwbEnable': False})
    bridge = UvcControlBridge(fc)
    key = (uc.PROCESSING_UNIT_ID, uc.PU_WHITE_BALANCE_TEMPERATURE_AUTO_CONTROL)
    assert _unpack(bridge.get(*key, uc.GET_CUR)) == 0
    bridge.set_cur(*key, b'\x01')
    assert fc._state['AwbEnable'] is True


def test_get_info_and_unknown_control():
    """GET_INFO reports GET+SET; an unadvertised control is not handled."""
    bridge = UvcControlBridge(FakeController(BOUNDS))
    key = (uc.PROCESSING_UNIT_ID, uc.PU_CONTRAST_CONTROL)
    assert bridge.get(*key, uc.GET_INFO) == bytes([0x03])
    assert bridge.handles(*key) is True
    assert bridge.handles(99, 99) is False
    assert bridge.get(99, 99, uc.GET_CUR) is None


def test_missing_libcamera_control_does_not_crash():
    """A control whose libcamera bound is absent still answers GET and accepts SET."""
    fc = FakeController({})
    bridge = UvcControlBridge(fc)
    key = (uc.CAMERA_TERMINAL_ID, uc.CT_FOCUS_ABSOLUTE_CONTROL)
    assert bridge.get(*key, uc.GET_CUR) is not None
    bridge.set_cur(*key, (500).to_bytes(2, 'little'))
