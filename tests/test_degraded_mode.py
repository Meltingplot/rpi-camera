# -*- coding: utf-8 -*-
"""Tests for the degraded no-camera mode.

When ``Picamera2()`` fails at startup (no camera detected), the server must
not crash: it serves an explanatory error page on port 80, answers 503 on the
snapshot and stream endpoints, and the camera probe ends the process once a
camera shows up so systemd restarts it cleanly.

``server.py`` imports the Pi-only ``picamera2``/``libcamera`` stack at module
load, so stub those out before importing the module under test.
"""

import asyncio
import sys
import threading
import urllib.error
import urllib.request
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

from meltingplot.rpi_camera import server as srv  # noqa: E402,I100

MESSAGE = 'no camera found (libcamera detected no camera modules)'


def _get(url):
    """Return (status, body) for a GET, treating HTTP errors as responses."""
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status, response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'replace')


@pytest.fixture
def degraded_servers():
    """Run both HTTP servers wired up the way ``start()`` does on camera failure."""
    frame_buffer = srv.StreamingOutput(rotation=0)
    saved = {
        'http': (srv.HttpHandler.frame_buffer, srv.HttpHandler.page_bytes, srv.HttpHandler.camera_error),
        'stream': (srv.StreamingHandler.frame_buffer, srv.StreamingHandler.camera_error),
    }
    srv.HttpHandler.frame_buffer = frame_buffer
    srv.HttpHandler.page_bytes = srv._camera_error_page_bytes(MESSAGE)
    srv.HttpHandler.camera_error = MESSAGE
    srv.StreamingHandler.frame_buffer = frame_buffer
    srv.StreamingHandler.camera_error = MESSAGE

    http = srv.StreamingServer(('127.0.0.1', 0), srv.HttpHandler)
    stream = srv.StreamingServer(('127.0.0.1', 0), srv.StreamingHandler)
    for s in (http, stream):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        yield http.server_address[1], stream.server_address[1]
    finally:
        for s in (http, stream):
            s.shutdown()
            s.server_close()
        srv.HttpHandler.frame_buffer, srv.HttpHandler.page_bytes, srv.HttpHandler.camera_error = saved['http']
        srv.StreamingHandler.frame_buffer, srv.StreamingHandler.camera_error = saved['stream']


def test_error_page_contains_message_and_reload_poll():
    """The degraded landing page names the error and polls /api/status to reload."""
    page = srv._camera_error_page_bytes(MESSAGE).decode('utf-8')
    assert MESSAGE in page
    assert 'No camera detected' in page
    assert '/api/status' in page


def test_error_page_escapes_html():
    """Exception text is HTML-escaped before being embedded in the page."""
    page = srv._camera_error_page_bytes('<script>alert(1)</script>').decode('utf-8')
    assert '<script>alert(1)' not in page
    assert '&lt;script&gt;' in page


def test_landing_page_serves_error_page(degraded_servers):
    """Port 80's landing page returns 200 with the explanatory error page."""
    http_port, _ = degraded_servers
    status, body = _get('http://127.0.0.1:%d/' % http_port)
    assert status == 200
    assert 'No camera detected' in body
    assert MESSAGE in body


def test_snapshot_returns_503(degraded_servers):
    """The snapshot endpoint refuses immediately instead of stalling 5s."""
    http_port, _ = degraded_servers
    status, _ = _get('http://127.0.0.1:%d/snapshot' % http_port)
    assert status == 503


def test_api_status_reports_camera_error(degraded_servers):
    """/api/status carries camera_error so the error page knows when to reload."""
    http_port, _ = degraded_servers
    status, body = _get('http://127.0.0.1:%d/api/status' % http_port)
    assert status == 200
    assert MESSAGE in body


def test_stream_returns_503(degraded_servers):
    """The MJPEG stream endpoint refuses immediately instead of stalling."""
    _, stream_port = degraded_servers
    status, _ = _get('http://127.0.0.1:%d/webcam' % stream_port)
    assert status == 503


def test_stream_on_http_port_returns_503(degraded_servers):
    """The stream mirrored onto port 80 refuses too, instead of stalling."""
    http_port, _ = degraded_servers
    status, _ = _get('http://127.0.0.1:%d/webcam' % http_port)
    assert status == 503


def test_camera_probe_waits_then_returns_on_detection():
    """The probe loops while no camera is present and returns once one appears."""

    async def scenario():
        with mock.patch.object(srv.Picamera2, 'global_camera_info', return_value=[]):
            task = asyncio.ensure_future(srv._camera_probe(interval=0.01))
            await asyncio.sleep(0.1)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        with mock.patch.object(
            srv.Picamera2,
            'global_camera_info',
            return_value=[{'Model': 'imx708', 'Num': 0}],
        ):
            await asyncio.wait_for(srv._camera_probe(interval=0.01), timeout=2)

    asyncio.run(scenario())


def test_camera_probe_survives_probe_errors():
    """A failing global_camera_info() keeps the probe looping instead of raising."""

    async def scenario():
        with mock.patch.object(srv.Picamera2, 'global_camera_info', side_effect=RuntimeError('boom')):
            task = asyncio.ensure_future(srv._camera_probe(interval=0.01))
            await asyncio.sleep(0.1)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
