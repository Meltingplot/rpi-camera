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

    click.echo(f"Installing the service as user/group: {current_user}/{current_group}")

    # Save the modified content to a temp file
    with tempfile.NamedTemporaryFile('wt+') as tmp_file:
        tmp_file.write(service_content)
        tmp_file.flush()

        # Copy the service file to /etc/systemd/system
        subprocess.check_output(['sudo', 'cp', tmp_file.name, '/etc/systemd/system/rpi-camera.service'])

    executable_file = os.path.join(sys.exec_prefix, 'bin/rpi-camera')

    # Make the rpi-camera command available outside the venv
    subprocess.check_output(['sudo', 'ln', '-sf', executable_file, '/usr/local/bin/rpi-camera'])

    click.echo('Configuring static IP for wlan0 using nmcli to 10.42.0.3')

    # Set the static IP for wlan0 using nmcli
    subprocess.check_output(['sudo', 'nmcli', 'con', 'mod', 'preconfigured', 'ipv4.addresses', '10.42.0.3/24'])
    subprocess.check_output(['sudo', 'nmcli', 'con', 'mod', 'preconfigured', 'ipv4.gateway', '10.42.0.1'])
    subprocess.check_output(['sudo', 'nmcli', 'con', 'mod', 'preconfigured', 'ipv4.dns', '10.42.0.1'])
    subprocess.check_output(['sudo', 'nmcli', 'con', 'mod', 'preconfigured', 'ipv4.method', 'manual'])

    # Bring the connection down and up to apply changes
    subprocess.check_output(['sudo', 'nmcli', 'con', 'down', 'preconfigured'])
    subprocess.check_output(['sudo', 'nmcli', 'con', 'up', 'preconfigured'])

    click.echo('Install reboot on wifi disconnect service')
    wifi_script_file = os.path.join(sys.prefix, 'reboot_on_wifi_disconnect.sh')
    subprocess.check_output(['sudo', 'cp', '-f', wifi_script_file, '/usr/local/bin/reboot_on_wifi_disconnect.sh'])
    subprocess.check_output(['sudo', 'chmod', '+x', '/usr/local/bin/reboot_on_wifi_disconnect.sh'])
    subprocess.check_output(['sudo', '/usr/local/bin/reboot_on_wifi_disconnect.sh', 'install'])

    # Reload the systemd daemon
    subprocess.check_output(['sudo', 'systemctl', 'daemon-reload'])

    # Enable the service
    subprocess.check_output(['sudo', 'systemctl', 'enable', 'rpi-camera'])

    # Start the service
    subprocess.check_output(['sudo', 'systemctl', 'start', 'rpi-camera'])

    click.echo('The RPi Camera has been installed as a systemd service.')
