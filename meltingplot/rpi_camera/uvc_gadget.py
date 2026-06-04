# -*- coding: utf-8 -*-
"""Userspace pump for the Linux UVC gadget (``g_uvc`` / configfs ``uvc`` function).

When the Pi is configured as a USB UVC webcam the kernel exposes a V4L2
**output** node (``/dev/videoN``) for the gadget side. The host's UVC
driver negotiates a format over the control endpoint and then pulls video
frames; the kernel forwards both as events on that node and expects a
userspace process to (a) answer the PROBE/COMMIT control negotiation and
(b) feed frames into queued buffers. This module is that process.

Scope: the configfs descriptors (written once by the image's gadget-setup
script) advertise a **single MJPEG format with several frame sizes** and are
never rewritten at runtime. The USB host picks a resolution/fps via
PROBE/COMMIT; this pump honours that ``bFrameIndex``, asks the coordinator to
drive picamera2 to it, sizes its buffers to the chosen frame, and feeds the
JPEGs into the node — so the host negotiates resolution the regular UVC way,
with no descriptor rewrite or USB re-enumeration. Delivery is **paced to the
camera's frame rate**: a new buffer is queued only when a new frame appears
(signalled via an eventfd), so a low fps costs no extra CPU or USB bandwidth
(the isochronous endpoint simply idles between frames instead of re-sending
duplicate frames).

This is intricate kernel-ABI code. ioctl numbers are computed at runtime
from ``ctypes.sizeof`` so they are correct for whatever ABI we run on
(the target is 32-bit armhf). It is a faithful, compact port of the
kernel ``tools/usb/uvc-gadget.c`` select-loop. Any failure logs and stops
the pump thread without touching the HTTP/MJPEG server.
"""

import ctypes
import errno
import fcntl
import logging
import mmap
import os
import select
import struct
import threading

from . import gadget_configfs
from .uvc_controls import UvcControlBridge

log = logging.getLogger(__name__)

# --- Static gadget envelope -------------------------------------------
# The configfs descriptors (rpi-cam-gadget-setup.sh) provision the UVC
# function ONCE for a per-board "envelope" (the largest frame the board is
# expected to ever stream) and are never rewritten at runtime. The pump
# advertises and sizes its buffers for that same envelope; a smaller capture
# resolution just yields a smaller (self-describing) JPEG that the host
# decodes at its embedded size, with no USB re-enumeration.
MAX_FPS = 30
# Fallback defaults ONLY. At runtime the pump reads the frames, per-frame
# intervals and streaming_maxpacket back from configfs (the values
# rpi-cam-gadget-setup.sh actually wrote — the single source of truth), so it
# can never advertise something the kernel didn't. These are used only if
# configfs is absent/unreadable (e.g. running outside the gadget image).
#
# Advertised MJPEG frame intervals (100 ns units), fastest first: 30/15/10/5/4/2/1 fps.
FRAME_INTERVALS = (333333, 666666, 1000000, 2000000, 2500000, 5000000, 10000000)
# dwMaxPayloadTransferSize fallback. >1024 needs high-bandwidth iso, which the
# Pi's dwc2 UDC lacks; the real value comes from configfs streaming_maxpacket.
_ISO_MAXPACKET = 1024


def _read_board_model():
    """Return the Raspberry Pi model string, or '' if it can't be read."""
    try:
        with open('/proc/device-tree/model', 'rb') as fh:
            # The device-tree string is NUL-terminated.
            return fh.read().decode('utf-8', 'replace').rstrip('\x00').strip()
    except OSError:
        return ''


def gadget_frames(model=None):
    """Ordered list of advertised MJPEG frame sizes ``[(w, h), ...]`` for this board.

    The 1-based index into this list IS the UVC ``bFrameIndex`` the host
    negotiates, so ``rpi-cam-gadget-setup.sh`` MUST create the configfs frames
    in this exact (ascending) order for the indices to line up. The largest
    entry is bounded per board to what the hardware can sensibly stream:

    * single-core Pi Zero / Zero W -> up to 1280x720  (ARMv6, mem + CPU bound)
    * Pi Zero 2 W                  -> up to 1920x1080
    * everything else (Pi 4/5/...) -> up to 4608x2592 (IMX708 full sensor)

    The host (UVC consumer) picks one of these; the pump then drives picamera2
    to the chosen size, so no descriptor rewrite / USB re-enumeration occurs.
    """
    if model is None:
        model = _read_board_model()
    if 'Zero 2' in model:
        return [(640, 480), (1280, 720), (1920, 1080)]
    if 'Zero' in model:
        return [(640, 480), (1280, 720)]
    return [(640, 480), (1280, 720), (1920, 1080), (2304, 1296), (4608, 2592)]


def _default_frame_index(frames):
    """1-based default ``bFrameIndex`` — 720p where present, else the first."""
    for i, (w, h) in enumerate(frames, start=1):
        if (w, h) == (1280, 720):
            return i
    return 1


def _fps_from_interval(interval):
    """Convert a UVC ``dwFrameInterval`` (100 ns units) to an integer fps."""
    if interval <= 0:
        return MAX_FPS
    return max(1, min(MAX_FPS, round(10_000_000 / interval)))


def _snap_interval(interval, intervals):
    """Snap a requested frame interval to the nearest advertised value.

    ``intervals`` is the chosen frame's advertised list, ascending (fastest
    first). Returns the first entry >= the request (rounding the fps down to a
    supported rate), or the slowest if the request is below every listed rate.
    UVC requires the device to echo back a value it actually advertised, not an
    arbitrary in-between interval, so this snaps instead of clamping the range.
    """
    if interval <= 0:
        return intervals[0]
    for iv in intervals:
        if iv >= interval:
            return iv
    return intervals[-1]


# --- ioctl number construction (asm-generic _IOC, used by arm) ---------
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_NONE = 0
_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction, typ, nr, size):
    return (
        (direction << _IOC_DIRSHIFT) | (ord(typ) << _IOC_TYPESHIFT) | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def _ior(typ, nr, struct_type):
    return _ioc(_IOC_READ, typ, nr, ctypes.sizeof(struct_type))


def _iow(typ, nr, struct_type):
    return _ioc(_IOC_WRITE, typ, nr, ctypes.sizeof(struct_type))


def _iowr(typ, nr, struct_type):
    return _ioc(_IOC_READ | _IOC_WRITE, typ, nr, ctypes.sizeof(struct_type))


# --- V4L2 / UVC constants ---------------------------------------------
V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_MEMORY_MMAP = 1
V4L2_FIELD_NONE = 1
V4L2_CAP_VIDEO_OUTPUT = 0x00000002
V4L2_CAP_DEVICE_CAPS = 0x80000000


def _fourcc(a, b, c, d):
    return ord(a) | (ord(b) << 8) | (ord(c) << 16) | (ord(d) << 24)


V4L2_PIX_FMT_MJPEG = _fourcc('M', 'J', 'P', 'G')

V4L2_EVENT_PRIVATE_START = 0x08000000
UVC_EVENT_CONNECT = V4L2_EVENT_PRIVATE_START + 0
UVC_EVENT_DISCONNECT = V4L2_EVENT_PRIVATE_START + 1
UVC_EVENT_STREAMON = V4L2_EVENT_PRIVATE_START + 2
UVC_EVENT_STREAMOFF = V4L2_EVENT_PRIVATE_START + 3
UVC_EVENT_SETUP = V4L2_EVENT_PRIVATE_START + 4
UVC_EVENT_DATA = V4L2_EVENT_PRIVATE_START + 5

# UVC streaming-interface control selectors and USB requests.
UVC_VS_PROBE_CONTROL = 0x01
UVC_VS_COMMIT_CONTROL = 0x02
UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_MIN = 0x82
UVC_GET_MAX = 0x83
UVC_GET_RES = 0x84
UVC_GET_LEN = 0x85
UVC_GET_INFO = 0x86
UVC_GET_DEF = 0x87

USB_TYPE_MASK = 0x60
USB_TYPE_CLASS = 0x20
USB_DIR_IN = 0x80

_EVENT_NAMES = {
    UVC_EVENT_CONNECT: 'CONNECT',
    UVC_EVENT_DISCONNECT: 'DISCONNECT',
    UVC_EVENT_STREAMON: 'STREAMON',
    UVC_EVENT_STREAMOFF: 'STREAMOFF',
    UVC_EVENT_SETUP: 'SETUP',
    UVC_EVENT_DATA: 'DATA',
}
_REQ_NAMES = {
    UVC_SET_CUR: 'SET_CUR',
    UVC_GET_CUR: 'GET_CUR',
    UVC_GET_MIN: 'GET_MIN',
    UVC_GET_MAX: 'GET_MAX',
    UVC_GET_RES: 'GET_RES',
    UVC_GET_LEN: 'GET_LEN',
    UVC_GET_INFO: 'GET_INFO',
    UVC_GET_DEF: 'GET_DEF',
}


# --- ctypes structures (sizes resolved per-ABI at runtime) ------------
class Timeval(ctypes.Structure):
    """Time64 ``struct timeval`` (16 bytes — trixie armhf uses 64-bit time_t)."""

    _fields_ = [('tv_sec', ctypes.c_int64), ('tv_usec', ctypes.c_int64)]


class Timespec(ctypes.Structure):
    """``struct __kernel_timespec`` (time64, 16 bytes on 32- and 64-bit)."""

    _fields_ = [('tv_sec', ctypes.c_int64), ('tv_nsec', ctypes.c_int64)]


class V4l2Timecode(ctypes.Structure):
    """Mirror of ``struct v4l2_timecode``."""

    _fields_ = [
        ('type', ctypes.c_uint32),
        ('flags', ctypes.c_uint32),
        ('frames', ctypes.c_uint8),
        ('seconds', ctypes.c_uint8),
        ('minutes', ctypes.c_uint8),
        ('hours', ctypes.c_uint8),
        ('userbits', ctypes.c_uint8 * 4),
    ]


class V4l2Capability(ctypes.Structure):
    """Mirror of ``struct v4l2_capability`` (VIDIOC_QUERYCAP)."""

    _fields_ = [
        ('driver', ctypes.c_uint8 * 16),
        ('card', ctypes.c_uint8 * 32),
        ('bus_info', ctypes.c_uint8 * 32),
        ('version', ctypes.c_uint32),
        ('capabilities', ctypes.c_uint32),
        ('device_caps', ctypes.c_uint32),
        ('reserved', ctypes.c_uint32 * 3),
    ]


class V4l2PixFormat(ctypes.Structure):
    """Mirror of ``struct v4l2_pix_format``."""

    _fields_ = [
        ('width', ctypes.c_uint32),
        ('height', ctypes.c_uint32),
        ('pixelformat', ctypes.c_uint32),
        ('field', ctypes.c_uint32),
        ('bytesperline', ctypes.c_uint32),
        ('sizeimage', ctypes.c_uint32),
        ('colorspace', ctypes.c_uint32),
        ('priv', ctypes.c_uint32),
        ('flags', ctypes.c_uint32),
        ('enc', ctypes.c_uint32),
        ('quantization', ctypes.c_uint32),
        ('xfer_func', ctypes.c_uint32),
    ]


class _V4l2FormatUnion(ctypes.Union):
    _fields_ = [('pix', V4l2PixFormat), ('raw_data', ctypes.c_uint8 * 200)]


class V4l2Format(ctypes.Structure):
    """Mirror of ``struct v4l2_format``."""

    _fields_ = [('type', ctypes.c_uint32), ('fmt', _V4l2FormatUnion)]


class V4l2Requestbuffers(ctypes.Structure):
    """Mirror of ``struct v4l2_requestbuffers``."""

    _fields_ = [
        ('count', ctypes.c_uint32),
        ('type', ctypes.c_uint32),
        ('memory', ctypes.c_uint32),
        ('capabilities', ctypes.c_uint32),
        ('flags', ctypes.c_uint8),
        ('reserved', ctypes.c_uint8 * 3),
    ]


class _V4l2BufferM(ctypes.Union):
    _fields_ = [
        ('offset', ctypes.c_uint32),
        ('userptr', ctypes.c_ulong),
        ('planes', ctypes.c_void_p),
        ('fd', ctypes.c_int32),
    ]


class V4l2Buffer(ctypes.Structure):
    """Mirror of ``struct v4l2_buffer`` (size differs 32- vs 64-bit)."""

    _fields_ = [
        ('index', ctypes.c_uint32),
        ('type', ctypes.c_uint32),
        ('bytesused', ctypes.c_uint32),
        ('flags', ctypes.c_uint32),
        ('field', ctypes.c_uint32),
        ('timestamp', Timeval),
        ('timecode', V4l2Timecode),
        ('sequence', ctypes.c_uint32),
        ('memory', ctypes.c_uint32),
        ('m', _V4l2BufferM),
        ('length', ctypes.c_uint32),
        ('reserved2', ctypes.c_uint32),
        ('request_fd', ctypes.c_int32),
    ]


class V4l2EventSubscription(ctypes.Structure):
    """Mirror of ``struct v4l2_event_subscription``."""

    _fields_ = [
        ('type', ctypes.c_uint32),
        ('id', ctypes.c_uint32),
        ('flags', ctypes.c_uint32),
        ('reserved', ctypes.c_uint32 * 5),
    ]


class _V4l2EventUnion(ctypes.Union):
    # The kernel's union has an __s64 member (v4l2_event_ctrl.value64), so it
    # is 8-byte aligned and lands at offset 8 in v4l2_event (after 4 bytes of
    # padding past `type`). The _align member forces that same alignment here
    # so ev.u.data starts where the kernel actually wrote the payload.
    _fields_ = [('data', ctypes.c_uint8 * 64), ('_align', ctypes.c_uint64)]


class V4l2Event(ctypes.Structure):
    """Mirror of ``struct v4l2_event`` (VIDIOC_DQEVENT)."""

    _fields_ = [
        ('type', ctypes.c_uint32),
        ('u', _V4l2EventUnion),
        ('pending', ctypes.c_uint32),
        ('sequence', ctypes.c_uint32),
        ('timestamp', Timespec),
        ('id', ctypes.c_uint32),
        ('reserved', ctypes.c_uint32 * 8),
    ]


class UsbCtrlRequest(ctypes.Structure):
    """Mirror of ``struct usb_ctrlrequest`` (UVC control setup packet)."""

    _pack_ = 1
    _fields_ = [
        ('bRequestType', ctypes.c_uint8),
        ('bRequest', ctypes.c_uint8),
        ('wValue', ctypes.c_uint16),
        ('wIndex', ctypes.c_uint16),
        ('wLength', ctypes.c_uint16),
    ]


class UvcRequestData(ctypes.Structure):
    """Mirror of ``struct uvc_request_data`` (UVCIOC_SEND_RESPONSE)."""

    _fields_ = [('length', ctypes.c_int32), ('data', ctypes.c_uint8 * 60)]


class UvcStreamingControl(ctypes.Structure):
    """Mirror of ``struct uvc_streaming_control`` (PROBE/COMMIT payload)."""

    _pack_ = 1
    _fields_ = [
        ('bmHint', ctypes.c_uint16),
        ('bFormatIndex', ctypes.c_uint8),
        ('bFrameIndex', ctypes.c_uint8),
        ('dwFrameInterval', ctypes.c_uint32),
        ('wKeyFrameRate', ctypes.c_uint16),
        ('wPFrameRate', ctypes.c_uint16),
        ('wCompQuality', ctypes.c_uint16),
        ('wCompWindowSize', ctypes.c_uint16),
        ('wDelay', ctypes.c_uint16),
        ('dwMaxVideoFrameSize', ctypes.c_uint32),
        ('dwMaxPayloadTransferSize', ctypes.c_uint32),
        ('dwClockFrequency', ctypes.c_uint32),
        ('bmFramingInfo', ctypes.c_uint8),
        ('bPreferedVersion', ctypes.c_uint8),
        ('bMinVersion', ctypes.c_uint8),
        ('bMaxVersion', ctypes.c_uint8),
    ]


# ioctl numbers (built after the structs they reference exist).
VIDIOC_QUERYCAP = _ior('V', 0, V4l2Capability)
VIDIOC_S_FMT = _iowr('V', 5, V4l2Format)
VIDIOC_REQBUFS = _iowr('V', 8, V4l2Requestbuffers)
VIDIOC_QUERYBUF = _iowr('V', 9, V4l2Buffer)
VIDIOC_QBUF = _iowr('V', 15, V4l2Buffer)
VIDIOC_DQBUF = _iowr('V', 17, V4l2Buffer)
VIDIOC_STREAMON = _iow('V', 18, ctypes.c_int)
VIDIOC_STREAMOFF = _iow('V', 19, ctypes.c_int)
VIDIOC_DQEVENT = _ior('V', 89, V4l2Event)
VIDIOC_SUBSCRIBE_EVENT = _iow('V', 90, V4l2EventSubscription)
UVCIOC_SEND_RESPONSE = _iow('U', 1, UvcRequestData)

# Frame-gating queues at most one buffer per new camera frame, so a small
# ring (one transmitting, one just-filled, a couple spare) is plenty — and at
# the full-sensor envelope each buffer is large, so we keep the count low.
_NUM_BUFFERS = 4


class UvcGadget(threading.Thread):
    """Pump camera JPEGs into the UVC gadget node in a background thread.

    The host (UVC consumer) chooses the resolution/fps from the advertised
    set (:func:`gadget_frames`); the pump honours that ``bFrameIndex``, sizes
    its buffers to the chosen frame, and asks the coordinator to reconfigure
    picamera2 to match — no descriptor rewrite or USB re-enumeration.

    Args:
        device: gadget V4L2 output node, e.g. ``/dev/video0``.
        frame_buffer: the shared :class:`StreamingOutput`; the pump reads
            ``.frame`` (latest JPEG bytes), ``.frame_counter`` (to detect a
            new frame) and waits on ``.condition`` to pace delivery.
        on_host_format: optional ``callback(width, height, fps)`` invoked when
            the host COMMITs a format, so the camera can be reconfigured.
        on_stream_state: optional ``callback(active: bool)`` invoked when the
            host opens (True) / closes (False) the UVC stream, so the HTTP
            server can yield the camera while it is bound as a webcam.
        controller: optional :class:`CameraController`; when given, the host's
            UVC VideoControl requests (brightness/exposure/focus/...) are mapped
            to libcamera controls via a :class:`UvcControlBridge`.
    """

    def __init__(self, device, frame_buffer, on_host_format=None, on_stream_state=None, controller=None):
        """Bind the pump to a gadget node, the frame buffer and coordinator callbacks."""
        super().__init__(name='uvc-gadget', daemon=True)
        self._device = device
        self._frame_buffer = frame_buffer
        self._on_host_format = on_host_format
        self._on_stream_state = on_stream_state
        self._bridge = UvcControlBridge(controller) if controller is not None else None
        # eventfd the frame buffer pokes on every new frame, so the select()
        # loop can block on "new frame OR V4L2 event" with no timeout/poll.
        self._wake_fd = os.eventfd(0, os.EFD_NONBLOCK | os.EFD_CLOEXEC)
        # Frames, per-frame intervals, per-frame max buffer size and iso
        # maxpacket all come from configfs (the gadget the kernel actually
        # advertised), never hardcoded here.
        self._frames, self._frame_intervals, self._frame_sizes, self._iso_maxpacket = self._load_descriptors()
        self._frame_index = _default_frame_index(self._frames)  # 1-based bFrameIndex
        self._interval = self._frame_intervals[self._frame_index - 1][0]  # default frame, fastest
        self._width, self._height = self._frames[self._frame_index - 1]
        # V4L2 output buffers are sized to the committed frame's advertised
        # dwMaxVideoFrameBufferSize at STREAMON (so the device can hold any frame
        # up to what it told the host).
        self._max_frame = self._frame_sizes[self._frame_index - 1]
        self._fd = -1
        self._stop = threading.Event()
        self._streaming = False
        self._buffers = []  # list of mmap objects, indexed by buffer index
        self._free = []  # indices of buffers we own and may fill/queue
        self._last_counter = -1  # frame_counter last pushed to the gadget
        self._probe = self._control_for(self._frame_index, self._interval)
        self._commit = self._control_for(self._frame_index, self._interval)
        self._pending_cs = UVC_VS_PROBE_CONTROL  # selector of an in-flight VS SET_CUR
        self._pending_vc = None  # (unit, selector) of an in-flight VC SET_CUR
        self._dqevent_fails = 0
        self._empty_fills = 0
        self._dqbuf_errs = 0

    @staticmethod
    def _load_descriptors():
        """Frames, per-frame intervals and iso maxpacket, read from configfs.

        configfs (written by rpi-cam-gadget-setup.sh) is the source of truth, so
        PROBE/COMMIT can never advertise a frame/interval/payload size the kernel
        didn't. Falls back to the built-in defaults only if configfs is absent or
        unreadable (logged) — e.g. when run outside the gadget image.
        """
        base = gadget_configfs.find_uvc_function()
        if base is not None:
            try:
                frames, intervals, frame_sizes, maxpacket = gadget_configfs.read_streaming(base)
                log.info(
                    'UVC: descriptors from configfs: %d frames, maxpacket=%d',
                    len(frames),
                    maxpacket,
                )
                return frames, intervals, frame_sizes, maxpacket
            except (OSError, ValueError) as exc:
                log.warning('UVC: configfs read failed (%s); using built-in defaults', exc)
        else:
            log.warning('UVC: no configfs UVC function found; using built-in defaults')
        frames = gadget_frames()
        intervals = [list(FRAME_INTERVALS) for _ in frames]
        # 1 byte/pixel MJPEG ceiling, matching the historical buffer sizing.
        frame_sizes = [w * h for (w, h) in frames]
        return frames, intervals, frame_sizes, _ISO_MAXPACKET

    @staticmethod
    def find_device():
        """Return the gadget's UVC output node, or None.

        The Pi camera capture nodes report ``V4L2_CAP_VIDEO_CAPTURE``; the
        UVC gadget side is the one reporting ``V4L2_CAP_VIDEO_OUTPUT``. Scan
        ``/dev/video*`` and return the first output-capable node.
        """
        try:
            nodes = sorted(n for n in os.listdir('/dev') if n.startswith('video') and n[5:].isdigit())
        except OSError:
            return None
        for name in nodes:
            path = '/dev/' + name
            fd = -1
            try:
                fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                cap = V4l2Capability()
                fcntl.ioctl(fd, VIDIOC_QUERYCAP, cap)
                caps = cap.device_caps if (cap.capabilities & V4L2_CAP_DEVICE_CAPS) else cap.capabilities
                if caps & V4L2_CAP_VIDEO_OUTPUT:
                    return path
            except OSError:
                continue
            finally:
                if fd >= 0:
                    os.close(fd)
        return None

    # -- control negotiation ------------------------------------------
    def _control_for(self, frame_index, interval):
        """Build a streaming control for a given (1-based) frame index + interval."""
        frame_index = max(1, min(len(self._frames), frame_index))
        width, height = self._frames[frame_index - 1]
        ctrl = UvcStreamingControl()
        ctrl.bmHint = 1
        ctrl.bFormatIndex = 1  # the gadget advertises a single MJPEG format -> index 1
        ctrl.bFrameIndex = frame_index
        ctrl.dwFrameInterval = _snap_interval(interval, self._frame_intervals[frame_index - 1])
        # Must equal the frame descriptor's dwMaxVideoFrameBufferSize (configfs).
        ctrl.dwMaxVideoFrameSize = self._frame_sizes[frame_index - 1]
        # Match the iso endpoint (streaming_maxpacket, read from configfs) so the
        # host sizes its payload requests correctly, and advertise the 48 MHz UVC
        # clock the gadget uses for presentation timestamps.
        ctrl.dwMaxPayloadTransferSize = self._iso_maxpacket
        ctrl.dwClockFrequency = 48_000_000
        ctrl.bmFramingInfo = 3
        ctrl.bPreferedVersion = 1
        ctrl.bMinVersion = 1
        ctrl.bMaxVersion = 1
        return ctrl

    def stop(self):
        """Signal the thread to stop and wake the select loop."""
        self._stop.set()
        try:
            os.eventfd_write(self._wake_fd, 1)  # break the blocking select()
        except OSError:
            pass

    # -- thread entry --------------------------------------------------
    def run(self):
        """Open the gadget node and run the event/streaming loop."""
        try:
            self._fd = os.open(self._device, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            log.error('UVC: cannot open %s (%s); pump disabled', self._device, exc)
            self._close_wake()
            return
        # Get woken on every new camera frame via the eventfd.
        self._frame_buffer.add_wake_fd(self._wake_fd)
        try:
            self._subscribe_events()
            log.info('UVC: subscribed to events on %s, entering loop', self._device)
            self._loop()
        except Exception:  # never let the pump take down the process
            log.exception('UVC: pump thread crashed; gadget streaming disabled')
        finally:
            self._frame_buffer.remove_wake_fd(self._wake_fd)
            self._teardown_stream()
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            self._close_wake()

    def _close_wake(self):
        """Close the wake eventfd (idempotent)."""
        if self._wake_fd >= 0:
            try:
                os.close(self._wake_fd)
            except OSError:
                pass
            self._wake_fd = -1

    def _subscribe_events(self):
        for ev in (
            UVC_EVENT_SETUP,
            UVC_EVENT_DATA,
            UVC_EVENT_STREAMON,
            UVC_EVENT_STREAMOFF,
            UVC_EVENT_DISCONNECT,
        ):
            sub = V4l2EventSubscription()
            sub.type = ev
            # Tolerate a single unsupported event rather than killing the whole
            # pump: subscribe each independently and warn-and-continue on error.
            try:
                fcntl.ioctl(self._fd, VIDIOC_SUBSCRIBE_EVENT, sub)
            except OSError as exc:
                log.warning(
                    'UVC: could not subscribe event %s (%s); continuing',
                    _EVENT_NAMES.get(ev, hex(ev)),
                    exc,
                )

    def _loop(self):
        fb = self._frame_buffer
        while not self._stop.is_set():
            # One blocking wait on both sources: V4L2 events arrive on the
            # exception set; a new camera frame (or stop()) pokes the eventfd
            # on the read set. No timeout, no poll — the loop only wakes when
            # there is actually something to do.
            try:
                ready_r, _, ready_x = select.select([self._wake_fd], [], [self._fd])
            except OSError:
                break
            if ready_x:
                self._handle_event()
                # ENOTTY on every DQEVENT means our v4l2_event size is wrong
                # for this kernel; bail rather than spin the CPU forever.
                if self._dqevent_fails > 20:
                    log.error('UVC: giving up after repeated DQEVENT failures; pump stopped')
                    return
            if ready_r:
                self._drain_wake()
            # Re-check _streaming: a STREAMOFF handled just above may have
            # torn the stream down, so DQBUF/QBUF would raise EINVAL.
            if not self._streaming:
                continue
            # Reclaim every buffer the gadget has finished transmitting, then
            # push the current frame — but only once per *new* camera frame.
            # Between frames the queue is left to drain and the gadget sends
            # zero-length iso packets (normal webcam underrun), so we neither
            # burn CPU re-copying nor re-send duplicate frames over USB.
            self._reclaim_buffers()
            counter = fb.frame_counter
            frame = fb.frame
            if frame and counter != self._last_counter:
                if self._push_frame(frame):
                    self._last_counter = counter

    def _drain_wake(self):
        """Clear the wake eventfd (one read resets its accumulated counter)."""
        try:
            os.eventfd_read(self._wake_fd)
        except OSError:
            pass

    # -- event handling ------------------------------------------------
    def _handle_event(self):
        ev = V4l2Event()
        try:
            fcntl.ioctl(self._fd, VIDIOC_DQEVENT, ev)
        except OSError as exc:
            self._dqevent_fails += 1
            if self._dqevent_fails == 1:
                log.warning(
                    'UVC: DQEVENT failed (%s); v4l2_event size=%d may be wrong',
                    exc,
                    ctypes.sizeof(V4l2Event),
                )
            return
        self._dqevent_fails = 0

        log.debug('UVC: event %s', _EVENT_NAMES.get(ev.type, hex(ev.type)))
        if ev.type == UVC_EVENT_SETUP:
            req = UsbCtrlRequest.from_buffer_copy(bytes(ev.u.data)[:ctypes.sizeof(UsbCtrlRequest)])
            self._handle_setup(req)
        elif ev.type == UVC_EVENT_DATA:
            data = UvcRequestData.from_buffer_copy(bytes(ev.u.data)[:ctypes.sizeof(UvcRequestData)])
            self._handle_data(data)
        elif ev.type == UVC_EVENT_STREAMON:
            self._start_stream()
        elif ev.type == UVC_EVENT_STREAMOFF:
            self._teardown_stream()
        elif ev.type == UVC_EVENT_DISCONNECT:
            self._teardown_stream()

    def _handle_setup(self, req):
        cs = (req.wValue >> 8) & 0xFF  # control selector in the high byte
        entity = (req.wIndex >> 8) & 0xFF  # unit/terminal id (0 == interface)
        is_class = (req.bRequestType & USB_TYPE_MASK) == USB_TYPE_CLASS
        log.debug(
            'UVC: setup bmReqType=0x%02x req=%s entity=%d cs=%d wLength=%d',
            req.bRequestType,
            _REQ_NAMES.get(req.bRequest, hex(req.bRequest)),
            entity,
            cs,
            req.wLength,
        )
        if not is_class:
            # Only class requests are ours; stall anything else.
            self._send_response(b'')
            return
        if entity != 0:
            # A VideoControl unit/terminal request (brightness, exposure, ...).
            self._handle_vc_setup(req, entity, cs)
            return
        self._handle_vs_setup(req, cs)

    def _handle_vs_setup(self, req, cs):
        # VideoStreaming interface: PROBE/COMMIT resolution+fps negotiation.
        if req.bRequest == UVC_SET_CUR:
            # Accept the data stage; the payload arrives as a UVC_EVENT_DATA.
            self._pending_cs = cs
            self._pending_vc = None
            self._send_response(bytes(ctypes.sizeof(UvcStreamingControl)))
            return
        # GET_* requests: report the negotiable range so the host can enumerate
        # resolutions, and the current/default selection.
        if cs in (UVC_VS_PROBE_CONTROL, UVC_VS_COMMIT_CONTROL):
            if req.bRequest == UVC_GET_LEN:
                self._send_response(struct.pack('<H', ctypes.sizeof(UvcStreamingControl)))
            elif req.bRequest == UVC_GET_INFO:
                self._send_response(b'\x03')  # GET/SET supported
            elif req.bRequest == UVC_GET_MIN:
                # Smallest frame, its fastest interval.
                self._send_response(bytes(self._control_for(1, self._frame_intervals[0][0])))
            elif req.bRequest == UVC_GET_MAX:
                # Largest frame, its slowest interval.
                self._send_response(bytes(self._control_for(len(self._frames), self._frame_intervals[-1][-1])))
            elif req.bRequest == UVC_GET_DEF:
                di = _default_frame_index(self._frames)
                self._send_response(bytes(self._control_for(di, self._frame_intervals[di - 1][0])))
            elif req.bRequest == UVC_GET_CUR:
                # Current selection for the addressed control: the host reads the
                # PROBE control while negotiating and the COMMIT control after.
                ctrl = self._commit if cs == UVC_VS_COMMIT_CONTROL else self._probe
                self._send_response(bytes(ctrl))
            else:  # GET_RES — step size; none of our control fields are steppable.
                self._send_response(bytes(UvcStreamingControl()))
        else:
            self._send_response(b'')

    def _handle_vc_setup(self, req, entity, cs):
        # VideoControl unit/terminal request -> the camera-control bridge.
        if self._bridge is None or not self._bridge.handles(entity, cs):
            self._send_response(b'')  # not advertised — stall
            return
        if req.bRequest == UVC_SET_CUR:
            # Accept the data stage; payload arrives as a UVC_EVENT_DATA.
            self._pending_vc = (entity, cs)
            self._pending_cs = None
            self._send_response(bytes(self._bridge.length(entity, cs)))
            return
        resp = self._bridge.get(entity, cs, req.bRequest)
        self._send_response(resp if resp is not None else b'')

    def _handle_data(self, data):
        # Payload of a SET_CUR. Route to the control bridge if it was a
        # VideoControl request, otherwise interpret it as PROBE/COMMIT.
        if self._pending_vc is not None:
            entity, cs = self._pending_vc
            self._pending_vc = None
            raw = bytes(data.data)[:max(0, min(data.length, len(data.data)))]
            self._bridge.set_cur(entity, cs, raw)
            return
        # PROBE/COMMIT: read the host's chosen frame index + interval, clamp to
        # what we advertise, and echo back a consistent control.
        cs = self._pending_cs
        length = min(max(0, data.length), ctypes.sizeof(UvcStreamingControl))
        raw = bytes(data.data)[:length]
        req = UvcStreamingControl.from_buffer_copy(
            raw + b'\x00' * (ctypes.sizeof(UvcStreamingControl) - len(raw)),
        )
        frame_index = req.bFrameIndex if 1 <= req.bFrameIndex <= len(self._frames) else self._frame_index
        # _control_for snaps the requested interval to one this frame advertises.
        ctrl = self._control_for(frame_index, req.dwFrameInterval)
        interval = ctrl.dwFrameInterval
        log.debug(
            'UVC: %s data -> frame %d %dx%d @ %d fps',
            'COMMIT' if cs == UVC_VS_COMMIT_CONTROL else 'PROBE',
            frame_index,
            *self._frames[frame_index - 1],
            _fps_from_interval(interval),
        )
        if cs == UVC_VS_COMMIT_CONTROL:
            self._commit = ctrl
            self._frame_index = frame_index
            self._interval = interval
            self._width, self._height = self._frames[frame_index - 1]
            # Drive the camera to the host's choice (head start before STREAMON).
            if self._on_host_format is not None:
                try:
                    self._on_host_format(self._width, self._height, _fps_from_interval(interval))
                except Exception:
                    log.exception('UVC: on_host_format callback failed')
        else:
            self._probe = ctrl

    def _send_response(self, payload):
        resp = UvcRequestData()
        resp.length = len(payload)
        for i, byte in enumerate(payload[:60]):
            resp.data[i] = byte
        try:
            fcntl.ioctl(self._fd, UVCIOC_SEND_RESPONSE, resp)
        except OSError as exc:
            log.debug('UVC: send_response failed: %s', exc)

    # -- streaming -----------------------------------------------------
    def _notify_stream(self, active):
        """Fire the stream-state callback, swallowing any error."""
        if self._on_stream_state is None:
            return
        try:
            self._on_stream_state(active)
        except Exception:
            log.exception('UVC: on_stream_state(%s) callback failed', active)

    def _start_stream(self):
        if self._streaming:
            return
        try:
            # Size buffers to the committed frame's advertised max buffer size
            # (dwMaxVideoFrameBufferSize from configfs), matching what we told
            # the host in dwMaxVideoFrameSize. _frame_index is set in _handle_data.
            self._max_frame = self._frame_sizes[self._frame_index - 1]
            frame = self._frame_buffer.frame
            log.info(
                'UVC: frame source at streamon: %s',
                ('%d bytes' % len(frame)) if frame else 'EMPTY/None',
            )
            self._set_format()
            log.debug('UVC: S_FMT ok')
            self._request_buffers(_NUM_BUFFERS)
            log.debug('UVC: REQBUFS got %d buffers', len(self._buffers))
            # Prime a single buffer with the current frame so the host gets an
            # immediate first image; the rest stay free for the pacing loop,
            # which queues one buffer per new camera frame.
            if frame:
                self._push_frame(frame)
                self._last_counter = self._frame_buffer.frame_counter
            on = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_OUTPUT)
            fcntl.ioctl(self._fd, VIDIOC_STREAMON, on)
            self._streaming = True
            log.info(
                'UVC: streaming started %dx%d @ %d fps',
                self._width,
                self._height,
                _fps_from_interval(self._interval),
            )
            self._notify_stream(True)
        except OSError as exc:
            log.error('UVC: failed to start streaming: %s', exc)
            self._teardown_stream()

    def _set_format(self):
        fmt = V4l2Format()
        fmt.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
        fmt.fmt.pix.width = self._width
        fmt.fmt.pix.height = self._height
        fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG
        fmt.fmt.pix.field = V4L2_FIELD_NONE
        fmt.fmt.pix.sizeimage = self._max_frame
        fcntl.ioctl(self._fd, VIDIOC_S_FMT, fmt)

    def _request_buffers(self, count):
        req = V4l2Requestbuffers()
        req.count = count
        req.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
        req.memory = V4L2_MEMORY_MMAP
        fcntl.ioctl(self._fd, VIDIOC_REQBUFS, req)
        self._buffers = []
        self._free = list(range(req.count))
        for index in range(req.count):
            buf = V4l2Buffer()
            buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
            buf.memory = V4L2_MEMORY_MMAP
            buf.index = index
            fcntl.ioctl(self._fd, VIDIOC_QUERYBUF, buf)
            if index == 0:
                log.debug(
                    'UVC: QUERYBUF len=%d off=%d sizeof=%d raw=%s',
                    buf.length,
                    buf.m.offset,
                    ctypes.sizeof(V4l2Buffer),
                    bytes(buf).hex(),
                )
            mm = mmap.mmap(
                self._fd,
                buf.length,
                flags=mmap.MAP_SHARED,
                prot=mmap.PROT_READ | mmap.PROT_WRITE,
                offset=buf.m.offset,
            )
            self._buffers.append(mm)

    def _fill_buffer(self, index, frame):
        # len(mm) is the mapped length; mm.size() is the backing file size,
        # which is 0 for a V4L2 device fd — using it here yielded 0-byte frames.
        mm = self._buffers[index]
        size = min(len(frame), len(mm))
        mm.seek(0)
        # memoryview slice is zero-copy; mm.write then does the single copy
        # into the mapping. Slicing `frame[:size]` would allocate and copy the
        # whole JPEG first — wasteful per-frame at video rates.
        mm.write(memoryview(frame)[:size])
        return size

    def _push_frame(self, frame):
        # Queue exactly one free buffer with this frame. If none is free (the
        # gadget hasn't returned a buffer yet) we simply drop the frame — a
        # webcam skipping a frame is harmless and avoids any blocking wait.
        if not self._free:
            self._empty_fills += 1
            if self._empty_fills <= 3 or self._empty_fills % 300 == 0:
                log.debug('UVC: no free buffer for frame (#%d)', self._empty_fills)
            return False
        index = self._free.pop(0)
        size = self._fill_buffer(index, frame)
        buf = V4l2Buffer()
        buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
        buf.memory = V4L2_MEMORY_MMAP
        buf.index = index
        buf.bytesused = size  # must reflect the JPEG length we wrote
        buf.length = len(self._buffers[index])
        try:
            fcntl.ioctl(self._fd, VIDIOC_QBUF, buf)
        except OSError as exc:
            log.debug('UVC: QBUF failed: %s', exc)
            self._free.append(index)  # hand the buffer back
            return False
        return True

    def _reclaim_buffers(self):
        # Drain every buffer the gadget has finished transmitting back onto
        # the free list so the next new frame can reuse it.
        while True:
            buf = V4l2Buffer()
            buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
            buf.memory = V4L2_MEMORY_MMAP
            try:
                fcntl.ioctl(self._fd, VIDIOC_DQBUF, buf)
            except OSError as exc:
                if exc.errno == errno.EAGAIN:
                    return  # no more finished buffers — normal
                self._dqbuf_errs += 1
                if self._dqbuf_errs <= 3:
                    log.warning('UVC: DQBUF failed: %s', exc)
                if self._dqbuf_errs > 10 and self._streaming:
                    log.error('UVC: too many DQBUF failures; stopping stream to avoid CPU starvation')
                    self._teardown_stream()
                return
            self._dqbuf_errs = 0
            if buf.index not in self._free:
                self._free.append(buf.index)

    def _teardown_stream(self):
        if self._fd < 0:
            return
        was_streaming = self._streaming
        if self._streaming:
            try:
                off = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_OUTPUT)
                fcntl.ioctl(self._fd, VIDIOC_STREAMOFF, off)
            except OSError:
                pass
            self._streaming = False
        if was_streaming:
            self._notify_stream(False)
        self._release_buffers()

    def _release_buffers(self):
        """Unmap the buffers and free the V4L2 queue (REQBUFS count=0)."""
        for mm in self._buffers:
            try:
                mm.close()
            except (OSError, ValueError):
                pass
        self._buffers = []
        self._free = []
        self._last_counter = -1
        try:
            req = V4l2Requestbuffers()
            req.count = 0
            req.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
            req.memory = V4L2_MEMORY_MMAP
            fcntl.ioctl(self._fd, VIDIOC_REQBUFS, req)
        except OSError:
            pass
