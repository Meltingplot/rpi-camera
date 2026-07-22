# -*- coding: utf-8 -*-
"""Tests for the idle frame-rate throttling in ReconfigCoordinator.

While no consumer is streaming (no HTTP MJPEG client, no UVC host) the
coordinator applies ``idle_fps`` instead of the configured frame rate; the
first consumer restores the configured rate and the last one leaving drops
back. Capture never stops — only the FrameRate control changes.

``reconfig.py`` imports the Pi-only ``picamera2``/``libcamera`` stack at
module load, so stub those out before importing the module under test.
"""

import sys
import time
from unittest import mock

for _name in (
    'picamera2',
    'picamera2.encoders',
    'picamera2.outputs',
    'libcamera',
    'piexif',
):
    sys.modules.setdefault(_name, mock.MagicMock())

from meltingplot.rpi_camera import reconfig  # noqa: E402,I100


def _wait_for(predicate, timeout=2.0):
    """Poll ``predicate`` until it is truthy or ``timeout`` seconds elapsed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _last_framerate(picam2):
    """Return the FrameRate from the most recent set_controls() call, or None."""
    for call in reversed(picam2.set_controls.call_args_list):
        controls = call.args[0] if call.args else {}
        if isinstance(controls, dict) and 'FrameRate' in controls:
            return controls['FrameRate']
    return None


def _make_coordinator(idle_fps=1):
    picam2 = mock.MagicMock()
    coordinator = reconfig.ReconfigCoordinator(
        picam2,
        mock.MagicMock(),
        autofocus=False,
        enable_uvc=False,
        idle_fps=idle_fps,
    )
    return coordinator, picam2


def test_bring_up_without_consumers_starts_at_idle_fps():
    """With no consumer connected, the initial frame rate is the idle rate."""
    coordinator, picam2 = _make_coordinator(idle_fps=1)
    coordinator.bring_up(1280, 720, 15)
    assert _last_framerate(picam2) == 1


def test_first_consumer_restores_configured_fps():
    """The first consumer switches the camera back to the configured rate."""
    coordinator, picam2 = _make_coordinator(idle_fps=1)
    coordinator.bring_up(1280, 720, 15)
    coordinator.consumer_added()
    assert _wait_for(lambda: _last_framerate(picam2) == 15)


def test_last_consumer_drops_back_to_idle():
    """Only the LAST consumer leaving lowers the rate; earlier leavers do not."""
    coordinator, picam2 = _make_coordinator(idle_fps=1)
    coordinator.bring_up(1280, 720, 15)
    coordinator.consumer_added()
    coordinator.consumer_added()
    assert _wait_for(lambda: _last_framerate(picam2) == 15)

    coordinator.consumer_removed()
    time.sleep(0.1)  # give a (wrong) idle update a chance to surface
    assert _last_framerate(picam2) == 15

    coordinator.consumer_removed()
    assert _wait_for(lambda: _last_framerate(picam2) == 1)


def test_idle_fps_is_capped_at_configured_fps():
    """An idle rate above the configured rate never raises the frame rate."""
    coordinator, picam2 = _make_coordinator(idle_fps=5)
    coordinator.bring_up(1280, 720, 2)
    assert _last_framerate(picam2) == 2


def test_idle_fps_zero_disables_throttling():
    """idle_fps=0 keeps the configured rate with and without consumers."""
    coordinator, picam2 = _make_coordinator(idle_fps=0)
    coordinator.bring_up(1280, 720, 15)
    assert _last_framerate(picam2) == 15
    coordinator.consumer_added()
    coordinator.consumer_removed()
    time.sleep(0.1)
    assert _last_framerate(picam2) == 15


def test_uvc_host_counts_as_consumer():
    """A USB host opening/closing the UVC stream toggles the frame rate too."""
    coordinator, picam2 = _make_coordinator(idle_fps=1)
    coordinator.bring_up(1280, 720, 15)
    coordinator._on_stream_state(True)
    assert _wait_for(lambda: _last_framerate(picam2) == 15)
    coordinator._on_stream_state(False)
    assert _wait_for(lambda: _last_framerate(picam2) == 1)


def test_fps_only_change_while_idle_applies_idle_rate():
    """A UI FrameRate change while idle records the target but stays throttled."""
    coordinator, picam2 = _make_coordinator(idle_fps=1)
    coordinator.bring_up(1280, 720, 15)
    coordinator.on_change({'Resolution': '1280x720', 'FrameRate': 30}, {'FrameRate'})
    # _fps is updated after the frame rate was applied, so this is race-free.
    assert _wait_for(lambda: coordinator._fps == 30)
    assert _last_framerate(picam2) == 1
    coordinator.consumer_added()
    assert _wait_for(lambda: _last_framerate(picam2) == 30)
