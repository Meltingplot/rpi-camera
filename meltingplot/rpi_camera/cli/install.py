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
    service_file = os.path.join(sys.prefix, 'rpi-camera.service')

    service_content = None

    # Read the content of the service file
    with open(service_file, 'r') as file:
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

    # Reload the systemd daemon
    subprocess.run(['sudo', 'systemctl', 'daemon-reload'])

    # Enable the service
    subprocess.run(['sudo', 'systemctl', 'enable', 'rpi-camera'])

    # Start the service
    subprocess.run(['sudo', 'systemctl', 'start', 'rpi-camera'])

    executable_file = os.path.join(sys.prefix, '/bin/rpi-camera')

    # Make the rpi-camera command available outside the venv
    subprocess.run(['sudo', 'ln', '-s', executable_file, '/usr/local/bin/rpi-camera'])

    print('The RPi Camera has been installed as a systemd service.')
