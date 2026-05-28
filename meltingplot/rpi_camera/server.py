# -*- coding: utf-8 -*-
"""
This script sets up an HTTP server to stream video from a Raspberry Pi camera using the Picamera2 library.

It is based on the official Picamera2 example for streaming video from a Raspberry Pi camera using the MJPEG format.

https://github.com/raspberrypi/picamera2/blob/main/examples/mjpeg_server_2.py

Which is licensed under the BSD 2-Clause License (https://github.com/raspberrypi/picamera2/blob/main/LICENSE)

It provides both a web interface for viewing the stream and an endpoint for fetching the current frame as a JPEG image.
Classes:
    StreamingOutput: A class that buffers the video frames and notifies waiting threads when a new frame is available.
    StreamingHandler: A request handler for serving the MJPEG stream.
    HttpHandler: A request handler for serving the HTML page and current frame as a JPEG image.
    StreamingServer: A server class that supports threading and reuses addresses.
Functions:
    main: The main function that configures the camera, starts recording, and sets up the HTTP servers.
HTML Page:
    The HTML page (loaded from package data web/index.html) hosts the live
    stream view and a control panel that talks to /api/controls.
Endpoints (port 80):
    / or /index.html: Serves the HTML page.
    /picture/1/current/ or /snapshot: Serves the current frame as a JPEG image.
    GET /api/controls: Returns curated capabilities and currently applied state.
    POST /api/controls: Applies a partial control dict, persists it to JSON.
    POST /api/controls/reset: Restores every control to its sensor default.
    POST /api/autofocus: Runs a one-shot autofocus cycle.
Endpoints (port 8081):
    / or /webcam: Serves the MJPEG stream.
Usage:
    Run the script to start the HTTP servers on ports 80 and 8081.
"""

import asyncio
import importlib.resources
import io
import json
import logging
import os
import signal
import socketserver
import subprocess
from http import server
from threading import Condition, Semaphore
from urllib.parse import urlparse

import click

from libcamera import Transform, controls

from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

import piexif

from .controls import CameraController

# Fallback HTML for environments where the package data isn't installed
# (e.g. someone running the source tree directly without `pip install -e .`).
# The full UI lives in meltingplot/rpi_camera/web/index.html and is loaded
# from package data at startup; this stub is only the legacy minimal page.
_FALLBACK_PAGE = """\
<html>
<head>
<title>Meltingplot RPi Camera MJPEG streaming</title>
</head>
<body>
<h1>Meltingplot RPi Camera</h1>
<img src="data:image/png;base64,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" width=160 height=66></img>
<p><a href="picture/1/current/">Screenshot URL</a> <span>picture/1/current/</span></p>
<p><a id="streamLink" href="">Stream URL</a> <span>Streaming URL hostname:__STREAM_PORT__</span></p>
<script type="text/javascript">
    document.addEventListener("DOMContentLoaded", function() {
        var hostname = window.location.hostname;
        var streamLink = document.getElementById("streamLink");
        streamLink.href = "http://" + hostname + ":__STREAM_PORT__";
    });
</script>
</body>
</html>
"""  # noqa:E501


class StreamingOutput(io.BufferedIOBase):
    """A class that buffers the video frames and notifies waiting threads when a new frame is available."""

    def __init__(self, rotation: int = 0):
        """
        Initialize the streaming output with a frame buffer and condition.

        0° and 180° are applied by the sensor (see :attr:`hw_transform`) and
        therefore need no EXIF tag. 90°/270° cannot be done in the sensor and
        are offloaded to the client via an EXIF Orientation tag.

        Args:
            rotation (int): The rotation angle for the JPEG image. Must be one of
                            [0, 90, 180, 270]. Default is 0.

        Raises:
            ValueError: If the rotation value is not one of [0, 90, 180, 270].
        """
        if rotation not in (0, 90, 180, 270):
            raise ValueError("Invalid rotation value")

        self.frame = None
        self.condition = Condition()
        self.frame_counter = 0
        self.rotation = rotation

        # EXIF Orientation tag — only needed for rotations the sensor can't do.
        # See http://sylvana.net/jpegcrop/exif_orientation.html
        orientation = {90: 6, 270: 8}.get(rotation)
        if orientation is None:
            self._jpeg_app1 = None
        else:
            exif_data = piexif.dump({
                "0th": {
                    piexif.ImageIFD.Orientation: orientation,
                },
            })
            jpeg_app_len = len(exif_data) + 2
            self._jpeg_app1 = b"\xff\xe1" + (jpeg_app_len).to_bytes(2, byteorder="big") + exif_data

    @property
    def hw_transform(self):
        """Sensor-readout Transform applied for free by libcamera.

        180° = hflip + vflip is a sensor-register flip on IMX219/477/708;
        90°/270° fall back to the EXIF tag set in ``__init__``.
        """
        flip = self.rotation == 180
        return Transform(hflip=flip, vflip=flip)

    def write(self, buf):
        """Write the buffer to the stream and notify waiting threads.

        ``self.frame`` is always *replaced* with a fresh immutable bytes object,
        never mutated in place — readers may therefore keep a local reference
        after releasing the condition lock.
        """
        with self.condition:
            if self._jpeg_app1 is None:
                self.frame = buf
            else:
                # One pre-sized allocation instead of two intermediate copies.
                self.frame = b"".join((buf[:2], self._jpeg_app1, buf[2:]))
            self.frame_counter += 1
            self.condition.notify_all()


class StreamingHandler(server.BaseHTTPRequestHandler):
    """A request handler for serving the MJPEG stream."""

    frame_buffer = None

    def do_GET(self):  # noqa:N802
        """Serve the MJPEG stream."""
        url = urlparse(self.path)
        if url.path in ('/', '/webcam'):
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            try:
                while True:
                    with self.frame_buffer.condition:
                        if not self.frame_buffer.condition.wait(timeout=5):
                            # Camera stalled — drop the client so the thread can exit
                            # rather than blocking forever. Watchdog will reboot if needed.
                            logging.warning('No frame within timeout, disconnecting %s', self.client_address)
                            break
                        frame = self.frame_buffer.frame
                    self.wfile.write(b'--FRAME\r\n')
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', len(frame))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b'\r\n')
            except Exception as e:
                logging.warning('Removed streaming client %s: %s', self.client_address, str(e))
        else:
            self.send_error(404)
            self.end_headers()


class HttpHandler(server.BaseHTTPRequestHandler):
    """A request handler for the HTML page, snapshots, and the JSON control API."""

    frame_buffer = None
    # Pre-rendered landing page bytes; templated with the stream port at start time.
    page_bytes = None
    # CameraController instance shared across handler threads. None means the
    # control API endpoints respond 503 (camera initialised without controls).
    controller = None

    # The MJPEG hot path lives on a separate StreamingServer (port 8081) and
    # never goes through this handler — so the JSON control endpoints below
    # cannot starve frame delivery, even under POST flood.

    def do_GET(self):  # noqa:N802
        """Serve the HTML page, current frame as JPEG, or current control state."""
        url = urlparse(self.path)

        if url.path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(self.page_bytes))
            self.end_headers()
            self.wfile.write(self.page_bytes)
        elif url.path in ('/picture/1/current/', '/snapshot'):
            try:
                with self.frame_buffer.condition:
                    if not self.frame_buffer.condition.wait(timeout=5):
                        self.send_error(503, 'Camera frame unavailable')
                        return
                    frame = self.frame_buffer.frame
                self.send_response(200)
                self.send_header('Age', 0)
                self.send_header('Cache-Control', 'no-cache, private')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', len(frame))
                self.end_headers()
                self.wfile.write(frame)
            except Exception as e:
                logging.warning('Removed client %s: %s', self.client_address, str(e))
        elif url.path == '/api/controls':
            if self.controller is None:
                self.send_error(503, 'Controls unavailable')
                return
            self._send_json(
                200,
                {
                    'capabilities': self.controller.capabilities(),
                    'state': self.controller.get_state(),
                },
            )
        else:
            self.send_error(404)
            self.end_headers()

    def do_POST(self):  # noqa:N802
        """Handle control updates, reset, and one-shot autofocus."""
        url = urlparse(self.path)

        if url.path == '/api/controls':
            self._handle_apply_controls()
        elif url.path == '/api/controls/reset':
            self._handle_reset_controls()
        elif url.path == '/api/autofocus':
            self._handle_autofocus()
        else:
            self.send_error(404)
            self.end_headers()

    def _handle_apply_controls(self):
        """Apply a partial control dict and echo the merged state back."""
        if self.controller is None:
            self.send_error(503, 'Controls unavailable')
            return
        body = self._read_json_body()
        if body is None:
            return  # _read_json_body already sent the error
        try:
            new_state = self.controller.apply(body)
        except ValueError as exc:
            self._send_json(400, {'error': str(exc)})
            return
        except Exception as exc:
            logging.warning('Failed to apply controls: %s', exc)
            self._send_json(422, {'error': str(exc)})
            return
        self._send_json(200, {'state': new_state})

    def _handle_reset_controls(self):
        """Restore every supported control to its sensor default."""
        if self.controller is None:
            self.send_error(503, 'Controls unavailable')
            return
        try:
            new_state = self.controller.reset()
        except Exception as exc:
            logging.warning('Failed to reset controls: %s', exc)
            self._send_json(422, {'error': str(exc)})
            return
        self._send_json(200, {'state': new_state})

    def _handle_autofocus(self):
        """Run the one-shot autofocus cycle and return its outcome."""
        if self.controller is None:
            self.send_error(503, 'Controls unavailable')
            return
        body = self._read_json_body(allow_empty=True) or {}
        timeout = float(body.get('timeout', 5.0))
        try:
            result = self.controller.trigger_autofocus(timeout=timeout)
        except RuntimeError as exc:
            self._send_json(400, {'error': str(exc)})
            return
        except Exception as exc:
            logging.warning('Autofocus failed: %s', exc)
            self._send_json(422, {'error': str(exc)})
            return
        self._send_json(200, result)

    def _read_json_body(self, allow_empty=False):
        """Read and parse a JSON request body, sending an error response on failure."""
        length = int(self.headers.get('Content-Length') or 0)
        if length == 0:
            if allow_empty:
                return None
            self._send_json(400, {'error': 'empty body'})
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            self._send_json(400, {'error': f'invalid JSON: {exc}'})
            return None

    def _send_json(self, status, payload):
        """Serialise ``payload`` as JSON and write it as a complete response."""
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    """Threaded HTTPServer with a hard cap on concurrent worker threads.

    Each MJPEG client holds a worker thread for the entire stream duration, so
    without a cap a slow disconnect / TCP-half-close can pile up threads. New
    connections beyond ``max_clients`` are refused immediately.
    """

    allow_reuse_address = True
    daemon_threads = True
    max_clients = 16

    def __init__(self, *args, **kwargs):
        """Initialize the HTTP server and the concurrent-client semaphore."""
        super().__init__(*args, **kwargs)
        self._client_sem = Semaphore(self.max_clients)

    def process_request(self, request, client_address):
        """Reject the connection if the concurrent-client limit is reached."""
        if not self._client_sem.acquire(blocking=False):
            logging.warning('Rejecting %s: max %d clients reached', client_address, self.max_clients)
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        """Run the request and always release the semaphore on exit."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._client_sem.release()


async def watchdog(frame_buffer, interval=2, grace_period=30):
    """Monitor the frame buffer and reboot if no new frames are received within the interval.

    The grace_period absorbs camera initialization delay at startup; without it a
    slow-to-start camera triggers an immediate reboot loop.
    """
    await asyncio.sleep(grace_period)
    last_count = frame_buffer.frame_counter
    while True:
        await asyncio.sleep(interval)
        if frame_buffer.frame_counter == last_count:
            logging.warning("No new frames received in the last interval! Rebooting...")
            subprocess.run(["sudo", "reboot"], check=False)
        last_count = frame_buffer.frame_counter


@click.command()
@click.option(
    '--rotation',
    type=click.Choice(['0', '90', '180', '270']),
    default='180',
    show_default=True,
    help='Image rotation in degrees. 0/180 are applied by the sensor (free); '
    '90/270 are signalled to the client via EXIF.',
)
@click.option('--width', type=int, default=1920, show_default=True, help='Capture width in pixels.')
@click.option('--height', type=int, default=1080, show_default=True, help='Capture height in pixels.')
@click.option('--framerate', type=int, default=10, show_default=True, help='Target frame rate.')
@click.option(
    '--http-port',
    type=int,
    default=80,
    show_default=True,
    help='Port for the landing page and snapshot endpoint.',
)
@click.option('--stream-port', type=int, default=8081, show_default=True, help='Port for the MJPEG stream.')
@click.option(
    '--autofocus/--no-autofocus',
    default=True,
    show_default=True,
    help='Request continuous autofocus. Silently falls back if the sensor has no AF.',
)
@click.option(
    '--watchdog-interval',
    type=int,
    default=2,
    show_default=True,
    help='Seconds between watchdog frame-counter checks.',
)
@click.option(
    '--watchdog-grace-period',
    type=int,
    default=30,
    show_default=True,
    help='Seconds the watchdog waits at startup before enforcing the frame-counter check.',
)
@click.option(
    '--controls-file',
    type=click.Path(dir_okay=False),
    default=None,
    show_default=False,
    help='JSON file used to persist UI control changes across restarts. '
    'Default: ~/.config/meltingplot-rpi-camera/controls.json.',
)
@click.option(
    '--log-level',
    type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR'], case_sensitive=False),
    default='INFO',
    show_default=True,
    help='Logging verbosity.',
)
def start(
    rotation,
    width,
    height,
    framerate,
    http_port,
    stream_port,
    autofocus,
    watchdog_interval,
    watchdog_grace_period,
    controls_file,
    log_level,
):
    """
    Initialize and start the Raspberry Pi camera for video streaming.

    Configures the camera, optionally enables continuous autofocus, and runs
    both HTTP servers and the watchdog until any task stops. Ensures recording
    is stopped on exit so systemd can restart cleanly.
    """
    logging.basicConfig(
        level=log_level.upper(),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )

    frame_buffer = StreamingOutput(rotation=int(rotation))
    HttpHandler.frame_buffer = frame_buffer
    HttpHandler.page_bytes = _load_page_bytes(stream_port)
    StreamingHandler.frame_buffer = frame_buffer

    try:
        picam2 = Picamera2()
    except Exception as e:
        logging.error("Error initializing the camera: %s — is a RPi camera connected?", e)
        raise

    picam2.configure(
        picam2.create_video_configuration(
            main={"size": (width, height)},
            transform=frame_buffer.hw_transform,
        ),
    )
    picam2.start_recording(MJPEGEncoder(), FileOutput(frame_buffer))
    try:
        wanted_controls = {"FrameRate": framerate}
        if autofocus:
            try:
                picam2.set_controls({**wanted_controls, "AfMode": controls.AfModeEnum.Continuous})
            except Exception as e:
                # Camera Module v2 and similar have no AF — fall back without it.
                logging.warning("Autofocus not supported, continuing without: %s", e)
                picam2.set_controls(wanted_controls)
        else:
            picam2.set_controls(wanted_controls)

        # Wire up the live control API. Persisted values (if any) override the
        # CLI defaults applied above, so a saved exposure/AF setting survives
        # a watchdog reboot.
        persist_path = controls_file or os.path.join(
            os.path.expanduser('~'),
            '.config',
            'meltingplot-rpi-camera',
            'controls.json',
        )
        controller = CameraController(picam2, persist_path)
        controller.load_and_apply_persisted()
        HttpHandler.controller = controller

        asyncio.run(_run(frame_buffer, http_port, stream_port, watchdog_interval, watchdog_grace_period))
    finally:
        picam2.stop_recording()


def _load_page_bytes(stream_port):
    """Read the bundled landing page from package data and template the stream port."""
    try:
        html = importlib.resources.files('meltingplot.rpi_camera').joinpath('web/index.html').read_text(
            encoding='utf-8',
        )
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        logging.warning('Bundled landing page not found (%s); falling back to legacy page', exc)
        html = _FALLBACK_PAGE
    return html.replace('__STREAM_PORT__', str(stream_port)).encode('utf-8')


async def _run(frame_buffer, http_port, stream_port, watchdog_interval, watchdog_grace_period):
    """Run both HTTP servers and the watchdog; exit when any of them stops."""
    loop = asyncio.get_running_loop()
    http = StreamingServer(('', http_port), HttpHandler)
    stream = StreamingServer(('', stream_port), StreamingHandler)
    servers = (http, stream)

    # SIGTERM has no default Python translation; without this handler systemd's
    # stop signal terminates the process before the finally below can release
    # the camera and close sockets, forcing systemd to SIGKILL after its timeout.
    # SIGINT is already turned into KeyboardInterrupt by asyncio.run.
    stop_event = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    server_futures = [loop.run_in_executor(None, s.serve_forever) for s in servers]
    watchdog_task = asyncio.create_task(
        watchdog(frame_buffer, interval=watchdog_interval, grace_period=watchdog_grace_period),
    )
    stop_task = asyncio.create_task(stop_event.wait())
    pending = (*server_futures, watchdog_task, stop_task)
    labels = ('http server', 'stream server', 'watchdog', 'stop signal')

    try:
        await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # shutdown() signals serve_forever (running in the executor) to return,
        # then server_close() releases the listening sockets.
        for s in servers:
            s.shutdown()
            s.server_close()
        watchdog_task.cancel()
        stop_task.cancel()
        results = await asyncio.gather(*pending, return_exceptions=True)
        for label, result in zip(labels, results):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logging.error('%s task failed', label, exc_info=result)
