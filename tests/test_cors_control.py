# -*- coding: utf-8 -*-
"""Tests for setting the CORS allow-list from the admin UI.

``CorsOrigin`` is a SERVER control: unlike the WiFi-watchdog system toggles it
is persisted to controls.json (nothing else remembers it), and unlike a live
libcamera control it is never handed to ``set_controls`` — a registered server
listener pushes it onto the running request handlers instead.

``controls.py`` imports the Pi-only ``libcamera`` stack at module load, so stub
that out before importing the module under test.
"""

import json
import sys
from unittest import mock

import pytest

for _name in (
    'picamera2',
    'picamera2.encoders',
    'picamera2.outputs',
    'libcamera',
    'piexif',
):
    sys.modules.setdefault(_name, mock.MagicMock())

from meltingplot.rpi_camera import controls  # noqa: E402,I100


@pytest.fixture
def controller(tmp_path, monkeypatch):
    """Build a CameraController over a fake sensor with one live control."""
    # No WiFi watchdog unit on a dev host — keeps SYSTEM controls out of the way.
    monkeypatch.setattr(controls, '_wifi_watchdog_present', lambda: False)
    picam2 = mock.Mock()
    picam2.camera_controls = {'Brightness': (-1.0, 1.0, 0.0)}
    return controls.CameraController(picam2, str(tmp_path / 'controls.json'))


@pytest.mark.parametrize(
    'raw, expected',
    [
        ('', ''),
        ('   ', ''),
        ('*', '*'),
        ('https://Ops.Example.com/', 'https://ops.example.com'),
        ('http://10.42.0.9:8080', 'http://10.42.0.9:8080'),
        (' https://a.example , https://b.example ', 'https://a.example,https://b.example'),
    ],
)
def test_normalise_accepts_and_canonicalises(raw, expected):
    """Case, trailing slashes and spacing are normalised before storage."""
    assert controls._normalise_cors_origin(raw) == expected


@pytest.mark.parametrize(
    'bad',
    [
        'ops.example.com',  # no scheme — the classic mistake
        'https://ops.example.com/embed',  # an origin has no path
        'ftp://ops.example.com',
        'javascript:alert(1)',
    ],
)
def test_normalise_rejects_non_origins(bad):
    """A malformed origin is refused, so the admin is told instead of the browser."""
    with pytest.raises(ValueError):
        controls._normalise_cors_origin(bad)


def test_control_is_offered_without_a_camera_control(controller):
    """The UI gets CorsOrigin even though no sensor advertises it."""
    caps = controller.capabilities()
    assert caps['CorsOrigin']['ui_type'] == 'text'
    assert caps['CorsOrigin']['group'] == 'system'
    # A hint is what makes a raw origin box usable in the admin panel.
    assert caps['CorsOrigin']['hint']


def test_apply_pushes_to_the_listener_and_persists(controller, tmp_path):
    """Applying from the UI reaches the handlers live and survives a restart."""
    seen = []
    controller.register_server_listener(lambda name, value: seen.append((name, value)))

    state = controller.apply({'CorsOrigin': 'https://Ops.Example.com/'})

    assert seen == [('CorsOrigin', 'https://ops.example.com')]
    assert state['CorsOrigin'] == 'https://ops.example.com'
    saved = json.loads((tmp_path / 'controls.json').read_text())
    assert saved['CorsOrigin'] == 'https://ops.example.com'


def test_apply_never_reaches_the_camera(controller):
    """The setting configures the server, so set_controls must never see it."""
    controller.apply({'CorsOrigin': '*'})
    for call in controller._picam2.set_controls.call_args_list:
        assert 'CorsOrigin' not in call.args[0]


def test_invalid_value_is_rejected_before_anything_changes(controller, tmp_path):
    """A bad origin raises (the API turns it into a 400) and changes nothing."""
    seen = []
    controller.register_server_listener(lambda name, value: seen.append((name, value)))
    with pytest.raises(ValueError):
        controller.apply({'CorsOrigin': 'ops.example.com'})
    assert seen == []
    assert 'CorsOrigin' not in controller.get_state()
    assert not (tmp_path / 'controls.json').exists()


def test_persisted_value_is_reapplied_on_startup(controller, tmp_path):
    """A UI change survives a restart and lands back on the handlers."""
    (tmp_path / 'controls.json').write_text(json.dumps({'CorsOrigin': 'https://ops.example.com'}))
    seen = []
    controller.register_server_listener(lambda name, value: seen.append((name, value)))
    controller.load_and_apply_persisted()
    assert seen == [('CorsOrigin', 'https://ops.example.com')]


def test_a_hand_edited_file_does_not_break_startup(controller, tmp_path, caplog):
    """A garbage origin in controls.json is logged and skipped, not fatal."""
    (tmp_path / 'controls.json').write_text(json.dumps({'CorsOrigin': 'nonsense'}))
    controller.load_and_apply_persisted()
    assert 'CorsOrigin' not in controller.get_state()


def test_seed_server_state_yields_to_a_persisted_value(controller, tmp_path):
    """The --cors-origin value shows in the UI, but a saved UI change wins."""
    controller.seed_server_state(CorsOrigin='https://cli.example.com')
    assert controller.get_state()['CorsOrigin'] == 'https://cli.example.com'

    (tmp_path / 'controls.json').write_text(json.dumps({'CorsOrigin': 'https://ui.example.com'}))
    controller.load_and_apply_persisted()
    assert controller.get_state()['CorsOrigin'] == 'https://ui.example.com'


def test_reset_keeps_the_allow_list(controller, tmp_path):
    """Reset to defaults is about the image, not the embedder it would cut off."""
    controller.apply({'CorsOrigin': 'https://ops.example.com', 'Brightness': 0.5})
    state = controller.reset()
    assert state == {'CorsOrigin': 'https://ops.example.com'}
    saved = json.loads((tmp_path / 'controls.json').read_text())
    assert saved == {'CorsOrigin': 'https://ops.example.com'}
