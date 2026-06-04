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
    GET /api/controls: Returns curated capabilities and currently applied state (plus host_active).
    POST /api/controls: Applies a partial control dict, persists it to JSON.
    POST /api/controls/reset: Restores every control to its sensor default.
    POST /api/autofocus: Runs a one-shot autofocus cycle.
    GET /api/status: Reports whether a USB host currently owns the camera (UVC).
While a USB host streams the camera as a UVC webcam it owns the device: the
MJPEG stream and snapshot return 503, control writes return 409, and the web
UI greys out — resuming automatically once the host stops streaming.
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
import time
from http import server
from threading import Condition, Event, Semaphore
from urllib.parse import urlparse

import click

from libcamera import Transform

from picamera2 import Picamera2

import piexif

from .controls import CameraController
from .reconfig import ReconfigCoordinator

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
        self.frame = None
        self.condition = Condition()
        self.frame_counter = 0
        # eventfds to poke on every new frame, so a select()-based consumer
        # (the UVC pump) can wait on "new frame" without polling. Guarded by
        # ``condition``; see add_wake_fd / write.
        self._wake_fds = []
        self._rotation = 0
        self._jpeg_app1 = None
        # Validate + build the EXIF tag via the setter.
        self.rotation = rotation

    @property
    def rotation(self):
        """Current output rotation in degrees: one of 0, 90, 180, 270."""
        return self._rotation

    @rotation.setter
    def rotation(self, value):
        """Set the rotation and (re)build the EXIF Orientation tag.

        0°/180° are applied by the sensor (see :attr:`hw_transform`) so need no
        EXIF tag; 90°/270° cannot be done in the sensor and are offloaded to the
        client via an EXIF Orientation tag (see
        http://sylvana.net/jpegcrop/exif_orientation.html). Runtime-settable so
        the reconfigure coordinator can change orientation from the web UI; the
        rebuild happens under ``condition`` so ``write`` never sees a torn tag.
        """
        value = int(value)
        if value not in (0, 90, 180, 270):
            raise ValueError("Invalid rotation value")
        orientation = {90: 6, 270: 8}.get(value)
        if orientation is None:
            app1 = None
        else:
            exif_data = piexif.dump({
                "0th": {
                    piexif.ImageIFD.Orientation: orientation,
                },
            })
            jpeg_app_len = len(exif_data) + 2
            app1 = b"\xff\xe1" + (jpeg_app_len).to_bytes(2, byteorder="big") + exif_data
        with self.condition:
            self._rotation = value
            self._jpeg_app1 = app1

    @property
    def hw_transform(self):
        """Sensor-readout Transform applied for free by libcamera.

        180° = hflip + vflip is a sensor-register flip on IMX219/477/708;
        90°/270° fall back to the EXIF tag set by the ``rotation`` setter.
        """
        flip = self._rotation == 180
        return Transform(hflip=flip, vflip=flip)

    def add_wake_fd(self, fd):
        """Register an eventfd poked (os.eventfd_write) on every new frame."""
        with self.condition:
            self._wake_fds.append(fd)

    def remove_wake_fd(self, fd):
        """Unregister a previously added eventfd (before the consumer closes it)."""
        with self.condition:
            try:
                self._wake_fds.remove(fd)
            except ValueError:
                pass

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
            # Wake any select()-based consumer (UVC pump). Non-blocking; a
            # closed/removed fd is simply skipped.
            for fd in self._wake_fds:
                try:
                    os.eventfd_write(fd, 1)
                except OSError:
                    pass


def _host_active(event):
    """Return True if a USB host is currently streaming the camera as a UVC webcam."""
    return bool(event is not None and event.is_set())


# OS connectivity-check probe paths. The matching hostnames are hijacked to this
# Pi by the usb0 dnsmasq (image: dnsmasq-shared.d/captive-portal.conf); answering
# them with a 302 to the webcam UI (instead of the expected success body) makes
# the connected host detect a captive portal and surface the camera page.
_CAPTIVE_PROBE_PATHS = frozenset({
    '/connecttest.txt',            # Windows NCSI
    '/ncsi.txt',                   # Windows NCSI (legacy)
    '/redirect',                   # Windows
    '/hotspot-detect.html',        # Apple
    '/library/test/success.html',  # Apple
    '/generate_204',               # Android / Chrome
    '/gen_204',                    # Android / Chrome
    '/canonical.html',             # Firefox / NetworkManager
    '/success.txt',                # NetworkManager / generic
})


class StreamingHandler(server.BaseHTTPRequestHandler):
    """A request handler for serving the MJPEG stream."""

    frame_buffer = None
    # threading.Event set while a USB host streams the camera as a UVC webcam.
    # The HTTP stream yields to it (no dual consumers). None until wired.
    host_streaming = None

    def do_GET(self):  # noqa:N802
        """Serve the MJPEG stream."""
        url = urlparse(self.path)
        if url.path in ('/', '/webcam'):
            if _host_active(self.host_streaming):
                self.send_error(503, 'Camera is in use as a USB (UVC) webcam')
                return
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
                    if _host_active(self.host_streaming):
                        # A USB host just took over the camera — drop the client.
                        logging.info('USB host took over; disconnecting stream client %s', self.client_address)
                        break
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
    # threading.Event set while a USB host streams the camera as a UVC webcam.
    # While set, the host owns the camera: snapshots and control writes are
    # refused and the UI greys out (the host drives controls over UVC).
    host_streaming = None

    # The MJPEG hot path lives on a separate StreamingServer (port 8081) and
    # never goes through this handler — so the JSON control endpoints below
    # cannot starve frame delivery, even under POST flood.

    def _serve_snapshot(self):
        """Serve the current frame as a single JPEG (refused while a host streams)."""
        if _host_active(self.host_streaming):
            self.send_error(503, 'Camera is in use as a USB (UVC) webcam')
            return
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

    def do_GET(self):  # noqa:N802
        """Serve the HTML page, current frame as JPEG, or current control state."""
        url = urlparse(self.path)

        # Captive-portal: OS connectivity-check probes (their hostnames are
        # pointed at this Pi by the usb0 dnsmasq) must NOT get the success
        # response they expect — 302 them to the webcam UI so the host flags a
        # captive portal and pops up "Sign in", landing the user on the camera.
        if url.path in _CAPTIVE_PROBE_PATHS:
            host_ip = self.connection.getsockname()[0]
            self.send_response(302)
            self.send_header('Location', 'http://%s/' % host_ip)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        if url.path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(self.page_bytes))
            self.end_headers()
            self.wfile.write(self.page_bytes)
        elif url.path == '/api/status':
            # Cheap endpoint the UI polls to learn when the host owns the camera.
            self._send_json(200, {'host_active': _host_active(self.host_streaming)})
        elif url.path in ('/picture/1/current/', '/snapshot'):
            self._serve_snapshot()
        elif url.path == '/api/controls':
            if self.controller is None:
                self.send_error(503, 'Controls unavailable')
                return
            self._send_json(
                200,
                {
                    'host_active': _host_active(self.host_streaming),
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

        if url.path in ('/api/controls', '/api/controls/reset',
                        '/api/autofocus') and _host_active(self.host_streaming):
            # The USB host owns the camera while it streams; reject UI writes.
            self._send_json(409, {'error': 'Camera is controlled by the USB host (UVC)'})
            return
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


# Self-issued service restarts are recorded here so the *next* process (the
# restart kills this one) can tell a recovered pipeline from one that is still
# wedged. Lives under the unit's RuntimeDirectory (/run, tmpfs): it survives a
# `systemctl restart` (RuntimeDirectoryPreserve=restart) but is wiped on reboot,
# so a hard reboot always re-arms stage 1.
_STALL_MARKER = '/run/rpi-camera/frame-stall'


def _read_restart_marker():
    """Return the time.time() of the last self-issued restart, or 0.0 if none."""
    try:
        with open(_STALL_MARKER) as fh:
            return float(fh.read().strip())
    except (OSError, ValueError):
        return 0.0


def _clear_restart_marker():
    """Drop the restart marker (no-op if absent)."""
    try:
        os.remove(_STALL_MARKER)
    except OSError:
        pass


def _escalate_stall(now, restart_window):
    """Act on a confirmed frame stall; return the action taken.

    Stage 1 ('restart'): no restart issued within ``restart_window`` seconds, so
    restart rpi-camera.service to recover a wedged libcamera pipeline without a
    full boot, recording the attempt in the /run marker.
    Stage 2 ('reboot'): a restart was already issued within the window and frames
    still have not resumed, so reboot.

    If the marker cannot be written, stage 2 could never fire, so we reboot
    rather than risk an unbounded restart loop on a permanently wedged pipeline.
    """
    restarted_at = _read_restart_marker()
    if restarted_at and (now - restarted_at) < restart_window:
        logging.error(
            "Watchdog: no new frames %ds after restarting rpi-camera.service; rebooting",
            int(now - restarted_at),
        )
        _clear_restart_marker()
        subprocess.run(["sudo", "reboot"], check=False)
        return 'reboot'

    try:
        with open(_STALL_MARKER, 'w') as fh:
            fh.write('%f' % now)
    except OSError as exc:
        logging.error("Watchdog: cannot persist restart marker (%s); rebooting", exc)
        subprocess.run(["sudo", "reboot"], check=False)
        return 'reboot'

    logging.warning("Watchdog: frame stall detected; restarting rpi-camera.service")
    subprocess.run(["sudo", "systemctl", "restart", "rpi-camera.service"], check=False)
    return 'restart'


async def watchdog(frame_buffer, interval=2, grace_period=30, stall_limit=5, restart_window=300):
    """Two-stage frame-stall watchdog.

    After ``grace_period`` (which absorbs camera init at startup), sample
    ``frame_counter`` every ``interval`` seconds. When no new frame arrives for
    ``stall_limit`` consecutive samples, escalate via :func:`_escalate_stall`:
    first restart the service, and only reboot if a restart within the last
    ``restart_window`` seconds failed to restore frames. This is deliberately
    more forgiving than an immediate reboot — a transient hiccup no longer costs
    a full boot cycle.
    """
    await asyncio.sleep(grace_period)
    last_count = frame_buffer.frame_counter
    stalls = 0
    # A pre-restart instance may have left a marker; clear it once frames prove
    # healthy. Tracked locally so the healthy path isn't a remove() every tick.
    marker_maybe = _read_restart_marker() > 0
    while True:
        await asyncio.sleep(interval)
        count = frame_buffer.frame_counter
        if count != last_count:
            last_count = count
            stalls = 0
            if marker_maybe:
                _clear_restart_marker()
                marker_maybe = False
            continue
        stalls += 1
        if stalls < stall_limit:
            continue
        if _escalate_stall(time.time(), restart_window) == 'restart':
            marker_maybe = True
        stalls = 0


def _read_board_model():
    """Return the Raspberry Pi model string, or '' if it can't be read."""
    try:
        with open('/proc/device-tree/model', 'rb') as fh:
            # The device-tree string is NUL-terminated.
            return fh.read().decode('utf-8', 'replace').rstrip('\x00').strip()
    except OSError:
        return ''


def _default_resolution(model=None):
    """Pick the default capture resolution for this board.

    The single-core BCM2835 boards (Pi Zero / Zero W, ARMv6, 1 GHz) cannot
    sustain 1080p MJPEG alongside the HTTP/UVC fan-out comfortably, so they
    default to 720p. Every other board defaults to 1080p. An explicit
    ``--width``/``--height`` (or ``RPI_CAMERA_WIDTH``/``HEIGHT``) overrides
    this. The Pi Zero **2** W is a quad-core board and is excluded from the
    720p case.
    """
    if model is None:
        model = _read_board_model()
    if 'Zero' in model and 'Zero 2' not in model:
        return 1280, 720
    return 1920, 1080


@click.command()
@click.option(
    '--rotation',
    type=click.Choice(['0', '90', '180', '270']),
    default='180',
    show_default=True,
    help='Image rotation in degrees. 0/180 are applied by the sensor (free); '
    '90/270 are signalled to the client via EXIF.',
)
@click.option(
    '--width',
    type=int,
    envvar='RPI_CAMERA_WIDTH',
    default=None,
    help='Capture width in pixels. Defaults per board: 1280 on the '
    'single-core Pi Zero / Zero W, 1920 elsewhere.',
)
@click.option(
    '--height',
    type=int,
    envvar='RPI_CAMERA_HEIGHT',
    default=None,
    help='Capture height in pixels. Defaults per board: 720 on the '
    'single-core Pi Zero / Zero W, 1080 elsewhere.',
)
@click.option(
    '--framerate',
    type=int,
    envvar='RPI_CAMERA_FRAMERATE',
    default=4,
    show_default=True,
    help='Target frame rate. Kept low by default because MJPEG capture '
    'plus the HTTP and USB-UVC fan-out saturates the low-end Pi boards.',
)
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
    '--watchdog-stall-limit',
    type=int,
    default=5,
    show_default=True,
    help='Consecutive watchdog checks with no new frame before the watchdog acts. '
    'With the default 2s interval, 5 means ~10s of frozen video before a restart.',
)
@click.option(
    '--watchdog-restart-window',
    type=int,
    default=300,
    show_default=True,
    help='If a watchdog-triggered service restart does not restore frames within '
    'this many seconds, the watchdog escalates to a full reboot.',
)
@click.option(
    '--watchdog/--no-watchdog',
    envvar='RPI_CAMERA_WATCHDOG',
    default=True,
    show_default=True,
    help='Frame-stall watchdog (restart the service, then reboot). Disable on a '
    'bench/test device so it does not restart or reboot during debugging '
    '(RPI_CAMERA_WATCHDOG=0).',
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
    '--enable-uvc/--no-enable-uvc',
    envvar='RPI_CAMERA_ENABLE_UVC',
    default=True,
    show_default=True,
    help='Feed the MJPEG stream into a USB UVC gadget when one is present. '
    'No-op on boards/images without the gadget configured.',
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
    watchdog_stall_limit,
    watchdog_restart_window,
    watchdog,
    controls_file,
    enable_uvc,
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

    # Resolve the per-board default capture resolution when the operator
    # has not pinned --width/--height (or the RPI_CAMERA_WIDTH/HEIGHT env).
    default_width, default_height = _default_resolution()
    if width is None:
        width = default_width
    if height is None:
        height = default_height
    logging.info('Capture resolution: %dx%d @ %d fps', width, height, framerate)

    frame_buffer = StreamingOutput(rotation=int(rotation))
    HttpHandler.frame_buffer = frame_buffer
    HttpHandler.page_bytes = _load_page_bytes(stream_port)
    StreamingHandler.frame_buffer = frame_buffer

    try:
        picam2 = Picamera2()
    except Exception as e:
        logging.error("Error initializing the camera: %s — is a RPi camera connected?", e)
        raise

    persist_path = controls_file or os.path.join(
        os.path.expanduser('~'),
        '.config',
        'meltingplot-rpi-camera',
        'controls.json',
    )
    controller = CameraController(picam2, persist_path)

    # The coordinator owns the recording, the UVC gadget descriptors and the
    # pump, so a UI Resolution/FrameRate change reconfigures camera + gadget
    # together (picamera2 stays the sole camera owner; the pump only reads the
    # shared frame buffer, so HTTP and UVC run from one capture).
    coordinator = ReconfigCoordinator(
        picam2,
        frame_buffer,
        autofocus=autofocus,
        enable_uvc=enable_uvc,
    )
    coordinator.set_controller(controller)
    controller.register_change_listener(coordinator.on_change)
    HttpHandler.controller = controller

    # When a USB host opens the UVC stream it owns the camera: the HTTP stream
    # and control writes yield to it (the UI greys out with a hint). The pump
    # toggles this via the coordinator's stream listener.
    host_streaming = Event()
    HttpHandler.host_streaming = host_streaming
    StreamingHandler.host_streaming = host_streaming
    coordinator.register_stream_listener(
        lambda active: host_streaming.set() if active else host_streaming.clear(),
    )

    try:
        # Initial pipeline + gadget + pump.
        coordinator.bring_up(width, height, framerate)
        # Show the active capture settings in the UI, then load persisted
        # controls — a saved Resolution/FrameRate triggers a reconfigure, the
        # rest are applied live (survives a watchdog reboot).
        controller.seed_reconfig_state('%dx%d' % (width, height), framerate, rotation=frame_buffer.rotation)
        controller.load_and_apply_persisted()

        asyncio.run(
            _run(
                frame_buffer,
                http_port,
                stream_port,
                watchdog_interval,
                watchdog_grace_period,
                watchdog_stall_limit,
                watchdog_restart_window,
                watchdog,
            ),
        )
    finally:
        coordinator.stop()


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


async def _run(
    frame_buffer,
    http_port,
    stream_port,
    watchdog_interval,
    watchdog_grace_period,
    watchdog_stall_limit=5,
    watchdog_restart_window=300,
    watchdog_enabled=True,
):
    """Run both HTTP servers and (optionally) the watchdog; exit when any stops."""
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
    stop_task = asyncio.create_task(stop_event.wait())
    pending = [*server_futures, stop_task]
    labels = ['http server', 'stream server', 'stop signal']
    watchdog_task = None
    if watchdog_enabled:
        watchdog_task = asyncio.create_task(
            watchdog(
                frame_buffer,
                interval=watchdog_interval,
                grace_period=watchdog_grace_period,
                stall_limit=watchdog_stall_limit,
                restart_window=watchdog_restart_window,
            ),
        )
        pending.append(watchdog_task)
        labels.append('watchdog')
    else:
        logging.warning('Frame watchdog disabled (--no-watchdog / RPI_CAMERA_WATCHDOG=0)')

    try:
        await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # shutdown() signals serve_forever (running in the executor) to return,
        # then server_close() releases the listening sockets.
        for s in servers:
            s.shutdown()
            s.server_close()
        if watchdog_task is not None:
            watchdog_task.cancel()
        stop_task.cancel()
        results = await asyncio.gather(*pending, return_exceptions=True)
        for label, result in zip(labels, results):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logging.error('%s task failed', label, exc_info=result)
