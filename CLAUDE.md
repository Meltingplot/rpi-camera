# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`meltingplot.rpi_camera` is an MJPEG streaming server for the Raspberry Pi camera module (Picamera2). It is intended to run on a Raspberry Pi as a systemd service. The `rpi-camera` CLI exposes two subcommands: `start` (run the server) and `install` (install as a systemd service and configure the host).

## Commands

Dev environment bootstrap (intended to run on a Raspberry Pi — uses `--system-site-packages` to pick up the system `picamera2`/`libcamera`):

```bash
./initialize_dev_environmenvt.sh   # apt deps + venv + picamera2 + test requirements
source venv/bin/activate
```

Common scripts (all wrap a single command and `set -e`):

- `./pytest-module.sh` — `pytest --cov-config .coveragerc --cov meltingplot tests/ -vv`
- `./flake8-module.sh` — `flake8 --statistics meltingplot`
- `./yapf-module.sh` — applies yapf formatting in-place to `meltingplot/` using `.style.yapf`
- `./create-package.sh` — runs yapf + flake8, then `python3 -m build` and `twine check`

Run a single test: `pytest tests/path/to/test_file.py::test_name -vv`

Run the server locally (requires a real Pi camera): `rpi-camera start`. Install as a service on a Pi: `sudo rpi-camera install`.

## Architecture

**Entry point.** `meltingplot/rpi_camera/__main__.py` registers a Click group with two commands: `start` (from [server.py](meltingplot/rpi_camera/server.py)) and `install` (from [cli/install.py](meltingplot/rpi_camera/cli/install.py)). Exposed via the `rpi-camera` console_script in [setup.py](setup.py).

**Streaming server** ([server.py](meltingplot/rpi_camera/server.py)). Single process binds **two** HTTP servers concurrently:

- Port **80** (`HttpHandler`): serves the landing HTML page and a single-frame JPEG snapshot at `/snapshot` and `/picture/1/current/`. Binding port 80 unprivileged requires `CAP_NET_BIND_SERVICE`, set in `rpi-camera.service`.
- Port **8081** (`StreamingHandler`): serves the continuous MJPEG `multipart/x-mixed-replace` stream at `/` or `/webcam`.

Both handlers share a single `StreamingOutput` (`frame_buffer`) attached as a class attribute. Picamera2 writes MJPEG frames into it via `FileOutput`; handlers `condition.wait(timeout=5)` on a new frame and then write it to the client (timeout disconnects the client / returns 503 on snapshot if the camera stalls). Rotation handling is hybrid: 0°/180° is done at the sensor via `Transform(hflip, vflip)` (free — see `StreamingOutput.hw_transform`), 90°/270° fall back to a client-side EXIF Orientation tag injected by `StreamingOutput.write` (`piexif`).

The two `StreamingServer`s (threaded `HTTPServer`s) are run via `loop.run_in_executor` (orchestrated by `_run()` under `asyncio.run`), alongside an async `watchdog` task. The whole process exits on `FIRST_COMPLETED`; the `finally` block then `shutdown()`/`server_close()`s both servers and cancels the watchdog, so any task failing tears the server down cleanly and lets systemd restart it.

**Watchdog → reboot.** `watchdog()` polls `frame_buffer.frame_counter` every 2s after an initial 30s grace period (the grace period is load-bearing — without it a slow-to-init camera triggers an immediate reboot loop). If no new frame arrived since the last tick it calls `os.system("sudo reboot")`. This is intentional: a hung camera should reboot the Pi, not just restart the service.

**Network-loss → reboot.** `reboot_on_wifi_disconnect.sh` is installed as a separate systemd service by `rpi-camera install`. On startup it blocks in `wait_for_initial_association` until `iw dev "$WIFI_IFACE" link` first reports "Connected" — bounded by `INITIAL_ASSOCIATION_TIMEOUT` (default 120s) so a chip that's stuck from boot still triggers a reboot. After that, two checks run once per second: (1) `iw dev "$WIFI_IFACE" link` — any loss of association reboots **immediately** (chip stuck, e.g. overheating); (2) ping `$GATEWAY` — only after `PING_FAILURES_BEFORE_REBOOT` consecutive failures (default 30s) is this a reboot trigger, as a slow safety net. The four runtime knobs (`WIFI_IFACE`, `GATEWAY`, `PING_FAILURES_BEFORE_REBOOT`, `INITIAL_ASSOCIATION_TIMEOUT`) are baked into the generated systemd unit as `Environment=` lines; the installer fills them from its own options. Defaults assume a fixed point-to-point network on `wlan0` (gateway `10.42.0.1`, camera `10.42.0.3`).

**Install side effects** ([cli/install.py](meltingplot/rpi_camera/cli/install.py)). `rpi-camera install` rewrites `rpi-camera.service` to use the current user/group/home, symlinks `<venv>/bin/rpi-camera` into `/usr/local/bin`, then `daemon-reload`/`enable`/`start`s the service. Network configuration is **opt-in** — the installer touches NetworkManager and installs the WiFi watchdog only when `--configure-network` / `RPI_CAMERA_CONFIGURE_NETWORK=1` is set; otherwise network setup is assumed to live in the Pi image. When enabled, the values are parametrized via `--connection`, `--ip`, `--gateway`, `--dns`, `--iface` (each with a matching `RPI_CAMERA_*` envvar). Many `sudo` calls — must be run on the target Pi.

## Conventions

- Python 3.10–3.12 (CI matrix). Line length 120 (flake8); yapf style in `.style.yapf`.
- Versioning via `versioneer` (git tags → `_version.py`).
- The `tests/` directory currently has no test files; CI runs pytest with `|| true`, so failing/missing tests do not break the build. Treat coverage as advisory until real tests exist.
