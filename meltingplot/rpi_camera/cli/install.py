"""Install the RPi Camera as a systemd service."""

import os
import shutil
import subprocess

import click


# File-system mutations try the direct Python op first and only escalate
# to sudo on PermissionError. This handles three environments uniformly:
#   - root: direct op works, never falls through to sudo.
#   - real Pi as user `pi` with passwordless sudo: direct fails on
#     root-owned dirs, sudo fallback succeeds.
#   - image-build chroot as `pi` with write access to the rootfs (typical
#     pi-gen stages): direct op works, sudo isn't called at all so the
#     missing interactive sudo password doesn't matter.
def _copy(src, dst):
    try:
        shutil.copy(src, dst)
    except PermissionError:
        subprocess.run(['sudo', 'cp', src, dst], check=True)


def _symlink(target, link):
    try:
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(target, link)
    except PermissionError:
        subprocess.run(['sudo', 'ln', '-sf', target, link], check=True)


def _chmod_exec(path):
    try:
        os.chmod(path, os.stat(path).st_mode | 0o111)
    except PermissionError:
        subprocess.run(['sudo', 'chmod', '+x', path], check=True)


def _makedirs(path):
    try:
        os.makedirs(path, exist_ok=True)
    except PermissionError:
        subprocess.run(['sudo', 'mkdir', '-p', path], check=True)


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
    help='Default IPv4 gateway (used by --configure-network nmcli setup).',
)
@click.option(
    '--dns',
    envvar='RPI_CAMERA_DNS',
    default='10.42.0.1',
    show_default=True,
    help='DNS server.',
)
@click.option(
    '--service-user',
    envvar='RPI_CAMERA_SERVICE_USER',
    default=None,
    help='User to run the rpi-camera systemd service as. Defaults to the user invoking the '
    'installer — override for image-build chroots where the installer runs as root but the '
    'service should run as `pi`.',
)
@click.option(
    '--service-group',
    envvar='RPI_CAMERA_SERVICE_GROUP',
    default=None,
    help="Group to run the rpi-camera systemd service as. Defaults to the invoking user's "
    "primary group.",
)
@click.option(
    '--working-directory',
    envvar='RPI_CAMERA_WORKING_DIRECTORY',
    default=None,
    help="WorkingDirectory= for the systemd service. Defaults to the invoking user's home "
    "directory.",
)
@click.option(
    '--configure-network',
    envvar='RPI_CAMERA_CONFIGURE_NETWORK',
    is_flag=True,
    help='Opt in to nmcli static IP setup on the chosen connection. '
    'Off by default — IP config is expected to come from the Pi image itself.',
)
def install(
    connection,
    address,
    gateway,
    dns,
    service_user,
    service_group,
    working_directory,
    configure_network,
):
    """Install the RPi Camera as a systemd service."""
    import getpass
    import grp
    import sys
    import tempfile

    # systemctl / nmcli have no safe direct equivalent and always need root.
    # Skip the sudo prefix when already root so it isn't called at all.
    sudo = [] if os.geteuid() == 0 else ['sudo']

    # Get the path of the service file
    rpi_camera_service_file = os.path.join(sys.prefix, 'rpi-camera.service')

    # Read the content of the service file
    with open(rpi_camera_service_file, 'r') as file:
        service_content = file.read()

    # Resolve user/group/home: explicit CLI/env wins, otherwise auto-detect from the
    # invoking user. Auto-detect is wrong in image-build chroots (installer runs as
    # root, target service should run as `pi`) — that case must set
    # RPI_CAMERA_SERVICE_USER / _GROUP / _WORKING_DIRECTORY.
    current_user = service_user or getpass.getuser()
    current_group = service_group or grp.getgrgid(os.getgid()).gr_name
    working_dir = working_directory or os.path.expanduser("~")
    service_content = service_content.replace('User=pi', f'User={current_user}', 1)
    service_content = service_content.replace('Group=pi', f'Group={current_group}', 1)
    service_content = service_content.replace(
        'WorkingDirectory=/home/pi',
        f'WorkingDirectory={working_dir}',
        1,
    )

    click.echo(f"Installing the service as user/group: {current_user}/{current_group}")

    # Save the modified content to a temp file
    with tempfile.NamedTemporaryFile('wt+') as tmp_file:
        tmp_file.write(service_content)
        tmp_file.flush()

        # Copy the service file to /etc/systemd/system
        subprocess.run([*sudo, 'cp', tmp_file.name, '/etc/systemd/system/rpi-camera.service'], check=True)

    executable_file = os.path.join(sys.exec_prefix, 'bin/rpi-camera')

    # Make the rpi-camera command available outside the venv
    subprocess.run([*sudo, 'ln', '-sf', executable_file, '/usr/local/bin/rpi-camera'], check=True)

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
                *sudo,
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
        subprocess.run([*sudo, 'nmcli', 'con', 'down', connection], check=True)
        subprocess.run([*sudo, 'nmcli', 'con', 'up', connection], check=True)

    # NOTE: the WiFi safety watchdog (reboot on lost wlan0) is no longer
    # installed here — it is a system service owned by the Pi image
    # (pi-cam-gen stage2/02-net-tweaks), shipped disabled and toggled from the
    # web UI. A reboot service does not belong in the Python camera package.

    # `/run/systemd/system` exists iff systemd is the active init AND running.
    # In image-build chroots it's absent, so daemon-reload/start would fail —
    # skip them and create the wants/ symlink manually (what `systemctl enable`
    # does under the hood for WantedBy=multi-user.target).
    systemd_running = os.path.isdir('/run/systemd/system')

    if systemd_running:
        subprocess.run([*sudo, 'systemctl', 'daemon-reload'], check=True)
        subprocess.run([*sudo, 'systemctl', 'enable', 'rpi-camera'], check=True)
        subprocess.run([*sudo, 'systemctl', 'start', 'rpi-camera'], check=True)
        click.echo('The RPi Camera has been installed as a systemd service.')
    else:
        wants_dir = '/etc/systemd/system/multi-user.target.wants'
        subprocess.run([*sudo, 'mkdir', '-p', wants_dir], check=True)
        subprocess.run(
            [*sudo, 'ln', '-sf', '/etc/systemd/system/rpi-camera.service', f'{wants_dir}/rpi-camera.service'],
            check=True,
        )
        click.echo(
            'systemd is not running (image build?) — skipped daemon-reload/start, '
            'created enable symlink manually. The service will come up on next boot.',
        )
