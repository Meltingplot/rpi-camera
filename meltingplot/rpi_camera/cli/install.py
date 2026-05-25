"""Install the RPi Camera as a systemd service."""

import click


@click.command()
def install():
    """Install the RPi Camera as a systemd service."""
    import os
    import getpass
    import grp
    import subprocess
    import sys
    import tempfile

    # Get the path of the service file
    rpi_camera_service_file = os.path.join(sys.prefix, 'rpi-camera.service')

    service_content = None

    # Read the content of the service file
    with open(rpi_camera_service_file, 'r') as file:
        service_content = file.read()

    # Replace User and Group with the current user and group
    current_user = getpass.getuser()
    current_group = grp.getgrgid(os.getgid()).gr_name
    service_content = service_content.replace('User=pi', f'User={current_user}', 1)
    service_content = service_content.replace('Group=pi', f'Group={current_group}', 1)
    service_content = service_content.replace(
        'WorkingDirectory=/home/pi',
        f'WorkingDirectory={os.path.expanduser("~")}',
        1,
    )

    click.echo(f"Installing the service as user/group: {current_user}/{current_group}")

    # Save the modified content to a temp file
    with tempfile.NamedTemporaryFile('wt+') as tmp_file:
        tmp_file.write(service_content)
        tmp_file.flush()

        # Copy the service file to /etc/systemd/system
        subprocess.run(['sudo', 'cp', tmp_file.name, '/etc/systemd/system/rpi-camera.service'], check=True)

    executable_file = os.path.join(sys.exec_prefix, 'bin/rpi-camera')

    # Make the rpi-camera command available outside the venv
    subprocess.run(['sudo', 'ln', '-sf', executable_file, '/usr/local/bin/rpi-camera'], check=True)

    # Verify the NetworkManager connection we are about to modify actually exists,
    # otherwise nmcli fails later with a cryptic error.
    existing = subprocess.run(
        ['nmcli', '-t', '-f', 'NAME', 'con', 'show'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if 'preconfigured' not in existing:
        raise click.ClickException(
            "NetworkManager connection 'preconfigured' not found — this installer "
            "expects a Raspberry Pi OS image with the preconfigured WiFi setup.",
        )

    click.echo('Configuring static IP for wlan0 using nmcli to 10.42.0.3')

    # Set address, gateway, DNS and method in a single atomic nmcli call
    subprocess.run(
        [
            'sudo', 'nmcli', 'con', 'mod', 'preconfigured',
            'ipv4.addresses', '10.42.0.3/24',
            'ipv4.gateway', '10.42.0.1',
            'ipv4.dns', '10.42.0.1',
            'ipv4.method', 'manual',
        ],
        check=True,
    )

    # Bring the connection down and up to apply changes
    subprocess.run(['sudo', 'nmcli', 'con', 'down', 'preconfigured'], check=True)
    subprocess.run(['sudo', 'nmcli', 'con', 'up', 'preconfigured'], check=True)

    click.echo('Install reboot on wifi disconnect service')
    wifi_script_file = os.path.join(sys.prefix, 'reboot_on_wifi_disconnect.sh')
    subprocess.run(['sudo', 'cp', '-f', wifi_script_file, '/usr/local/bin/reboot_on_wifi_disconnect.sh'], check=True)
    subprocess.run(['sudo', 'chmod', '+x', '/usr/local/bin/reboot_on_wifi_disconnect.sh'], check=True)
    subprocess.run(['sudo', '/usr/local/bin/reboot_on_wifi_disconnect.sh', 'install'], check=True)

    # Reload the systemd daemon
    subprocess.run(['sudo', 'systemctl', 'daemon-reload'], check=True)

    # Enable the service
    subprocess.run(['sudo', 'systemctl', 'enable', 'rpi-camera'], check=True)

    # Start the service
    subprocess.run(['sudo', 'systemctl', 'start', 'rpi-camera'], check=True)

    click.echo('The RPi Camera has been installed as a systemd service.')
