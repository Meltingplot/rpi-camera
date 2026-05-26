#!/bin/bash

# Runtime config — overridable via systemd Environment= (set by the installer)
# or via shell env when running ad-hoc.
WIFI_IFACE="${WIFI_IFACE:-wlan0}"
GATEWAY="${GATEWAY:-10.42.0.1}"
PING_FAILURES_BEFORE_REBOOT="${PING_FAILURES_BEFORE_REBOOT:-30}"
INITIAL_ASSOCIATION_TIMEOUT="${INITIAL_ASSOCIATION_TIMEOUT:-120}"

# Check whether the radio is still associated with an AP.
# Loss of association = chip stuck (e.g. overheating) → reboot immediately.
check_wlan0_connected() {
    iw dev "$WIFI_IFACE" link | grep -q "Connected"
}

# Check whether the gateway is reachable. Used only as a slow safety net —
# transient packet loss should not trigger a reboot on its own.
check_ip_reachable() {
    ping -c 1 "$GATEWAY" &> /dev/null
}

# Function to install the systemd service.
# Embeds the current env values as Environment= lines so the running service
# uses them — env vars do not survive across `systemctl start` by themselves.
install_service() {
    cat <<EOF | sudo tee /etc/systemd/system/reboot_on_wifi_disconnect.service
[Unit]
Description=Reboot on Lost WiFi
After=network.target NetworkManager.service

[Service]
ExecStart=/usr/local/bin/reboot_on_wifi_disconnect.sh
Restart=always
User=root
Type=simple
Environment=WIFI_IFACE=${WIFI_IFACE}
Environment=GATEWAY=${GATEWAY}
Environment=PING_FAILURES_BEFORE_REBOOT=${PING_FAILURES_BEFORE_REBOOT}
Environment=INITIAL_ASSOCIATION_TIMEOUT=${INITIAL_ASSOCIATION_TIMEOUT}

[Install]
WantedBy=multi-user.target
EOF

    # /run/systemd/system exists iff systemd is the active init AND running.
    # In image-build chroots it's absent — skip daemon-reload/start and create
    # the wants/ symlink by hand instead of calling `systemctl enable`.
    if [ -d /run/systemd/system ]; then
        sudo systemctl daemon-reload
        sudo systemctl enable reboot_on_wifi_disconnect.service
        sudo systemctl start reboot_on_wifi_disconnect.service
    else
        echo "systemd not running (image build?) — creating wants/ symlink manually."
        sudo mkdir -p /etc/systemd/system/multi-user.target.wants
        sudo ln -sf /etc/systemd/system/reboot_on_wifi_disconnect.service \
            /etc/systemd/system/multi-user.target.wants/reboot_on_wifi_disconnect.service
    fi
}

# Block until wlan0 associates for the first time, so the monitor loop's
# immediate-reboot rule doesn't fire during the initial WiFi scan.
wait_for_initial_association() {
    waited=0
    while ! check_wlan0_connected; do
        if [ "$waited" -ge "$INITIAL_ASSOCIATION_TIMEOUT" ]; then
            echo "${WIFI_IFACE} failed to associate within ${INITIAL_ASSOCIATION_TIMEOUT}s. Rebooting..."
            reboot
        fi
        sleep 2
        waited=$((waited + 2))
    done
    echo "${WIFI_IFACE} associated after ${waited}s, starting monitor"
}

monitor_wifi() {
    ping_failures=0
    while true; do
        if ! check_wlan0_connected; then
            echo "${WIFI_IFACE} lost association (chip likely stuck). Rebooting..."
            reboot
        fi

        if ! check_ip_reachable; then
            ping_failures=$((ping_failures + 1))
            if [ "$ping_failures" -ge "$PING_FAILURES_BEFORE_REBOOT" ]; then
                echo "Gateway ${GATEWAY} unreachable for ${PING_FAILURES_BEFORE_REBOOT}s. Rebooting (safety net)..."
                reboot
            fi
        else
            ping_failures=0
        fi
        sleep 1
    done
}

# Check if the script is called with "install" argument
if [ "$1" == "install" ]; then
    install_service
else
    wait_for_initial_association
    monitor_wifi
fi