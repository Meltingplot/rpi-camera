# -*- coding: utf-8 -*-
"""Coordinate camera + USB-gadget reconfiguration on resolution/fps changes.

Resolution and frame rate are not live libcamera controls: changing them
means re-configuring the Picamera2 pipeline and rewriting the UVC gadget
descriptors (which forces a USB re-enumeration on the host). This module
owns that sequence so a UI change to either value reconfigures the camera
and the gadget consistently.

The work runs on a dedicated worker thread fed by a coalescing queue, so
the HTTP control handler that triggered the change returns immediately
instead of blocking for the seconds a reconfigure + USB reconnect takes.
"""

import logging
import os
import queue
import subprocess
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


def parse_resolution(value, fallback):
    """Parse a ``"WIDTHxHEIGHT"`` string into an (int, int), else fallback."""
    try:
        width, height = str(value).lower().split('x')
        return int(width), int(height)
    except (ValueError, AttributeError):
        return fallback


class ReconfigCoordinator:
    """Own the camera recording, the UVC pump, and the gadget descriptors.

    ``bring_up`` performs the initial configure/record/pump synchronously.
    ``on_change`` (a :class:`CameraController` listener) enqueues later
    Resolution/FrameRate changes, which the worker applies one at a time.
    """

    def __init__(self, picam2, frame_buffer, *, transform, autofocus, enable_uvc, gadget_helper):
        """Bind dependencies and start the reconfigure worker thread."""
        self._picam2 = picam2
        self._frame_buffer = frame_buffer
        self._transform = transform
        self._autofocus = autofocus
        self._enable_uvc = enable_uvc
        self._gadget_helper = gadget_helper
        self._controller = None
        self._lock = threading.RLock()
        self._pump = None
        self._recording = False
        self._width = None
        self._height = None
        self._fps = None
        self._queue = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, name='reconfig', daemon=True)
        self._worker.start()

    def set_controller(self, controller):
        """Attach the controller so live controls can be re-applied after a reconfigure."""
        self._controller = controller

    def bring_up(self, width, height, framerate):
        """Run the initial synchronous configure + record + gadget + pump."""
        with self._lock:
            self._reconfigure(width, height, framerate, force=True)

    def on_change(self, merged_state, changed):
        """Enqueue a Resolution/FrameRate change for the worker (returns at once)."""
        width, height = parse_resolution(
            merged_state.get('Resolution'),
            (self._width or 1280, self._height or 720),
        )
        fps = int(merged_state.get('FrameRate', self._fps or 4))
        self._queue.put((width, height, fps))

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

        # Pause the pump while the gadget node is torn down / recreated.
        if self._pump is not None:
            self._pump.stop()
            self._pump = None

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

        if self._enable_uvc:
            self._reconfigure_gadget(width, height, fps)
            self._start_pump(width, height, fps)

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

    def _reconfigure_gadget(self, width, height, fps):
        if not self._gadget_helper or not os.path.exists('/run/rpi-cam-gadget.enabled'):
            return
        try:
            subprocess.run(
                ['sudo', self._gadget_helper, str(width),
                 str(height), str(fps)],
                check=True,
                timeout=20,
            )
        except Exception:
            log.exception('UVC gadget reconfigure helper failed')

    def _start_pump(self, width, height, fps):
        device = UvcGadget.find_device()
        if not device:
            log.info('No UVC gadget output node found; UVC disabled')
            return
        log.info('UVC gadget node: %s', device)
        self._pump = UvcGadget(device, width, height, fps, lambda: self._frame_buffer.frame)
        self._pump.start()
