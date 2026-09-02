# -*- coding: utf-8 -*-
"""Tests for the cross-origin headers that let another site embed the stream.

An ``<iframe>``/``<img>`` on a foreign page works without any of this — the
server sends no ``X-Frame-Options`` and no ``frame-ancestors`` CSP. What needs
``Access-Control-Allow-Origin`` is *reading* the pixels, i.e. drawing the stream
into a ``<canvas>``, plus ``fetch()`` against the snapshot and control API.

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

EMBEDDER = 'https://ops.example.com'


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
    """Run the port-80 handler with live frames; yields (port, set_cors)."""
    frame_buffer = srv.StreamingOutput(rotation=0)
    saved = (
        srv.HttpHandler.frame_buffer,
        srv.HttpHandler.page_bytes,
        srv.HttpHandler.camera_error,
        srv.HttpHandler.coordinator,
        srv.HttpHandler.cors_origins,
    )
    srv.HttpHandler.frame_buffer = frame_buffer
    srv.HttpHandler.page_bytes = b'<html></html>'
    srv.HttpHandler.camera_error = None
    srv.HttpHandler.coordinator = mock.Mock()
    srv.HttpHandler.cors_origins = ()

    feeder = _FrameFeeder(frame_buffer)
    feeder.start()
    http = srv.StreamingServer(('127.0.0.1', 0), srv.HttpHandler)
    threading.Thread(target=http.serve_forever, daemon=True).start()

    def set_cors(value):
        """Configure the handler the way ``start()`` does from --cors-origin."""
        srv.HttpHandler.cors_origins = srv._parse_cors_origins(value)

    try:
        yield http.server_address[1], set_cors
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
            srv.HttpHandler.cors_origins,
        ) = saved


def _request(port, path='/', origin=None, method='GET'):
    """Perform a request and return the response object (closed, headers kept)."""
    req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path), method=method)
    if origin is not None:
        req.add_header('Origin', origin)
    with urllib.request.urlopen(req, timeout=3) as response:
        response.read(len(JPEG) + 256)
        return response


def test_parse_cors_origins_normalises_the_list():
    """Whitespace, trailing slashes and case are normalised; empties dropped."""
    assert srv._parse_cors_origins(None) == ()
    assert srv._parse_cors_origins('') == ()
    assert srv._parse_cors_origins('  ,  ') == ()
    assert srv._parse_cors_origins('*') == ('*', )
    assert srv._parse_cors_origins(
        ' https://A.example.com/ , https://b.example.com ',
    ) == ('https://a.example.com', 'https://b.example.com')


@pytest.mark.parametrize('path', ['/', '/snapshot', '/webcam'])
def test_no_cors_headers_by_default(http_server, path):
    """Without --cors-origin the responses are unchanged: no CORS headers at all."""
    port, _ = http_server
    response = _request(port, path, origin=EMBEDDER)
    assert response.headers.get('Access-Control-Allow-Origin') is None
    assert response.headers.get('Cross-Origin-Resource-Policy') is None
    assert response.headers.get('Vary') is None


@pytest.mark.parametrize('path', ['/', '/snapshot', '/webcam', '/api/status'])
def test_wildcard_allows_every_origin(http_server, path):
    """``*`` marks page, snapshot, stream and API readable from any site."""
    port, set_cors = http_server
    set_cors('*')
    response = _request(port, path, origin=EMBEDDER)
    assert response.headers['Access-Control-Allow-Origin'] == '*'
    # Needed by embedders that opt into cross-origin isolation (COEP).
    assert response.headers['Cross-Origin-Resource-Policy'] == 'cross-origin'
    # A wildcard response is identical for everyone, so it stays cacheable.
    assert response.headers.get('Vary') is None


def test_allow_list_echoes_only_configured_origins(http_server):
    """A listed origin is echoed back; anything else gets no allow header."""
    port, set_cors = http_server
    set_cors('%s/, https://other.example.com' % EMBEDDER)

    allowed = _request(port, '/webcam', origin=EMBEDDER)
    assert allowed.headers['Access-Control-Allow-Origin'] == EMBEDDER
    assert allowed.headers['Cross-Origin-Resource-Policy'] == 'cross-origin'

    denied = _request(port, '/webcam', origin='https://evil.example.com')
    assert denied.headers.get('Access-Control-Allow-Origin') is None
    assert denied.headers.get('Cross-Origin-Resource-Policy') is None

    # Both answers must carry Vary so a cache never serves one to the other.
    assert allowed.headers['Vary'] == 'Origin'
    assert denied.headers['Vary'] == 'Origin'


def test_error_responses_carry_the_headers_too(http_server):
    """503/404 replies stay readable cross-origin, so the embedder sees the reason."""
    port, set_cors = http_server
    set_cors('*')
    srv.HttpHandler.camera_error = 'no camera found'
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _request(port, '/snapshot', origin=EMBEDDER)
    assert excinfo.value.code == 503
    assert excinfo.value.headers['Access-Control-Allow-Origin'] == '*'


def test_preflight_answers_the_control_api(http_server):
    """A cross-origin control POST is preflighted; OPTIONS must approve it."""
    port, set_cors = http_server
    set_cors(EMBEDDER)
    response = _request(port, '/api/controls', origin=EMBEDDER, method='OPTIONS')
    assert response.status == 204
    assert response.headers['Access-Control-Allow-Origin'] == EMBEDDER
    assert 'POST' in response.headers['Access-Control-Allow-Methods']
    assert 'Content-Type' in response.headers['Access-Control-Allow-Headers']


def test_preflight_refused_when_cors_is_off(http_server):
    """With CORS disabled OPTIONS is simply not an allowed method."""
    port, _ = http_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _request(port, '/api/controls', origin=EMBEDDER, method='OPTIONS')
    assert excinfo.value.code == 405
    assert excinfo.value.headers.get('Access-Control-Allow-Origin') is None


def test_ui_change_reaches_both_handlers_without_a_restart():
    """The admin UI's CorsOrigin lands on the live handlers via the server listener."""
    saved = (srv.HttpHandler.cors_origins, srv.StreamingHandler.cors_origins)
    try:
        srv._server_control_listener('CorsOrigin', 'https://Ops.Example.com/')
        assert srv.HttpHandler.cors_origins == ('https://ops.example.com', )
        assert srv.StreamingHandler.cors_origins == ('https://ops.example.com', )
        # Clearing the box in the UI turns embedding back off, also live.
        srv._server_control_listener('CorsOrigin', '')
        assert srv.HttpHandler.cors_origins == ()
        assert srv.StreamingHandler.cors_origins == ()
    finally:
        srv.HttpHandler.cors_origins, srv.StreamingHandler.cors_origins = saved


def test_stream_port_handler_shares_the_configuration():
    """The dedicated stream port gets the same headers, and is GET-only."""
    assert issubclass(srv.StreamingHandler, srv._CorsMixin)
    handler = srv.StreamingHandler.__new__(srv.StreamingHandler)
    handler.cors_origins = ('*', )
    assert handler._allowed_origin() == '*'
    assert handler._allowed_methods() == 'GET, OPTIONS'
