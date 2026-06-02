# -*- coding: utf-8 -*-
"""Userspace pump for the Linux UVC gadget (``g_uvc`` / configfs ``uvc`` function).

When the Pi is configured as a USB UVC webcam the kernel exposes a V4L2
**output** node (``/dev/videoN``) for the gadget side. The host's UVC
driver negotiates a format over the control endpoint and then pulls video
frames; the kernel forwards both as events on that node and expects a
userspace process to (a) answer the PROBE/COMMIT control negotiation and
(b) feed frames into queued buffers. This module is that process.

Scope (MVP, plan step 6): a **single MJPEG format** at one fixed
resolution — no dynamic resolution/fps reconfiguration, no governor. The
descriptors are written by the image's gadget-setup script; here we only
answer with a matching streaming-control and pump the latest JPEG from
the camera's frame buffer.

This is intricate kernel-ABI code. ioctl numbers are computed at runtime
from ``ctypes.sizeof`` so they are correct for whatever ABI we run on
(the target is 32-bit armhf). It is a faithful, compact port of the
kernel ``tools/usb/uvc-gadget.c`` select-loop. Any failure logs and stops
the pump thread without touching the HTTP/MJPEG server.
"""

import ctypes
import fcntl
import logging
import mmap
import os
import select
import struct
import threading

log = logging.getLogger(__name__)

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
    _fields_ = [('data', ctypes.c_uint8 * 64)]


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

_NUM_BUFFERS = 4


class UvcGadget(threading.Thread):
    """Pump camera JPEGs into the UVC gadget node in a background thread.

    Args:
        device: gadget V4L2 output node, e.g. ``/dev/video0``.
        width, height: the single advertised frame size (must match the
            descriptors written into configfs by the gadget-setup script).
        fps: frames per second for the advertised ``dwFrameInterval``.
        get_frame: callable returning the latest JPEG bytes (or ``None``).
    """

    def __init__(self, device, width, height, fps, get_frame):
        """Bind the pump to a gadget node, resolution and frame source."""
        super().__init__(name='uvc-gadget', daemon=True)
        self._device = device
        self._width = int(width)
        self._height = int(height)
        self._interval = max(1, int(10_000_000 // max(1, int(fps))))  # 100 ns units
        self._max_frame = self._width * self._height * 2  # generous MJPEG bound
        self._get_frame = get_frame
        self._fd = -1
        self._stop = threading.Event()
        self._streaming = False
        self._buffers = []  # list of mmap objects
        self._probe = self._make_streaming_control()
        self._commit = self._make_streaming_control()
        self._dqevent_fails = 0

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
    def _make_streaming_control(self):
        ctrl = UvcStreamingControl()
        ctrl.bmHint = 1
        ctrl.bFormatIndex = 1
        ctrl.bFrameIndex = 1
        ctrl.dwFrameInterval = self._interval
        ctrl.dwMaxVideoFrameSize = self._max_frame
        ctrl.dwMaxPayloadTransferSize = 1024
        ctrl.bmFramingInfo = 3
        ctrl.bPreferedVersion = 1
        ctrl.bMinVersion = 1
        ctrl.bMaxVersion = 1
        return ctrl

    def stop(self):
        """Signal the thread to stop and wake the select loop."""
        self._stop.set()

    # -- thread entry --------------------------------------------------
    def run(self):
        """Open the gadget node and run the event/streaming loop."""
        try:
            self._fd = os.open(self._device, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            log.error('UVC: cannot open %s (%s); pump disabled', self._device, exc)
            return
        try:
            self._subscribe_events()
            log.info('UVC: subscribed to events on %s, entering loop', self._device)
            self._loop()
        except Exception:  # never let the pump take down the process
            log.exception('UVC: pump thread crashed; gadget streaming disabled')
        finally:
            self._teardown_stream()
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1

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
            fcntl.ioctl(self._fd, VIDIOC_SUBSCRIBE_EVENT, sub)

    def _loop(self):
        while not self._stop.is_set():
            wfds = [self._fd] if self._streaming else []
            # Exception set carries V4L2 events; write set carries buffer
            # readiness on the output node.
            try:
                _, ready_w, ready_x = select.select([], wfds, [self._fd], 0.5)
            except OSError:
                break
            if ready_x:
                self._handle_event()
                # ENOTTY on every DQEVENT means our v4l2_event size is wrong
                # for this kernel; bail rather than spin the CPU forever.
                if self._dqevent_fails > 20:
                    log.error('UVC: giving up after repeated DQEVENT failures; pump stopped')
                    return
            if ready_w:
                self._process_frame()

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

        log.info('UVC: event %s', _EVENT_NAMES.get(ev.type, hex(ev.type)))
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
        is_class = (req.bRequestType & USB_TYPE_MASK) == USB_TYPE_CLASS
        log.info(
            'UVC: setup bmReqType=0x%02x req=%s cs=%d iface=%d wLength=%d',
            req.bRequestType,
            _REQ_NAMES.get(req.bRequest, hex(req.bRequest)),
            cs,
            req.wIndex & 0xFF,
            req.wLength,
        )
        if not is_class:
            # We only drive the streaming-interface class requests; stall
            # anything else by sending a zero-length response.
            self._send_response(b'')
            return
        if req.bRequest == UVC_SET_CUR:
            # Accept the data stage (length = control size); the payload
            # arrives next as a UVC_EVENT_DATA.
            self._pending_cs = cs
            self._send_response(bytes(ctypes.sizeof(UvcStreamingControl)))
            return
        # GET_* requests: answer PROBE/COMMIT with our single streaming control.
        if cs in (UVC_VS_PROBE_CONTROL, UVC_VS_COMMIT_CONTROL):
            if req.bRequest == UVC_GET_LEN:
                self._send_response(struct.pack('<H', ctypes.sizeof(UvcStreamingControl)))
            elif req.bRequest == UVC_GET_INFO:
                self._send_response(b'\x03')  # GET/SET supported
            else:  # GET_CUR/MIN/MAX/DEF/RES
                self._send_response(bytes(self._probe))
        else:
            self._send_response(b'')

    def _handle_data(self, data):
        # Payload of a SET_CUR on PROBE or COMMIT — accept and mirror it.
        cs = getattr(self, '_pending_cs', UVC_VS_PROBE_CONTROL)
        log.info('UVC: data for cs=%d length=%d', cs, data.length)
        length = min(max(0, data.length), ctypes.sizeof(UvcStreamingControl))
        raw = bytes(data.data)[:length]
        ctrl = UvcStreamingControl.from_buffer_copy(
            raw + b'\x00' * (ctypes.sizeof(UvcStreamingControl) - len(raw)),
        )
        # Force our single supported format/frame regardless of host choice.
        ctrl.bFormatIndex = 1
        ctrl.bFrameIndex = 1
        ctrl.dwFrameInterval = self._interval
        ctrl.dwMaxVideoFrameSize = self._max_frame
        if cs == UVC_VS_COMMIT_CONTROL:
            self._commit = ctrl
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
    def _start_stream(self):
        if self._streaming:
            return
        try:
            self._set_format()
            log.info('UVC: S_FMT ok')
            self._request_buffers(_NUM_BUFFERS)
            log.info('UVC: REQBUFS got %d buffers', len(self._buffers))
            # Queue every buffer (each is filled with the latest frame).
            for index in range(len(self._buffers)):
                self._queue_buffer(index)
            on = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_OUTPUT)
            fcntl.ioctl(self._fd, VIDIOC_STREAMON, on)
            self._streaming = True
            log.info('UVC: streaming %dx%d started', self._width, self._height)
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
        for index in range(req.count):
            buf = V4l2Buffer()
            buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
            buf.memory = V4L2_MEMORY_MMAP
            buf.index = index
            fcntl.ioctl(self._fd, VIDIOC_QUERYBUF, buf)
            mm = mmap.mmap(
                self._fd,
                buf.length,
                flags=mmap.MAP_SHARED,
                prot=mmap.PROT_READ | mmap.PROT_WRITE,
                offset=buf.m.offset,
            )
            self._buffers.append(mm)

    def _fill_buffer(self, index):
        frame = None
        try:
            frame = self._get_frame()
        except Exception:
            frame = None
        mm = self._buffers[index]
        if not frame:
            return 0
        size = min(len(frame), mm.size())
        mm.seek(0)
        mm.write(frame[:size])
        return size

    def _queue_buffer(self, index):
        buf = V4l2Buffer()
        buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
        buf.memory = V4L2_MEMORY_MMAP
        buf.index = index
        # bytesused must reflect the JPEG length we wrote.
        frame_len = self._fill_buffer(index)
        buf.bytesused = frame_len
        buf.length = self._buffers[index].size()
        fcntl.ioctl(self._fd, VIDIOC_QBUF, buf)

    def _process_frame(self):
        buf = V4l2Buffer()
        buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
        buf.memory = V4L2_MEMORY_MMAP
        try:
            fcntl.ioctl(self._fd, VIDIOC_DQBUF, buf)
        except OSError:
            return
        # Refill the just-consumed buffer with the newest frame and requeue.
        self._queue_buffer(buf.index)

    def _teardown_stream(self):
        if self._fd < 0:
            return
        if self._streaming:
            try:
                off = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_OUTPUT)
                fcntl.ioctl(self._fd, VIDIOC_STREAMOFF, off)
            except OSError:
                pass
            self._streaming = False
        for mm in self._buffers:
            try:
                mm.close()
            except (OSError, ValueError):
                pass
        self._buffers = []
        try:
            req = V4l2Requestbuffers()
            req.count = 0
            req.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
            req.memory = V4L2_MEMORY_MMAP
            fcntl.ioctl(self._fd, VIDIOC_REQBUFS, req)
        except OSError:
            pass
