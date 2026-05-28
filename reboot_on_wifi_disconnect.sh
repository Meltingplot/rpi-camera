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

# Resolve the IP to ping. Prefer the live default route on $WIFI_IFACE so
# this works on both the original static point-to-point network (gateway
# 10.42.0.1, baked into $GATEWAY) and on any DHCP network the Pi happens to
# join (gateway assigned by DHCP — never matches $GATEWAY). Fall back to a
# generic default route, and finally to the legacy $GATEWAY env var so an
# old config never silently breaks.
current_gateway() {
    local gw
    gw=$(ip route show default dev "$WIFI_IFACE" 2>/dev/null | awk '/^default/ {print $3; exit}')
    if [ -z "$gw" ]; then
        gw=$(ip route show default 2>/dev/null | awk '/^default/ {print $3; exit}')
    fi
    if [ -z "$gw" ]; then
        gw="$GATEWAY"
    fi
    echo "$gw"
}

# Check whether the current default gateway is reachable. Used only as a
# slow safety net — transient packet loss should not trigger a reboot on
# its own. -W 1 bounds the ICMP wait at one second so a 1 Hz outer loop
# stays accurate when the gateway is dropping packets.
check_ip_reachable() {
    local gw
    gw=$(current_gateway)
    [ -n "$gw" ] && ping -c 1 -W 1 "$gw" &> /dev/null
}

# Function to install the systemd service.
# Embeds the current env values as Environment= lines so the running service
# uses them — env vars do not survive across `systemctl start` by themselves.
install_service() {
    # Drop the sudo prefix when already root (image-build chroots typically
    # run as root and don't have an interactive sudo password available).
    if [ "$(id -u)" -eq 0 ]; then
        SUDO=""
    else
        SUDO="sudo"
    fi

    cat <<EOF | $SUDO tee /etc/systemd/system/reboot_on_wifi_disconnect.service
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
        $SUDO systemctl daemon-reload
        $SUDO systemctl enable reboot_on_wifi_disconnect.service
        $SUDO systemctl start reboot_on_wifi_disconnect.service
    else
        echo "systemd not running (image build?) — creating wants/ symlink manually."
        $SUDO mkdir -p /etc/systemd/system/multi-user.target.wants
        $SUDO ln -sf /etc/systemd/system/reboot_on_wifi_disconnect.service \
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
                gw=$(current_gateway)
                echo "Gateway ${gw:-<none>} unreachable for ${PING_FAILURES_BEFORE_REBOOT}s. Rebooting (safety net)..."
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