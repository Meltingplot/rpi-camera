# -*- coding: utf-8 -*-
"""Tests for configfs descriptor reads and the bmControls drift guard.

Covers reading the UVC gadget's frames/intervals/maxpacket and bmControls back
from configfs, plus the control-bridge validation. These modules are pure (no
picamera2/libcamera), so they import directly.
"""

import logging
import os
from unittest import mock

from meltingplot.rpi_camera import gadget_configfs as gc
from meltingplot.rpi_camera.uvc_controls import UvcControlBridge
from meltingplot.rpi_camera.uvc_gadget import _snap_interval


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        fh.write(content)


def _camera_terminal(base, bm):
    _write(os.path.join(base, 'control/terminal/camera/default/bmControls'), bm)


def _processing_unit(base, bm):
    _write(os.path.join(base, 'control/processing/default/bmControls'), bm)


def test_bit_set():
    """bit_set reports the right little-endian bits and tolerates over-range."""
    bm = bytes([0x1b, 0x12, 0x00])  # D0,D1,D3,D4 | D9,D12
    for d in (0, 1, 3, 4, 9, 12):
        assert gc.bit_set(bm, d), d
    for d in (2, 5, 8, 10, 23):
        assert not gc.bit_set(bm, d), d
    assert not gc.bit_set(bm, 999)  # past the end is just unset, not an error


def test_read_streaming(tmp_path):
    """read_streaming parses frames/intervals/maxpacket, ordered by bFrameIndex."""
    base = str(tmp_path / 'uvc.usb0')
    _write(base + '/streaming_maxpacket', '2048\n')
    # Out-of-order creation: read_streaming must order by bFrameIndex (dir name).
    for w, h, ivs in [(1280, 720, '333333'), (640, 480, '333333 416667 500000')]:
        d = '%s/streaming/mjpeg/m/%04dx%04d' % (base, w, h)
        _write(d + '/wWidth', str(w))
        _write(d + '/wHeight', str(h))
        _write(d + '/dwFrameInterval', ivs)
    frames, intervals, maxpacket = gc.read_streaming(base)
    assert maxpacket == 2048
    assert frames == [(640, 480), (1280, 720)]              # sorted by zero-padded name
    assert intervals == [[333333, 416667, 500000], [333333]]


def test_read_streaming_no_frames_raises(tmp_path):
    """A function with no frame descriptors raises ValueError (caller falls back)."""
    base = str(tmp_path / 'uvc.usb0')
    _write(base + '/streaming_maxpacket', '1024')
    try:
        gc.read_streaming(base)
    except ValueError:
        return
    raise AssertionError('expected ValueError for a function with no frames')


def test_read_controls_parses_hex_and_defaults_ids(tmp_path):
    """read_controls parses the hex bmControls and defaults the unit IDs to 1/2."""
    base = str(tmp_path / 'uvc.usb0')
    _camera_terminal(base, '0x2a 0x00 0x02')
    _processing_unit(base, '0x1b 0x12 0x00')
    cfg = gc.read_controls(base)
    assert cfg['camera'] == bytes([0x2a, 0x00, 0x02])
    assert cfg['processing'] == bytes([0x1b, 0x12, 0x00])
    assert cfg['camera_id'] == 1 and cfg['processing_id'] == 2  # no ID files -> defaults


def test_snap_interval():
    """_snap_interval rounds a request up to an advertised interval, else slowest."""
    ivs = [333333, 666666, 1000000]  # 30 / 15 / 10 fps, ascending
    assert _snap_interval(0, ivs) == 333333          # <=0 -> fastest
    assert _snap_interval(333333, ivs) == 333333     # exact match
    assert _snap_interval(800000, ivs) == 1000000    # in-between -> snap up to supported
    assert _snap_interval(99999999, ivs) == 1000000  # slower than slowest -> slowest


def test_bridge_validation_matches_setup_script(tmp_path, monkeypatch, caplog):
    """The bmControls the setup script writes match the bridge -> no STALL error."""
    base = str(tmp_path / 'uvc.usb0')
    _camera_terminal(base, '0x2a 0x00 0x02')     # CT: D1,D3,D5,D17
    _processing_unit(base, '0x1b 0x12 0x00')     # PU: D0,D1,D3,D4,D9,D12
    monkeypatch.setattr(gc, 'find_uvc_function', lambda: base)
    with caplog.at_level(logging.WARNING):
        UvcControlBridge(mock.MagicMock())
    assert not [r for r in caplog.records if 'STALL' in r.getMessage()]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_bridge_validation_flags_advertised_but_unhandled(tmp_path, monkeypatch, caplog):
    """Advertising a control the bridge can't service must log a STALL error."""
    base = str(tmp_path / 'uvc.usb0')
    _camera_terminal(base, '0x2a 0x00 0x02')
    # PU adds D2 (Hue, selector 0x06) which the bridge does not handle: 0x1f vs 0x1b.
    _processing_unit(base, '0x1f 0x12 0x00')
    monkeypatch.setattr(gc, 'find_uvc_function', lambda: base)
    with caplog.at_level(logging.ERROR):
        UvcControlBridge(mock.MagicMock())
    stalls = [r.getMessage() for r in caplog.records if 'STALL' in r.getMessage()]
    assert stalls and any('0x6' in m for m in stalls), stalls
