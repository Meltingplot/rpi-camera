# -*- coding: utf-8 -*-
"""Tests for the resolution string parsing used by the reconfig coordinator."""

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

from meltingplot.rpi_camera.reconfig import parse_resolution  # noqa: E402


def test_parse_valid_resolution():
    """A WxH string parses into an (int, int) tuple."""
    assert parse_resolution('1280x720', (0, 0)) == (1280, 720)


def test_parse_is_case_insensitive():
    """An upper-case X separator is accepted."""
    assert parse_resolution('640X480', (0, 0)) == (640, 480)


def test_parse_none_returns_fallback():
    """A missing value falls back."""
    assert parse_resolution(None, (1920, 1080)) == (1920, 1080)


def test_parse_garbage_returns_fallback():
    """An unparseable string falls back."""
    assert parse_resolution('not-a-res', (1280, 720)) == (1280, 720)
