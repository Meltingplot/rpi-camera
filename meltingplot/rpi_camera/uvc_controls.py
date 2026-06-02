# -*- coding: utf-8 -*-
"""Map UVC Camera-Terminal / Processing-Unit controls to libcamera controls.

When the Pi runs as a USB UVC webcam the host drives the standard webcam
controls (brightness, exposure, focus, ...) over the VideoControl interface.
The kernel forwards each ``GET_*``/``SET_CUR`` as a control-endpoint request;
the pump (:mod:`uvc_gadget`) routes unit/terminal requests here, and this
bridge translates them to/from the curated libcamera controls in
:mod:`controls`.

Only a curated set is exposed; the configfs ``bmControls`` bitmaps written by
``rpi-cam-gadget-setup.sh`` MUST advertise exactly these (unit, selector)
pairs (and use the unit IDs below).

UVC wire values are integers with fixed byte widths; libcamera controls are
floats/enums/bools. Numeric controls are linearly mapped between a fixed UVC
range ``[0, _UVC_SPAN]`` and the sensor's actual ``[min, max]`` (read at
runtime), so the mapping is sensor-independent. Exposure time is special-cased
(UVC unit = 100 us); the booleans (AE mode, focus-auto, white-balance-auto)
have bespoke encodings.
"""

import logging
import struct

log = logging.getLogger(__name__)

# configfs default unit/terminal IDs (rpi-cam-gadget-setup.sh keeps these).
CAMERA_TERMINAL_ID = 1
PROCESSING_UNIT_ID = 2

# UVC class request codes (subset; kept local to avoid importing uvc_gadget).
SET_CUR = 0x01
GET_CUR = 0x81
GET_MIN = 0x82
GET_MAX = 0x83
GET_RES = 0x84
GET_LEN = 0x85
GET_INFO = 0x86
GET_DEF = 0x87

# Camera Terminal (CT) control selectors.
CT_AE_MODE_CONTROL = 0x02
CT_EXPOSURE_TIME_ABSOLUTE_CONTROL = 0x04
CT_FOCUS_ABSOLUTE_CONTROL = 0x06
CT_FOCUS_AUTO_CONTROL = 0x08
# Processing Unit (PU) control selectors.
PU_BRIGHTNESS_CONTROL = 0x02
PU_CONTRAST_CONTROL = 0x03
PU_GAIN_CONTROL = 0x04
PU_SATURATION_CONTROL = 0x07
PU_SHARPNESS_CONTROL = 0x08
PU_WHITE_BALANCE_TEMPERATURE_AUTO_CONTROL = 0x0B

_INFO_GET_SET = 0x03  # GET and SET supported
_UVC_SPAN = 1000  # fixed UVC integer range [0, _UVC_SPAN] for scaled controls
_GET_FIELD = {GET_CUR: 'cur', GET_MIN: 'min', GET_MAX: 'max', GET_RES: 'res', GET_DEF: 'def_'}


class _ScaledControl:
    """Numeric control: linear map between UVC [0, span] and libcamera [min, max]."""

    def __init__(self, controller, name, size=2, signed=False):
        """Bind to a libcamera control name and its UVC wire width."""
        self._c = controller
        self._name = name
        self._size = size
        self._signed = signed

    def _bounds(self):
        b = self._c.bounds(self._name)
        if b is None:
            return (0.0, 1.0, 0.0)
        lmin, lmax = float(b[0]), float(b[1])
        ldef = float(b[2]) if b[2] is not None else lmin
        if lmax <= lmin:
            lmax = lmin + 1.0
        return (lmin, lmax, ldef)

    def _to_uvc(self, lib):
        lmin, lmax, _ = self._bounds()
        return int(round((float(lib) - lmin) / (lmax - lmin) * _UVC_SPAN))

    def _to_lib(self, uvc):
        lmin, lmax, _ = self._bounds()
        clamped = max(0, min(_UVC_SPAN, int(uvc)))
        return lmin + (clamped / _UVC_SPAN) * (lmax - lmin)

    def snapshot(self):
        """Return the current UVC field values for this control."""
        lmin, lmax, ldef = self._bounds()
        return {
            'size': self._size,
            'signed': self._signed,
            'info': _INFO_GET_SET,
            'cur': self._to_uvc(self._c.current(self._name, ldef)),
            'min': 0,
            'max': _UVC_SPAN,
            'res': 1,
            'def_': self._to_uvc(ldef),
        }

    def apply(self, value):
        """Apply a UVC integer value to the libcamera control."""
        self._c.apply({self._name: self._to_lib(value)})


class _ExposureControl:
    """CT Exposure Time (Absolute): UVC unit = 100 us, 4 bytes; libcamera us."""

    def __init__(self, controller):
        """Bind to the controller (reads/writes the ``ExposureTime`` control)."""
        self._c = controller

    def _bounds_us(self):
        b = self._c.bounds('ExposureTime')
        if b is None:
            return (1, 1_000_000, 10_000)
        lo, hi = int(b[0]), int(b[1])
        df = int(b[2]) if b[2] is not None else lo
        return (lo, hi, df)

    def snapshot(self):
        """Return the current UVC field values (in 100 us units)."""
        lo, hi, df = self._bounds_us()
        cur = int(self._c.current('ExposureTime', df))
        return {
            'size': 4,
            'signed': False,
            'info': _INFO_GET_SET,
            'cur': max(1, cur // 100),
            'min': max(1, lo // 100),
            'max': max(1, hi // 100),
            'res': 1,
            'def_': max(1, df // 100),
        }

    def apply(self, value):
        """Apply a UVC exposure value (100 us units) as libcamera microseconds."""
        self._c.apply({'ExposureTime': int(value) * 100})


class _MappedByte:
    """One-byte control with bespoke encode/decode (booleans, AE-mode bitmap)."""

    def __init__(self, getter, setter, default, minimum=0, maximum=1, res=1):
        """Bind getter/setter closures and the advertised min/max/res/default."""
        self._getter = getter
        self._setter = setter
        self._default = default
        self._min = minimum
        self._max = maximum
        self._res = res

    def snapshot(self):
        """Return the current UVC field values for this one-byte control."""
        return {
            'size': 1,
            'signed': False,
            'info': _INFO_GET_SET,
            'cur': int(self._getter()),
            'min': self._min,
            'max': self._max,
            'res': self._res,
            'def_': self._default,
        }

    def apply(self, value):
        """Apply a UVC byte value via the bound setter."""
        self._setter(int(value))


class UvcControlBridge:
    """Translate UVC VideoControl requests to/from the camera controller."""

    def __init__(self, controller):
        """Build the (unit, selector) -> control table for ``controller``."""
        self._table = self._build(controller)

    @staticmethod
    def _build(c):
        table = {
            (PROCESSING_UNIT_ID, PU_BRIGHTNESS_CONTROL):
            _ScaledControl(c, 'Brightness', signed=True),
            (PROCESSING_UNIT_ID, PU_CONTRAST_CONTROL):
            _ScaledControl(c, 'Contrast'),
            (PROCESSING_UNIT_ID, PU_SATURATION_CONTROL):
            _ScaledControl(c, 'Saturation'),
            (PROCESSING_UNIT_ID, PU_SHARPNESS_CONTROL):
            _ScaledControl(c, 'Sharpness'),
            (PROCESSING_UNIT_ID, PU_GAIN_CONTROL):
            _ScaledControl(c, 'AnalogueGain'),
            (PROCESSING_UNIT_ID, PU_WHITE_BALANCE_TEMPERATURE_AUTO_CONTROL):
            _MappedByte(
                getter=lambda: 1 if c.current('AwbEnable', True) else 0,
                setter=lambda v: c.apply({'AwbEnable': bool(v)}),
                default=1,
            ),
            # CT Auto-Exposure Mode bitmap: 1=Manual, 2=Auto -> AeEnable.
            (CAMERA_TERMINAL_ID, CT_AE_MODE_CONTROL):
            _MappedByte(
                getter=lambda: 2 if c.current('AeEnable', True) else 1,
                setter=lambda v: c.apply({'AeEnable': bool(v & 0x0A)}),
                default=2,
                minimum=1,
                maximum=2,
                res=0x03,  # bitmap of supported modes (manual | auto)
            ),
            (CAMERA_TERMINAL_ID, CT_EXPOSURE_TIME_ABSOLUTE_CONTROL):
            _ExposureControl(c),
            (CAMERA_TERMINAL_ID, CT_FOCUS_ABSOLUTE_CONTROL):
            _ScaledControl(c, 'LensPosition'),
            (CAMERA_TERMINAL_ID, CT_FOCUS_AUTO_CONTROL):
            _MappedByte(
                getter=lambda: 1 if c.current('AfMode', 'Continuous') == 'Continuous' else 0,
                setter=lambda v: c.apply({'AfMode': 'Continuous' if v else 'Manual'}),
                default=1,
            ),
        }
        return table

    def handles(self, unit, selector):
        """Return True if (unit, selector) is a control this bridge maps."""
        return (unit, selector) in self._table

    def length(self, unit, selector):
        """Return the wire byte length of a control (0 if unknown)."""
        ctl = self._table.get((unit, selector))
        if ctl is None:
            return 0
        try:
            return ctl.snapshot()['size']
        except Exception:
            return 0

    def get(self, unit, selector, request):
        """Answer a GET_* request, returning the wire bytes (or None to stall)."""
        ctl = self._table.get((unit, selector))
        if ctl is None:
            return None
        try:
            snap = ctl.snapshot()
        except Exception:
            log.exception('UVC control snapshot failed (unit=%d sel=%#x)', unit, selector)
            return None
        if request == GET_INFO:
            return bytes([snap['info']])
        if request == GET_LEN:
            return struct.pack('<H', snap['size'])
        field = _GET_FIELD.get(request)
        if field is None:
            return None
        return self._pack(snap[field], snap['size'], snap['signed'])

    def set_cur(self, unit, selector, data):
        """Apply a SET_CUR payload (raw little-endian wire bytes)."""
        ctl = self._table.get((unit, selector))
        if ctl is None:
            return
        try:
            size = ctl.snapshot()['size']
            signed = ctl.snapshot()['signed']
        except Exception:
            return
        try:
            ctl.apply(self._unpack(data, size, signed))
        except Exception:
            log.exception('UVC control SET failed (unit=%d sel=%#x)', unit, selector)

    @staticmethod
    def _pack(value, size, signed):
        value = int(round(value))
        if signed:
            lo, hi = -(1 << (size * 8 - 1)), (1 << (size * 8 - 1)) - 1
        else:
            lo, hi = 0, (1 << (size * 8)) - 1
        value = max(lo, min(hi, value))
        return value.to_bytes(size, 'little', signed=signed)

    @staticmethod
    def _unpack(data, size, signed):
        raw = bytes(data)[:size]
        if len(raw) < size:
            raw = raw + b'\x00' * (size - len(raw))
        return int.from_bytes(raw, 'little', signed=signed)
