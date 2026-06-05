# -*- coding: utf-8 -*-
"""Tests for the WiFi watchdog ping-target system control.

``controls.py`` imports the Pi-only ``libcamera`` stack at module load, so stub
that out before importing the module under test (mirrors ``test_watchdog.py``).
"""

import subprocess
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


def test_ping_target_reads_value_from_conf(tmp_path, monkeypatch):
    """A PING_TARGET line in the EnvironmentFile is read back verbatim."""
    conf = tmp_path / 'wifi-watchdog.conf'
    conf.write_text('PING_TARGET=192.168.7.1\n')
    monkeypatch.setattr(controls, '_WIFI_WATCHDOG_CONF', str(conf))
    assert controls._wifi_watchdog_ping_target() == '192.168.7.1'


def test_ping_target_empty_when_conf_missing(tmp_path, monkeypatch):
    """No config file means auto-detect (empty string)."""
    monkeypatch.setattr(controls, '_WIFI_WATCHDOG_CONF', str(tmp_path / 'nope.conf'))
    assert controls._wifi_watchdog_ping_target() == ''


def test_ping_target_empty_when_key_absent(tmp_path, monkeypatch):
    """A config file without the key also means auto-detect."""
    conf = tmp_path / 'wifi-watchdog.conf'
    conf.write_text('# no override set\n')
    monkeypatch.setattr(controls, '_WIFI_WATCHDOG_CONF', str(conf))
    assert controls._wifi_watchdog_ping_target() == ''


@pytest.mark.parametrize('bad', ['notanip', '10.0.0.1; rm -rf /', '999.1.1.1', 'host.local'])
def test_set_ping_target_rejects_invalid(bad, monkeypatch):
    """An invalid target raises ValueError and never shells out."""
    called = []
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: called.append(a) or None)
    with pytest.raises(ValueError):
        controls._set_wifi_watchdog_ping_target(bad)
    assert called == []


@pytest.mark.parametrize('good', ['10.42.0.1', '192.168.1.254', 'fe80::1', ''])
def test_set_ping_target_invokes_helper(good, monkeypatch):
    """A valid or empty target is passed straight to the sudo helper."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured['cmd'] = cmd
        return subprocess.CompletedProcess(cmd, 0, '', '')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr(controls.os, 'geteuid', lambda: 1000)  # force sudo prefix
    controls._set_wifi_watchdog_ping_target(good)
    assert captured['cmd'] == [
        'sudo', controls._WIFI_WATCHDOG_CONFIG, 'set-ping-target', good,
    ]


def test_set_ping_target_raises_on_helper_failure(monkeypatch):
    """A non-zero helper exit becomes a RuntimeError (server maps it to 422)."""
    monkeypatch.setattr(controls.os, 'geteuid', lambda: 0)  # root -> no sudo
    monkeypatch.setattr(
        subprocess, 'run',
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, '', 'boom'),
    )
    with pytest.raises(RuntimeError):
        controls._set_wifi_watchdog_ping_target('10.0.0.1')
