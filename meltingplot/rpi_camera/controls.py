# -*- coding: utf-8 -*-
"""Curated picamera2 control metadata, live application, and JSON persistence.

This module is the bridge between the HTTP control endpoints in
:mod:`meltingplot.rpi_camera.server` and the live ``Picamera2`` instance. It
exposes:

* ``CURATED_CONTROLS`` — a hand-picked subset of libcamera controls we want
  to surface in the web UI, with the widget metadata each one needs.
* ``CameraController`` — holds the picamera2 reference, serialises
  ``set_controls`` calls, persists the applied state to JSON, and runs the
  one-shot autofocus sequence.

Slider bounds are not hard-coded here. They are read at runtime from
``picam2.camera_controls`` so they match whatever sensor is actually
connected (the v2/v3/HQ modules expose different ranges, and some controls
are absent entirely on sensors without AF).
"""

import json
import logging
import os
import subprocess
import tempfile
import threading
import time

from libcamera import controls as libcontrols

from . import gadget_configfs
from .uvc_gadget import gadget_frames

log = logging.getLogger(__name__)

# Hand-picked control set surfaced in the web UI.
#
# Slider min/max are filled in at runtime from ``picam2.camera_controls`` so
# they track the actual sensor — only ``step`` and widget metadata live here.
# A few non-sensor controls (``FrameRate``) carry explicit bounds because
# picamera2 maps them to a virtual range rather than a libcamera control.
CURATED_CONTROLS = {
    'AeEnable': {
        'ui_type': 'toggle',
        'label': 'Auto exposure',
        'group': 'exposure',
    },
    'ExposureTime': {
        'ui_type': 'slider',
        'label': 'Exposure time (µs)',
        'group': 'exposure',
        'step': 100,
        'disabled_when': {
            'AeEnable': True,
        },
    },
    'AnalogueGain': {
        'ui_type': 'slider',
        'label': 'Analogue gain',
        'group': 'exposure',
        'step': 0.1,
        'disabled_when': {
            'AeEnable': True,
        },
    },
    'AeExposureMode': {
        'ui_type': 'select',
        'label': 'AE exposure mode',
        'group': 'exposure',
        'options': ['Normal', 'Short', 'Long', 'Custom'],
    },
    'AeMeteringMode': {
        'ui_type': 'select',
        'label': 'AE metering',
        'group': 'exposure',
        'options': ['CentreWeighted', 'Spot', 'Matrix', 'Custom'],
    },
    'AwbEnable': {
        'ui_type': 'toggle',
        'label': 'Auto white balance',
        'group': 'white_balance',
    },
    'AwbMode': {
        'ui_type': 'select',
        'label': 'AWB mode',
        'group': 'white_balance',
        'options': [
            'Auto',
            'Incandescent',
            'Tungsten',
            'Fluorescent',
            'Indoor',
            'Daylight',
            'Cloudy',
            'Custom',
        ],
        'disabled_when': {
            'AwbEnable': False,
        },
    },
    'ColourGains': {
        'ui_type': 'pair',
        'label': 'Colour gains (R, B)',
        'group': 'white_balance',
        'step': 0.1,
        'disabled_when': {
            'AwbEnable': True,
        },
    },
    'Brightness': {
        'ui_type': 'slider',
        'label': 'Brightness',
        'group': 'image',
        'step': 0.05,
    },
    'Contrast': {
        'ui_type': 'slider',
        'label': 'Contrast',
        'group': 'image',
        'step': 0.1,
    },
    'Saturation': {
        'ui_type': 'slider',
        'label': 'Saturation',
        'group': 'image',
        'step': 0.1,
    },
    'Sharpness': {
        'ui_type': 'slider',
        'label': 'Sharpness',
        'group': 'image',
        'step': 0.1,
        # Curated app-level default that overrides the libcamera sensor
        # default — see capabilities() / reset() for the precedence.
        'default': 4.5,
    },
    'NoiseReductionMode': {
        'ui_type': 'select',
        'label': 'Noise reduction',
        'group': 'image',
        'options': ['Off', 'Fast', 'HighQuality', 'Minimal', 'ZSL'],
    },
    'AfMode': {
        'ui_type': 'select',
        'label': 'Autofocus mode',
        'group': 'focus',
        'options': ['Manual', 'Auto', 'Continuous'],
    },
    'LensPosition': {
        'ui_type': 'slider',
        'label': 'Lens position (diopters)',
        'group': 'focus',
        'step': 0.1,
        'disabled_when_not': {
            'AfMode': 'Manual',
        },
    },
    'FrameRate': {
        'ui_type': 'slider',
        'label': 'Frame rate (fps)',
        'group': 'capture',
        'min': 1,
        'max': 30,
        'step': 1,
        'default': 4,
    },
    'Resolution': {
        'ui_type': 'select',
        'label': 'Resolution',
        'group': 'capture',
        'options': ['640x480', '1280x720', '1920x1080'],
    },
    'Rotation': {
        'ui_type': 'select',
        'label': 'Rotation',
        'group': 'capture',
        'options': ['0', '90', '180', '270'],
    },
    # Not a libcamera control — a toggle for the image's WiFi safety watchdog
    # systemd unit (reboot on lost wlan0). See SYSTEM_CONTROLS below; only
    # surfaced when that unit is actually present on the device.
    'RebootOnWifiLoss': {
        'ui_type': 'toggle',
        'label': 'Reboot on WiFi loss',
        'group': 'system',
    },
}

# Controls that change the capture pipeline (and therefore the USB UVC
# gadget descriptors) rather than a live libcamera setting. They are not
# passed to ``picam2.set_controls``; instead they are persisted and the
# registered change listeners reconfigure the camera + gadget. Rotation is
# here too: 0/180 are sensor hflip+vflip (a configure()-time Transform) and
# 90/270 set a JPEG EXIF tag, so neither is a live libcamera control.
RECONFIG_CONTROLS = frozenset({'Resolution', 'FrameRate', 'Rotation'})

# Controls that drive a SYSTEM service rather than libcamera or the capture
# pipeline. They are never passed to set_controls and never persisted to
# controls.json — the systemd unit's own enabled-state is the single source of
# truth (read back live). Applying one runs systemctl via sudo.
SYSTEM_CONTROLS = frozenset({'RebootOnWifiLoss'})

# The WiFi safety watchdog unit is provided by the Pi image (pi-cam-gen
# stage2/02-net-tweaks), shipped disabled; we only toggle it here.
_WIFI_WATCHDOG_UNIT = 'reboot_on_wifi_disconnect.service'


def _systemctl(verb, *, sudo):
    """Run ``systemctl <verb> [--now] <unit>``; return the CompletedProcess.

    ``enable``/``disable`` need root (sudo, granted by a pinned sudoers rule in
    the image); ``is-enabled`` is read-only and runs unprivileged.
    """
    cmd = []
    if sudo and os.geteuid() != 0:
        cmd.append('sudo')
    cmd.append('systemctl')
    cmd.append(verb)
    if verb in ('enable', 'disable'):
        cmd.append('--now')
    cmd.append(_WIFI_WATCHDOG_UNIT)
    return subprocess.run(cmd, capture_output=True, text=True)


def _wifi_watchdog_present():
    """Return True if the image shipped the WiFi watchdog unit (so we can toggle it)."""
    return os.path.exists('/etc/systemd/system/' + _WIFI_WATCHDOG_UNIT)


def _wifi_watchdog_enabled():
    """Return True if the watchdog unit is currently enabled (live systemd state)."""
    try:
        return _systemctl('is-enabled', sudo=False).stdout.strip() == 'enabled'
    except Exception:
        return False


def _set_wifi_watchdog(on):
    """Enable+start (or disable+stop) the watchdog unit; raise on failure."""
    verb = 'enable' if on else 'disable'
    result = _systemctl(verb, sudo=True)
    if result.returncode != 0:
        raise RuntimeError(
            'systemctl %s --now %s failed: %s' % (verb, _WIFI_WATCHDOG_UNIT, result.stderr.strip()),
        )


# Map each enum-valued control to the libcamera enum class that owns its
# values. Used to translate persisted/JSON string names ("Continuous") to
# the C++ enum value picamera2's set_controls expects.
ENUM_TYPES = {
    'AeExposureMode': libcontrols.AeExposureModeEnum,
    'AeMeteringMode': libcontrols.AeMeteringModeEnum,
    'AwbMode': libcontrols.AwbModeEnum,
    'AfMode': libcontrols.AfModeEnum,
    'NoiseReductionMode': libcontrols.draft.NoiseReductionModeEnum,
}


def _gadget_resolution_options():
    """Resolution dropdown options taken from the UVC gadget's advertised frames.

    Prefer the live configfs descriptors (what the bound gadget actually
    advertises); fall back to the per-board default list, then a static minimum.
    Returns ``['WxH', ...]`` in the gadget's bFrameIndex (ascending) order so the
    web UI mirrors exactly what the USB webcam offers.
    """
    base = gadget_configfs.find_uvc_function()
    if base is not None:
        try:
            frames = gadget_configfs.read_streaming(base)[0]
            if frames:
                return ['%dx%d' % (w, h) for w, h in frames]
        except Exception:
            pass
    try:
        return ['%dx%d' % (w, h) for w, h in gadget_frames()]
    except Exception:
        return ['640x480', '1280x720', '1920x1080']


def _to_libcamera(name, value):
    """Translate a JSON-friendly value into what ``set_controls`` expects.

    Enum string names go via :data:`ENUM_TYPES`. Tuple-typed controls
    (``ColourGains``) arrive from JSON as lists and need to be cast back.
    Everything else passes through unchanged.
    """
    enum_cls = ENUM_TYPES.get(name)
    if enum_cls is not None and isinstance(value, str):
        try:
            return getattr(enum_cls, value)
        except AttributeError as exc:
            raise ValueError(f'unknown {name} value {value!r}') from exc
    if name == 'ColourGains' and isinstance(value, (list, tuple)):
        return tuple(float(v) for v in value)
    return value


class CameraController:
    """Apply picamera2 controls under a lock and persist them to JSON.

    A single instance is shared between request-handler threads via a class
    attribute on :class:`HttpHandler` (same pattern already used for
    ``frame_buffer`` and ``page_bytes``). All mutation goes through
    :meth:`apply` or :meth:`trigger_autofocus`, both of which serialise
    writes via ``self._lock``.
    """

    # Block at most this long polling AfState before declaring a timeout.
    AF_POLL_INTERVAL = 0.1

    def __init__(self, picam2, persist_path):
        """Bind to a ``Picamera2`` instance and resolve the persistence path.

        ``persist_path`` is created on-demand; the parent directory is
        created with :func:`os.makedirs` so a fresh install needs no manual
        setup.
        """
        self._picam2 = picam2
        self._persist_path = persist_path
        self._lock = threading.RLock()
        self._state = {}
        self._listeners = []

        os.makedirs(os.path.dirname(self._persist_path), exist_ok=True)

        available = set(picam2.camera_controls.keys())
        # FrameRate/Resolution are virtual (capture-pipeline) controls that
        # camera_controls doesn't list, so accept them unconditionally.
        self._supported = (set(CURATED_CONTROLS.keys()) & available) | RECONFIG_CONTROLS
        # System toggles only when their backing service is actually present
        # (the image ships the WiFi watchdog unit; dev hosts won't have it).
        if _wifi_watchdog_present():
            self._supported |= SYSTEM_CONTROLS

    def register_change_listener(self, fn):
        """Register ``fn(merged_state, changed_reconfig)`` for reconfig changes.

        Called (outside the lock) whenever a control in
        :data:`RECONFIG_CONTROLS` (Resolution/FrameRate) is applied, so the
        server can reconfigure the camera and the USB UVC gadget. Listeners
        must return quickly; do the heavy work asynchronously.
        """
        self._listeners.append(fn)

    def capabilities(self):
        """Return curated metadata enriched with per-sensor bounds.

        Bounds come from ``picam2.camera_controls`` so the UI sees ranges
        that actually match the connected sensor. Controls the sensor does
        not advertise are omitted entirely.
        """
        info = self._picam2.camera_controls
        out = {}
        for name in self._supported:
            meta = dict(CURATED_CONTROLS[name])
            bounds = info.get(name)
            if bounds is not None and meta['ui_type'] in ('slider', 'pair'):
                meta['min'] = bounds[0]
                meta['max'] = bounds[1]
                if 'default' not in meta and bounds[2] is not None:
                    meta['default'] = bounds[2]
            # The Resolution dropdown mirrors the UVC gadget's advertised frames
            # so the web UI is constrained to exactly what the USB webcam offers
            # (per-board, e.g. up to 1080p on a Zero W). No hard-coded list.
            if name == 'Resolution':
                meta['options'] = _gadget_resolution_options()
            out[name] = meta
        return out

    def get_state(self):
        """Return a JSON-safe snapshot of currently applied UI values."""
        with self._lock:
            state = dict(self._state)
        # System toggles aren't persisted in _state — read their live state
        # from systemd so the UI always reflects reality.
        if 'RebootOnWifiLoss' in self._supported:
            state['RebootOnWifiLoss'] = _wifi_watchdog_enabled()
        return state

    def bounds(self, name):
        """Return the sensor's ``(min, max, default)`` for a control, or None.

        Used by the UVC control bridge to map the host's integer wire values
        onto the actual sensor range. ``Resolution``/``FrameRate`` are virtual
        and not present in ``camera_controls``.
        """
        return self._picam2.camera_controls.get(name)

    def current(self, name, fallback=None):
        """Return the currently applied value for a control, or ``fallback``."""
        with self._lock:
            return self._state.get(name, fallback)

    def seed_reconfig_state(self, resolution, framerate, rotation=None):
        """Record the active Resolution/FrameRate/Rotation for the UI without applying.

        Called once at startup so the UI shows the current capture settings.
        Uses ``setdefault`` so a value loaded from the persisted file wins.
        """
        with self._lock:
            self._state.setdefault('Resolution', resolution)
            self._state.setdefault('FrameRate', framerate)
            if rotation is not None:
                self._state.setdefault('Rotation', str(int(rotation)))

    def reapply_live(self):
        """Re-apply persisted live libcamera controls after a reconfigure.

        A ``picam2.configure`` resets every control, so the reconfigure
        coordinator calls this to restore exposure/white-balance/etc. from
        the persisted state. Resolution/FrameRate are skipped — they are the
        reconfigure inputs, not live controls.
        """
        with self._lock:
            live = {
                k: _to_libcamera(k, v)
                for k, v in self._state.items() if k not in RECONFIG_CONTROLS and k in self._supported
            }
            if not live:
                return
            try:
                self._picam2.set_controls(live)
            except Exception:
                log.exception('Failed to re-apply live controls after reconfigure')

    def _filter_supported(self, partial):
        """Drop unsupported keys (logged); return only controls we can apply."""
        out = {}
        for name, value in partial.items():
            if name in self._supported:
                out[name] = value
            else:
                log.info('Ignoring unsupported control %s', name)
        return out

    @staticmethod
    def _bucket(filtered):
        """Split into (system, reconfig, live) controls.

        SYSTEM controls drive a systemd unit (not libcamera, not persisted);
        RECONFIG controls (Resolution/FrameRate/Rotation) are persisted and
        handed to the reconfig listeners; the rest are live libcamera controls.
        """
        system, reconfig, live = {}, {}, {}
        for k, v in filtered.items():
            if k in SYSTEM_CONTROLS:
                system[k] = v
            elif k in RECONFIG_CONTROLS:
                reconfig[k] = v
            else:
                live[k] = v
        return system, reconfig, live

    @staticmethod
    def _apply_system(system):
        """Run the systemd action for each SYSTEM control (outside the lock)."""
        for name, value in system.items():
            if name == 'RebootOnWifiLoss':
                _set_wifi_watchdog(bool(value))

    def apply(self, partial):
        """Apply a partial control dict to the live camera and persist it.

        Unknown / unsupported keys are dropped with a log line — a config
        file written for a different sensor must not break startup. Returns
        the merged ``state`` dict after a successful apply.
        """
        if not isinstance(partial, dict):
            raise ValueError('controls payload must be a JSON object')

        filtered = self._filter_supported(partial)
        if not filtered:
            return self.get_state()

        system, reconfig, live = self._bucket(filtered)
        self._apply_system(system)

        with self._lock:
            if live:
                translated = {name: _to_libcamera(name, value) for name, value in live.items()}
                self._picam2.set_controls(translated)
            # System toggles are not persisted — systemd is their source of truth.
            self._state.update({k: v for k, v in filtered.items() if k not in SYSTEM_CONTROLS})
            self._save_locked()
            merged = dict(self._state)
        if 'RebootOnWifiLoss' in self._supported:
            merged['RebootOnWifiLoss'] = _wifi_watchdog_enabled()

        # Fire reconfig listeners outside the lock: reconfiguring the camera
        # and re-binding the USB gadget can take seconds (the host sees a USB
        # reconnect), and must not block other control writes.
        if reconfig:
            for fn in self._listeners:
                try:
                    fn(merged, reconfig)
                except Exception:
                    log.exception('Reconfig listener failed for %s', reconfig)

        return merged

    def reset(self):
        """Reset every supported control to its default and wipe state.

        For each supported control we prefer the curated app-level default
        (set in :data:`CURATED_CONTROLS` — e.g. ``Sharpness: 4.5``) and fall
        back to the libcamera sensor default from ``camera_controls``.
        ``self._state`` and the persisted JSON file are then cleared so a
        subsequent restart also starts blank. Controls that have neither a
        curated nor a sensor default (e.g. the virtual ``FrameRate``) are
        skipped — their current value stands.
        """
        info = self._picam2.camera_controls
        defaults = {}
        for name in self._supported:
            if name in SYSTEM_CONTROLS:
                continue  # not a libcamera control; leave the systemd unit as-is
            curated = CURATED_CONTROLS.get(name, {}).get('default')
            if curated is not None:
                defaults[name] = _to_libcamera(name, curated)
                continue
            bounds = info.get(name)
            if bounds is not None and bounds[2] is not None:
                defaults[name] = bounds[2]

        with self._lock:
            if defaults:
                self._picam2.set_controls(defaults)
            self._state = {}
            self._save_locked()
            return dict(self._state)

    def trigger_autofocus(self, timeout=5.0):
        """Run a one-shot AF cycle and return the resulting state.

        Sequence: set ``AfMode=Auto`` (if not already), trigger
        ``AfTrigger=Start``, then poll ``capture_metadata()['AfState']``
        until it leaves ``Scanning``. The lock is released between polls so
        concurrent control writes from other handler threads don't queue up
        behind a slow focus hunt.
        """
        if 'AfMode' not in self._supported:
            raise RuntimeError('sensor does not support autofocus')

        with self._lock:
            self._picam2.set_controls(
                {
                    'AfMode': libcontrols.AfModeEnum.Auto,
                    'AfTrigger': libcontrols.AfTriggerEnum.Start,
                },
            )
            self._state['AfMode'] = 'Auto'
            self._save_locked()

        deadline = time.monotonic() + timeout
        last_state = 'Idle'
        while time.monotonic() < deadline:
            meta = self._picam2.capture_metadata()
            af_state = meta.get('AfState')
            if af_state is not None:
                last_state = _af_state_name(af_state)
            if last_state in ('Focused', 'Failed'):
                return {
                    'state': last_state,
                    'lens_position': meta.get('LensPosition'),
                }
            time.sleep(self.AF_POLL_INTERVAL)

        return {'state': 'timeout', 'last': last_state}

    def load_and_apply_persisted(self):
        """Apply curated defaults plus any persisted user changes to the camera.

        Precedence (lowest to highest): libcamera sensor defaults (already in
        place after Picamera2 init) < curated app-level defaults from
        :data:`CURATED_CONTROLS` < persisted user changes from the JSON file.
        Called once from ``start()`` after the camera is initialised. Unknown
        keys in the JSON are skipped so a file written by an HQ-module install
        survives a swap to a v2 module without crashing the service.
        """
        defaults = {}
        for name in self._supported:
            curated = CURATED_CONTROLS.get(name, {}).get('default')
            if curated is not None:
                defaults[name] = _to_libcamera(name, curated)
        if defaults:
            with self._lock:
                self._picam2.set_controls(defaults)
            log.info('Applied %d curated defaults', len(defaults))

        try:
            with open(self._persist_path, 'r') as fh:
                persisted = json.load(fh)
        except FileNotFoundError:
            log.info('No persisted controls at %s', self._persist_path)
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning('Could not read persisted controls (%s); ignoring', exc)
            return

        if not isinstance(persisted, dict):
            log.warning('Persisted controls file is not a JSON object; ignoring')
            return

        log.info('Loaded %d persisted controls from %s', len(persisted), self._persist_path)
        self.apply(persisted)

    def _save_locked(self):
        """Write ``self._state`` to disk atomically. Caller holds the lock."""
        directory = os.path.dirname(self._persist_path) or '.'
        fd, tmp_path = tempfile.mkstemp(
            prefix='.controls-',
            suffix='.json.tmp',
            dir=directory,
        )
        try:
            with os.fdopen(fd, 'w') as fh:
                json.dump(self._state, fh, indent=2, sort_keys=True)
            os.replace(tmp_path, self._persist_path)
        except Exception:
            # Clean up the temp file on any failure so we don't litter the
            # config directory with .controls-*.json.tmp partials.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def _af_state_name(value):
    """Translate the libcamera ``AfState`` value to a human-readable string."""
    try:
        return libcontrols.AfStateEnum(value).name
    except (ValueError, AttributeError):
        return str(value)
