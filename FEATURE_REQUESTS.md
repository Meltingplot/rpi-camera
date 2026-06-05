# Feature requests / deferred work

## Sensor-agnostic capture resolutions via binned sensor modes

**Status:** deferred (not for 1.0.0).

### Problem
The capture/UVC resolution list is hard-coded per board model in two places —
`frames.conf` (pi-cam-gen) and `gadget_frames()` (rpi-camera) — with fixed sizes
per Pi tier (Zero / Zero 2 / Pi 4). It does not reflect the actual attached
sensor (OV5647 / IMX219 / IMX477 / IMX708), and `reconfig.py` does not pin a
sensor (raw) mode, so binning is left to picamera2's default heuristic.

### Observation motivating this
1080p and 720p show **no visible quality difference — only file size** (both are
fed from a binned sensor read; the extra output pixels are upscaled, not real
detail). So fixed per-board sizes are the wrong abstraction.

### Proposal
Make the resolution list **sensor-agnostic**, derived at runtime from
`picam2.sensor_modes`:

- Pick the **largest full-FoV binned mode** as the preferred capture mode
  (OV5647 1296×972, IMX219 1640×1232, IMX477 2028×1520, IMX708 2304×1296) and
  pin it via `create_video_configuration(sensor=…/raw=…)` so binning is
  deterministic instead of heuristic.
- Offer the full-res (un-binned) mode only as an explicit high-detail option.
- Generate (or drop) the per-board `frames.conf` from the sensor's modes rather
  than hardcoding sizes.

### Notes
- 1080p on the OV5647 is a centre-crop (no binning, narrower FoV) — another
  reason to drive the list from real sensor modes, not fixed sizes.
- Touches: `reconfig.py` (sensor-mode pin), `uvc_gadget.gadget_frames` /
  `controls._gadget_resolution_options`, and pi-cam-gen `frames.conf`.
