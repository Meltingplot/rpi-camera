"""Install the RPi Camera as a systemd service."""

import click


@click.command()
@click.option(
    '--connection',
    envvar='RPI_CAMERA_NM_CONNECTION',
    default='preconfigured',
    show_default=True,
    help='NetworkManager connection name to modify with the static IP config.',
)
@click.option(
    '--ip',
    'address',
    envvar='RPI_CAMERA_IP',
    default='10.42.0.3/24',
    show_default=True,
    help='Static IPv4 address with CIDR to assign on the connection.',
)
@click.option(
    '--gateway',
    envvar='RPI_CAMERA_GATEWAY',
    default='10.42.0.1',
    show_default=True,
    help='Default IPv4 gateway. Also used by the WiFi watchdog as the ping target.',
)
@click.option(
    '--dns',
    envvar='RPI_CAMERA_DNS',
    default='10.42.0.1',
    show_default=True,
    help='DNS server.',
)
@click.option(
    '--iface',
    envvar='RPI_CAMERA_IFACE',
    default='wlan0',
    show_default=True,
    help='WiFi interface name the watchdog monitors.',
)
@click.option(
    '--configure-network',
    envvar='RPI_CAMERA_CONFIGURE_NETWORK',
    is_flag=True,
    help='Opt in to nmcli static IP setup on the chosen connection. '
    'Off by default — IP config is expected to come from the Pi image itself.',
)
@click.option(
    '--wifi-watchdog/--no-wifi-watchdog',
    envvar='RPI_CAMERA_WIFI_WATCHDOG',
    default=True,
    show_default=True,
    help='Install the reboot-on-wifi-disconnect watchdog. Independent of --configure-network: '
    'the watchdog is camera-specific safety, not network setup.',
)
def install(connection, address, gateway, dns, iface, configure_network, wifi_watchdog):
    """Install the RPi Camera as a systemd service."""
    import os
    import getpass
    import grp
    import subprocess
    import sys
    import tempfile

    # Get the path of the service file
    rpi_camera_service_file = os.path.join(sys.prefix, 'rpi-camera.service')

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

    if not configure_network:
        click.echo(
            'Skipping nmcli static IP setup '
            '(pass --configure-network or set RPI_CAMERA_CONFIGURE_NETWORK=1 to enable).',
        )
    else:
        # Verify the NetworkManager connection we are about to modify actually exists,
        # otherwise nmcli fails later with a cryptic error.
        existing = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME', 'con', 'show'],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if connection not in existing:
            raise click.ClickException(
                f"NetworkManager connection {connection!r} not found. Pass --connection / "
                "RPI_CAMERA_NM_CONNECTION, or omit --configure-network to skip nmcli setup.",
            )

        click.echo(f'Configuring static IP {address} on connection {connection!r} via nmcli')

        # Set address, gateway, DNS and method in a single atomic nmcli call
        subprocess.run(
            [
                'sudo',
                'nmcli',
                'con',
                'mod',
                connection,
                'ipv4.addresses',
                address,
                'ipv4.gateway',
                gateway,
                'ipv4.dns',
                dns,
                'ipv4.method',
                'manual',
            ],
            check=True,
        )

        # Bring the connection down and up to apply changes
        subprocess.run(['sudo', 'nmcli', 'con', 'down', connection], check=True)
        subprocess.run(['sudo', 'nmcli', 'con', 'up', connection], check=True)

    if wifi_watchdog:
        click.echo(f'Installing WiFi watchdog for iface={iface}, gateway={gateway}')
        wifi_script_file = os.path.join(sys.prefix, 'reboot_on_wifi_disconnect.sh')
        subprocess.run(
            ['sudo', 'cp', '-f', wifi_script_file, '/usr/local/bin/reboot_on_wifi_disconnect.sh'],
            check=True,
        )
        subprocess.run(['sudo', 'chmod', '+x', '/usr/local/bin/reboot_on_wifi_disconnect.sh'], check=True)
        # `sudo VAR=val cmd` passes the env vars to the script (sudo's env strip
        # normally hides them); the script bakes them into the systemd unit.
        subprocess.run(
            [
                'sudo',
                f'WIFI_IFACE={iface}',
                f'GATEWAY={gateway}',
                '/usr/local/bin/reboot_on_wifi_disconnect.sh',
                'install',
            ],
            check=True,
        )
    else:
        click.echo('Skipping WiFi watchdog install (--no-wifi-watchdog).')

    # `/run/systemd/system` exists iff systemd is the active init AND running.
    # In image-build chroots it's absent, so daemon-reload/start would fail —
    # skip them and create the wants/ symlink manually (what `systemctl enable`
    # does under the hood for WantedBy=multi-user.target).
    systemd_running = os.path.isdir('/run/systemd/system')

    if systemd_running:
        subprocess.run(['sudo', 'systemctl', 'daemon-reload'], check=True)
        subprocess.run(['sudo', 'systemctl', 'enable', 'rpi-camera'], check=True)
        subprocess.run(['sudo', 'systemctl', 'start', 'rpi-camera'], check=True)
        click.echo('The RPi Camera has been installed as a systemd service.')
    else:
        wants_dir = '/etc/systemd/system/multi-user.target.wants'
        subprocess.run(['sudo', 'mkdir', '-p', wants_dir], check=True)
        subprocess.run(
            ['sudo', 'ln', '-sf', '/etc/systemd/system/rpi-camera.service', f'{wants_dir}/rpi-camera.service'],
            check=True,
        )
        click.echo(
            'systemd is not running (image build?) — skipped daemon-reload/start, '
            'created enable symlink manually. The service will come up on next boot.',
        )
