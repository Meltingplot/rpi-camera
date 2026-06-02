# -*- coding: utf-8 -*-
"""Tests for the per-board default capture resolution.

``server.py`` imports the Pi-only ``picamera2``/``libcamera`` stack at
module load, so stub those out before importing the module under test.
That keeps these pure-logic tests runnable on any machine (CI included).
"""

import sys
from unittest import mock

for _name in (
    'picamera2',
    'picamera2.encoders',
    'picamera2.outputs',
    'libcamera',
    'piexif',
):
    sys.modules.setdefault(_name, mock.MagicMock())

from meltingplot.rpi_camera.server import _default_resolution  # noqa: E402


def test_pi_zero_w_defaults_to_720p():
    """The single-core Pi Zero W falls back to 720p."""
    assert _default_resolution('Raspberry Pi Zero W Rev 1.1') == (1280, 720)


def test_pi_zero_v1_defaults_to_720p():
    """The original (non-W) Pi Zero is also single-core ARMv6 -> 720p."""
    assert _default_resolution('Raspberry Pi Zero') == (1280, 720)


def test_pi_zero_2_w_defaults_to_1080p():
    """The quad-core Pi Zero 2 W is excluded from the 720p case."""
    assert _default_resolution('Raspberry Pi Zero 2 W') == (1920, 1080)


def test_pi_3_defaults_to_1080p():
    """The Pi 3 family defaults to 1080p."""
    assert _default_resolution('Raspberry Pi 3 Model B Plus Rev 1.3') == (1920, 1080)


def test_pi_4_defaults_to_1080p():
    """The Pi 4 defaults to 1080p."""
    assert _default_resolution('Raspberry Pi 4 Model B Rev 1.4') == (1920, 1080)


def test_unknown_model_defaults_to_1080p():
    """An unreadable / empty model string defaults to 1080p."""
    assert _default_resolution('') == (1920, 1080)
