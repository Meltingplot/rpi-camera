# -*- coding: utf-8 -*-
"""Coordinate camera reconfiguration on resolution/fps changes.

Frame rate is a live libcamera control; resolution is not — changing it
means re-configuring the Picamera2 pipeline (stop/start recording). This
module owns that sequence so a UI change to either value is applied safely.

The USB UVC gadget needs **no** reconfiguration here: its configfs
descriptors are provisioned once at boot for the worst case the UI allows
(1080p @ 30 fps, see rpi-cam-gadget-setup.sh). MJPEG is self-describing, so
the pump streams any capture resolution <= that envelope into the same node
without a USB re-enumeration, and paces delivery to the capture fps. The
pump is therefore started once and kept running across reconfigures.

The work runs on a dedicated worker thread fed by a coalescing queue, so
the HTTP control handler that triggered the change returns immediately
instead of blocking for the seconds a pipeline reconfigure takes.
"""

import logging
import os
import queue
import threading

from libcamera import controls as libcontrols

from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

from .uvc_gadget import UvcGadget

log = logging.getLogger(__name__)

# Allow runtime FrameRate across the whole UI range (1..30 fps). Frame
# duration limits are microseconds: (min = 30 fps, max = 1 fps). Without a
# wide range here picamera2 picks a sensor mode that clamps low frame rates.
FRAME_DURATION_LIMITS = (33333, 1000000)

# Touched when a USB host opens the UVC stream. The image's gadget mode-switch
# (rpi-cam-gadget-mode.sh) reads this to decide whether to keep the device in
# UVC mode or fall back to NCM networking. Lives in the systemd RuntimeDirectory
# (wiped each boot); override for tests via RPI_CAMERA_UVC_ACTIVE_FLAG.
UVC_ACTIVE_FLAG = os.environ.get('RPI_CAMERA_UVC_ACTIVE_FLAG', '/run/rpi-camera/uvc-active')


def parse_resolution(value, fallback):
    """Parse a ``"WIDTHxHEIGHT"`` string into an (int, int), else fallback."""
    try:
        width, height = str(value).lower().split('x')
        return int(width), int(height)
    except (ValueError, AttributeError):
        return fallback


class ReconfigCoordinator:
    """Own the camera recording and the UVC pump.

    ``bring_up`` performs the initial configure/record/pump synchronously.
    ``on_change`` (a :class:`CameraController` listener) enqueues later
    Resolution/FrameRate changes from the web UI, which the worker applies one
    at a time. While a USB host is streaming the camera as a UVC webcam, the
    host owns the resolution/fps (via the pump's COMMIT callback) and UI-driven
    changes are ignored.
    """

    def __init__(self, picam2, frame_buffer, *, transform, autofocus, enable_uvc):
        """Bind dependencies and start the reconfigure worker thread."""
        self._picam2 = picam2
        self._frame_buffer = frame_buffer
        self._transform = transform
        self._autofocus = autofocus
        self._enable_uvc = enable_uvc
        self._controller = None
        self._lock = threading.RLock()
        self._pump = None
        self._recording = False
        self._width = None
        self._height = None
        self._fps = None
        self._host_streaming = False  # set by the UVC pump while a host streams
        self._stream_listener = None  # optional callback(active: bool)
        self._queue = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, name='reconfig', daemon=True)
        self._worker.start()

    def set_controller(self, controller):
        """Attach the controller so live controls can be re-applied after a reconfigure."""
        self._controller = controller

    def register_stream_listener(self, callback):
        """Register a ``callback(active: bool)`` fired when the host opens/closes the UVC stream."""
        self._stream_listener = callback

    def bring_up(self, width, height, framerate):
        """Run the initial synchronous configure + record + gadget + pump."""
        with self._lock:
            self._reconfigure(width, height, framerate, force=True)

    def on_change(self, merged_state, changed):
        """Enqueue a Resolution/FrameRate change for the worker (returns at once)."""
        if self._host_streaming:
            # The USB host owns resolution/fps while it streams; ignore the UI.
            log.info('Ignoring UI Resolution/FrameRate change while USB host is streaming')
            return
        width, height = parse_resolution(
            merged_state.get('Resolution'),
            (self._width or 1280, self._height or 720),
        )
        fps = int(merged_state.get('FrameRate', self._fps or 4))
        self._queue.put((width, height, fps))

    # -- UVC pump callbacks (run on the pump thread) -------------------
    def _on_host_format(self, width, height, fps):
        """Reconfigure the camera to the format the USB host just COMMITted."""
        log.info('USB host selected %dx%d @ %d fps', width, height, fps)
        self._queue.put((width, height, fps))

    def _on_stream_state(self, active):
        """Toggle host ownership and notify listeners when the host opens/closes the stream."""
        self._host_streaming = active
        log.info('USB host UVC stream %s', 'started' if active else 'stopped')
        if active:
            self._flag_uvc_active()
        if self._stream_listener is not None:
            try:
                self._stream_listener(active)
            except Exception:
                log.exception('stream listener failed')

    @staticmethod
    def _flag_uvc_active():
        """Record that a host opened the UVC stream (see UVC_ACTIVE_FLAG).

        Best-effort: the boot-time gadget mode-switch reads this to keep the
        device in UVC mode instead of falling back to NCM. A failure here only
        means a genuinely-used webcam might still flip to NCM, so it is logged
        and ignored rather than disrupting streaming.
        """
        try:
            with open(UVC_ACTIVE_FLAG, 'w') as fh:
                fh.write('1')
        except OSError as exc:
            log.debug('could not write UVC-active flag %s: %s', UVC_ACTIVE_FLAG, exc)

    def stop(self):
        """Stop the pump and recording (called on server shutdown)."""
        with self._lock:
            if self._pump is not None:
                self._pump.stop()
                self._pump = None
            if self._recording:
                try:
                    self._picam2.stop_recording()
                except Exception:
                    pass
                self._recording = False

    # -- worker --------------------------------------------------------
    def _worker_loop(self):
        while True:
            item = self._queue.get()
            # Coalesce: only the most recent request matters.
            while not self._queue.empty():
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
            width, height, fps = item
            try:
                with self._lock:
                    self._reconfigure(width, height, fps, force=False)
            except Exception:
                log.exception('Camera/UVC reconfigure to %dx%d@%d failed', width, height, fps)

    def _reconfigure(self, width, height, fps, force):
        size_changed = (width, height) != (self._width, self._height)
        fps_changed = fps != self._fps
        if not force and not size_changed and not fps_changed:
            return
        log.info('Reconfigure: %dx%d @ %d fps', width, height, fps)

        if force or size_changed:
            if self._recording:
                self._picam2.stop_recording()
                self._recording = False
            self._picam2.configure(
                self._picam2.create_video_configuration(
                    main={'size': (width, height)},
                    transform=self._transform,
                    controls={'FrameDurationLimits': FRAME_DURATION_LIMITS},
                ),
            )
            self._picam2.start_recording(MJPEGEncoder(), FileOutput(self._frame_buffer))
            self._recording = True
            self._apply_camera_controls(fps)
        else:
            # fps-only change: a live control, no pipeline restart needed.
            self._set_framerate(fps)

        self._width, self._height, self._fps = width, height, fps

        # The UVC gadget descriptors are static (the host negotiates one of the
        # advertised frames via PROBE/COMMIT), so a resolution/fps change never
        # touches USB or re-enumerates: the pump keeps running and shuttles the
        # new (self-describing) JPEGs. Only ensure it is up.
        if self._enable_uvc and self._pump is None:
            self._start_pump()

    def _apply_camera_controls(self, fps):
        # A configure() resets all controls, so re-apply autofocus default,
        # the persisted live controls, then the frame rate.
        if self._autofocus:
            try:
                self._picam2.set_controls({'AfMode': libcontrols.AfModeEnum.Continuous})
            except Exception as exc:
                log.warning('Autofocus not supported, continuing without: %s', exc)
        if self._controller is not None:
            self._controller.reapply_live()
        self._set_framerate(fps)

    def _set_framerate(self, fps):
        try:
            self._picam2.set_controls({'FrameRate': fps})
        except Exception as exc:
            log.warning('FrameRate %d not applied: %s', fps, exc)

    def _start_pump(self):
        device = UvcGadget.find_device()
        if not device:
            log.info('No UVC gadget output node found; UVC disabled')
            return
        log.info('UVC gadget node: %s', device)
        # The host picks the resolution/fps from the advertised set; the pump
        # calls back here to reconfigure the camera and to flag when the host
        # is streaming. The controller lets the pump map the host's UVC
        # VideoControl requests (brightness/exposure/...) to libcamera.
        self._pump = UvcGadget(
            device,
            self._frame_buffer,
            on_host_format=self._on_host_format,
            on_stream_state=self._on_stream_state,
            controller=self._controller,
        )
        self._pump.start()
