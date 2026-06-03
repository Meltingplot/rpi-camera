# -*- coding: utf-8 -*-
"""Tests for the two-stage frame-stall watchdog escalation.

``server.py`` imports the Pi-only ``picamera2``/``libcamera`` stack at module
load, so stub those out before importing the module under test (mirrors
``test_server_resolution.py``).
"""

import os
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

from meltingplot.rpi_camera import server  # noqa: E402


def _marker_path(tmp_path):
    return str(tmp_path / 'frame-stall')


def test_stage1_restarts_service_and_records_marker(tmp_path, monkeypatch):
    """First stall with no recent restart -> restart the service, write marker."""
    marker = _marker_path(tmp_path)
    monkeypatch.setattr(server, '_STALL_MARKER', marker)
    with mock.patch.object(server.subprocess, 'run') as run:
        action = server._escalate_stall(now=1000.0, restart_window=300)
    assert action == 'restart'
    run.assert_called_once_with(
        ['sudo', 'systemctl', 'restart', 'rpi-camera.service'], check=False,
    )
    # The attempt is persisted so the next process can escalate.
    assert os.path.exists(marker)
    assert server._read_restart_marker() == 1000.0


def test_stage2_reboots_when_restart_did_not_help(tmp_path, monkeypatch):
    """A recent restart that left frames stalled -> reboot, marker cleared."""
    marker = _marker_path(tmp_path)
    monkeypatch.setattr(server, '_STALL_MARKER', marker)
    with open(marker, 'w') as fh:
        fh.write('1000.0')  # restart issued at t=1000
    with mock.patch.object(server.subprocess, 'run') as run:
        action = server._escalate_stall(now=1100.0, restart_window=300)  # 100s later
    assert action == 'reboot'
    run.assert_called_once_with(['sudo', 'reboot'], check=False)
    assert not os.path.exists(marker)  # cleared (also gone after the reboot)


def test_stale_marker_restarts_again(tmp_path, monkeypatch):
    """Stale marker -> the earlier restart worked, so restart again.

    A marker older than the window means a new, unrelated stall should start at
    stage 1 rather than rebooting.
    """
    marker = _marker_path(tmp_path)
    monkeypatch.setattr(server, '_STALL_MARKER', marker)
    with open(marker, 'w') as fh:
        fh.write('1000.0')
    with mock.patch.object(server.subprocess, 'run') as run:
        action = server._escalate_stall(now=2000.0, restart_window=300)  # 1000s later
    assert action == 'restart'
    run.assert_called_once_with(
        ['sudo', 'systemctl', 'restart', 'rpi-camera.service'], check=False,
    )
    assert server._read_restart_marker() == 2000.0  # refreshed


def test_marker_write_failure_falls_back_to_reboot(tmp_path, monkeypatch):
    """Marker write failure -> reboot directly.

    If the marker can't be persisted, stage 2 could never fire, so reboot rather
    than risk an unbounded restart loop.
    """
    # Point the marker at a path whose parent is a file, so open('w') fails.
    not_a_dir = tmp_path / 'blocker'
    not_a_dir.write_text('x')
    monkeypatch.setattr(server, '_STALL_MARKER', str(not_a_dir / 'frame-stall'))
    with mock.patch.object(server.subprocess, 'run') as run:
        action = server._escalate_stall(now=1000.0, restart_window=300)
    assert action == 'reboot'
    run.assert_called_once_with(['sudo', 'reboot'], check=False)
