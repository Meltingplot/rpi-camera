#!/bin/bash

# Check whether the wlan0 radio is still associated with an AP.
# Loss of association = chip stuck (e.g. overheating) → reboot immediately.
check_wlan0_connected() {
    iw dev wlan0 link | grep -q "Connected"
}

# Check whether the gateway is reachable. Used only as a slow safety net —
# transient packet loss should not trigger a reboot on its own.
check_ip_reachable() {
    ping -c 1 10.42.0.1 &> /dev/null
}

# Function to install the systemd service
install_service() {
    # Create a systemd service file for this script
    cat <<EOF | sudo tee /etc/systemd/system/reboot_on_wifi_disconnect.service
[Unit]
Description=Reboot on Lost WiFi
After=network.target NetworkManager.service

[Service]
ExecStartPre=/bin/sleep 60
ExecStart=/usr/local/bin/reboot_on_wifi_disconnect.sh
Restart=always
User=root
Type=simple

[Install]
WantedBy=multi-user.target
EOF

    # Reload systemd manager configuration
    sudo systemctl daemon-reload

    # Enable the service to start on boot
    sudo systemctl enable reboot_on_wifi_disconnect.service

    # Start the service immediately
    sudo systemctl start reboot_on_wifi_disconnect.service
}

# Reboot immediately on lost association (primary signal).
# Reboot after PING_FAILURES_BEFORE_REBOOT consecutive ping failures
# (secondary safety net for the case where association is fine but routing died).
# INITIAL_ASSOCIATION_TIMEOUT bounds how long we wait for the first association
# after boot before assuming the chip is stuck from the start.
PING_FAILURES_BEFORE_REBOOT=30
INITIAL_ASSOCIATION_TIMEOUT=120

# Block until wlan0 associates for the first time, so the monitor loop's
# immediate-reboot rule doesn't fire during the initial WiFi scan.
wait_for_initial_association() {
    waited=0
    while ! check_wlan0_connected; do
        if [ "$waited" -ge "$INITIAL_ASSOCIATION_TIMEOUT" ]; then
            echo "wlan0 failed to associate within ${INITIAL_ASSOCIATION_TIMEOUT}s. Rebooting..."
            reboot
        fi
        sleep 2
        waited=$((waited + 2))
    done
    echo "wlan0 associated after ${waited}s, starting monitor"
}

monitor_wifi() {
    ping_failures=0
    while true; do
        if ! check_wlan0_connected; then
            echo "wlan0 lost association (chip likely stuck). Rebooting..."
            reboot
        fi

        if ! check_ip_reachable; then
            ping_failures=$((ping_failures + 1))
            if [ "$ping_failures" -ge "$PING_FAILURES_BEFORE_REBOOT" ]; then
                echo "Gateway 10.42.0.1 unreachable for ${PING_FAILURES_BEFORE_REBOOT}s. Rebooting (safety net)..."
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