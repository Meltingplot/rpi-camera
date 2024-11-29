#!/bin/bash

# Function to check if wlan0 is connected
check_wlan0_connected() {
    iw dev wlan0 link | grep -q "Connected"
}

# Function to check if 10.42.0.1 is reachable
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

# Main function to monitor WiFi connection
monitor_wifi() {
    failure_count=0
    while true; do
        # Check connection status every second for 5 seconds
        if ! check_wlan0_connected || ! check_ip_reachable; then
            failure_count=$((failure_count + 1))
            if [ "$failure_count" -ge 3 ]; then
                echo "wlan0 is not connected or 10.42.0.1 is not reachable for 3 consecutive checks. Rebooting..."
                reboot
            fi
        else
            failure_count=0
        fi
        sleep 1
    done
}

# Check if the script is called with "install" argument
if [ "$1" == "install" ]; then
    install_service
else
    monitor_wifi
fi