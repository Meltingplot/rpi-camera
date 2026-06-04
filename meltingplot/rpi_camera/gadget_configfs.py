# -*- coding: utf-8 -*-
"""Read the bound UVC gadget's descriptors back from configfs.

``rpi-cam-gadget-setup.sh`` writes the UVC function's frames, frame intervals,
``streaming_maxpacket`` and the camera/processing ``bmControls`` bitmaps into
configfs — that is the single source of truth for what the kernel actually
advertises to the host. :mod:`uvc_gadget` and :mod:`uvc_controls` read them
from here at runtime so the pump can never drift from the gadget descriptors
(a frame/interval/maxpacket mismatch makes the host negotiate against wrong
values; a bmControls mismatch stalls the control endpoint).

Everything here is best-effort and read-only: if configfs isn't present or a
field can't be parsed the caller falls back to its built-in defaults.
"""

import glob
import logging
import os

log = logging.getLogger(__name__)

# The image always names the gadget "picam"; glob anyway so a differently named
# gadget (or a future second one) still resolves. uvc.* is the function instance.
_UVC_FUNC_GLOB = '/sys/kernel/config/usb_gadget/*/functions/uvc.*'


def find_uvc_function():
    """Return the path of the configfs UVC function, or ``None`` if absent.

    Absent is normal when the gadget is in NCM mode (or not built yet).
    """
    matches = sorted(glob.glob(_UVC_FUNC_GLOB))
    return matches[0] if matches else None


def _read_text(path):
    with open(path) as fh:
        return fh.read()


def _read_int(path):
    return int(_read_text(path).strip(), 0)


def read_streaming(base):
    """Return ``(frames, intervals, frame_sizes, maxpacket)`` for ``base``.

    * ``frames``      – ``[(w, h), ...]`` ordered by ``bFrameIndex`` (the kernel
      orders frames by configfs dir name, which the setup script zero-pads).
    * ``intervals``   – ``[[ns, ...], ...]`` parallel to ``frames``, ascending.
    * ``frame_sizes`` – ``[dwMaxVideoFrameBufferSize, ...]`` parallel to
      ``frames``; the per-frame buffer ceiling the host allocates, which the
      PROBE/COMMIT ``dwMaxVideoFrameSize`` must echo.
    * ``maxpacket``   – ``streaming_maxpacket`` (int).

    Raises ``OSError``/``ValueError`` if the layout can't be read so the caller
    can fall back.
    """
    maxpacket = _read_int(os.path.join(base, 'streaming_maxpacket'))
    frames, intervals, frame_sizes = [], [], []
    # streaming/<format>/<instance>/<WxH>/ — only the frame dirs carry wWidth.
    for frame_dir in sorted(glob.glob(os.path.join(base, 'streaming', '*', '*', '*'))):
        wpath = os.path.join(frame_dir, 'wWidth')
        if not os.path.exists(wpath):
            continue
        width = _read_int(wpath)
        height = _read_int(os.path.join(frame_dir, 'wHeight'))
        ivs = sorted(int(tok) for tok in _read_text(os.path.join(frame_dir, 'dwFrameInterval')).split())
        frames.append((width, height))
        intervals.append(ivs)
        frame_sizes.append(_read_int(os.path.join(frame_dir, 'dwMaxVideoFrameBufferSize')))
    if not frames:
        raise ValueError('no UVC frame descriptors found under %s' % base)
    return frames, intervals, frame_sizes, maxpacket


def _parse_bmcontrols(path):
    """Read a configfs ``bmControls`` attribute as a ``bytes`` bitmap.

    The kernel may render it either as a space-separated hex string
    (``"0x1b 0x12 0x00"``) or as a raw byte blob, depending on version; handle
    both.
    """
    raw = open(path, 'rb').read()
    text = raw.decode('ascii', 'ignore').strip()
    toks = text.split()
    if toks and all(tok.lower().startswith('0x') or tok.isdigit() for tok in toks):
        try:
            return bytes(int(tok, 0) & 0xFF for tok in toks)
        except ValueError:
            pass
    return raw


def read_controls(base):
    """Return the control-entity bmControls + IDs, or ``{}`` if unreadable.

    Keys: ``camera`` / ``processing`` (``bytes`` bitmaps) and ``camera_id`` /
    ``processing_id`` (ints; default to the configfs defaults 1/2 if the kernel
    doesn't expose the ID attributes).
    """
    ct = os.path.join(base, 'control', 'terminal', 'camera', 'default')
    pu = os.path.join(base, 'control', 'processing', 'default')
    try:
        out = {
            'camera': _parse_bmcontrols(os.path.join(ct, 'bmControls')),
            'processing': _parse_bmcontrols(os.path.join(pu, 'bmControls')),
        }
    except OSError as exc:
        log.warning('UVC: could not read bmControls from configfs: %s', exc)
        return {}
    # bTerminalID / bUnitID are not exposed on every kernel; default to 1 / 2.
    for key, path, default in (
        ('camera_id', os.path.join(ct, 'bTerminalID'), 1),
        ('processing_id', os.path.join(pu, 'bUnitID'), 2),
    ):
        try:
            out[key] = _read_int(path)
        except (OSError, ValueError):
            out[key] = default
    return out


def bit_set(bitmap, bit):
    """Return True if ``bit`` (0-based, little-endian) is set in ``bitmap``."""
    idx = bit // 8
    return idx < len(bitmap) and bool((bitmap[idx] >> (bit % 8)) & 1)
