# -*- coding: utf-8 -*-
"""Tests for serving the MJPEG stream on the HTTP (landing page) port.

Corporate networks routinely route only :80 between VLANs, so the stream that
lives on the dedicated stream port must be reachable on the HTTP port too
(``/webcam`` and ``/stream``) — with a slot cap that keeps worker threads free
for the landing page and the control API.

``server.py`` imports the Pi-only ``picamera2``/``libcamera`` stack at module
load, so stub those out before importing the module under test.
"""

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

JPEG = b'\xff\xd8\xff\xdb' + b'x' * 32 + b'\xff\xd9'


class _FrameFeeder(threading.Thread):
    """Push a frame into the buffer every 20ms until stopped."""

    daemon = True

    def __init__(self, frame_buffer):
        super().__init__()
        self._frame_buffer = frame_buffer
        self._done = threading.Event()

    def run(self):
        while not self._done.wait(0.02):
            self._frame_buffer.write(JPEG)

    def stop(self):
        self._done.set()


@pytest.fixture
def http_server():
    """Run the port-80 handler with a live frame feeder, as ``start()`` wires it."""
    frame_buffer = srv.StreamingOutput(rotation=0)
    saved = (
        srv.HttpHandler.frame_buffer,
        srv.HttpHandler.page_bytes,
        srv.HttpHandler.camera_error,
        srv.HttpHandler.coordinator,
        srv.HttpHandler.stream_slots,
    )
    srv.HttpHandler.frame_buffer = frame_buffer
    srv.HttpHandler.page_bytes = b'<html></html>'
    srv.HttpHandler.camera_error = None
    srv.HttpHandler.coordinator = mock.Mock()

    feeder = _FrameFeeder(frame_buffer)
    feeder.start()
    http = srv.StreamingServer(('127.0.0.1', 0), srv.HttpHandler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        yield http.server_address[1]
    finally:
        http.shutdown()
        http.server_close()
        feeder.stop()
        feeder.join(timeout=2)
        (
            srv.HttpHandler.frame_buffer,
            srv.HttpHandler.page_bytes,
            srv.HttpHandler.camera_error,
            srv.HttpHandler.coordinator,
            srv.HttpHandler.stream_slots,
        ) = saved


def _open_stream(port, path='/webcam'):
    """Open the stream endpoint and return the live response object."""
    return urllib.request.urlopen('http://127.0.0.1:%d%s' % (port, path), timeout=3)


@pytest.mark.parametrize('path', ['/webcam', '/stream'])
def test_stream_served_on_http_port(http_server, path):
    """Both aliases deliver a multipart MJPEG stream on the HTTP port."""
    with _open_stream(http_server, path) as response:
        assert response.status == 200
        assert 'multipart/x-mixed-replace' in response.headers['Content-Type']
        chunk = response.read(len(JPEG) + 256)
    assert b'--FRAME' in chunk
    assert JPEG in chunk


def test_stream_reports_consumers_to_coordinator(http_server):
    """A stream client on the HTTP port un-throttles the capture frame rate."""
    coordinator = srv.HttpHandler.coordinator
    with _open_stream(http_server) as response:
        response.read(len(JPEG) + 256)
        assert coordinator.consumer_added.called
    # The handler thread releases the consumer after the client goes away.
    for _ in range(100):
        if coordinator.consumer_removed.called:
            break
        threading.Event().wait(0.02)
    assert coordinator.consumer_removed.called


def test_stream_slots_cap_leaves_threads_for_the_ui(http_server):
    """Beyond the slot cap streaming is refused, and the control API stays served."""
    srv.HttpHandler.stream_slots = threading.Semaphore(1)
    with _open_stream(http_server) as response:
        response.read(len(JPEG))
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _open_stream(http_server)
        assert excinfo.value.code == 503
        # The landing page is still served while a client streams.
        with urllib.request.urlopen('http://127.0.0.1:%d/' % http_server, timeout=3) as page:
            assert page.status == 200


def test_unknown_path_still_404(http_server):
    """Adding stream routes did not turn unknown paths into streams."""
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen('http://127.0.0.1:%d/nope' % http_server, timeout=3)
    assert excinfo.value.code == 404
