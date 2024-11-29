# -*- coding: utf-8 -*-

import os
import versioneer
from setuptools import setup, find_packages

PACKAGE_NAME = "meltingplot.rpi_camera"

REQUIREMENTS = []
with open(os.path.join(os.path.dirname(__file__), 'requirements.txt')) as f:
    for line in f:
        REQUIREMENTS.append(line.strip())

REQUIREMENTS_TEST = []
with open(
        os.path.join(os.path.dirname(__file__), 'requirements_test.txt')) as f:
    for line in f:
        REQUIREMENTS_TEST.append(line.strip())


with open('README.rst') as f:
    README = f.read()

with open('LICENSE') as f:
    LICENSE = f.read()

setup(
    name=PACKAGE_NAME,
    description='RPi Camera MJPEG Streamer.',
    long_description=README,
    long_description_content_type='text/x-rst',
    version=versioneer.get_version(),
    cmdclass=versioneer.get_cmdclass(),
    author='Tim Schneider',
    author_email='tim@meltingplot.net',
    url='',
    license=LICENSE,
    packages=find_packages(exclude=('tests', 'docs', 'venv')),
    include_package_data=True,
    zip_safe=False,
    install_requires=REQUIREMENTS,
    extras_require={
        'test': REQUIREMENTS_TEST,
    },
    entry_points={
        'console_scripts': [
            "rpi-camera = meltingplot.rpi_camera.__main__:main",
        ],
    },
    data_files=[
        ('', [
            'rpi-camera.service',
            'reboot_on_wifi_disconnect.sh',
            ]),
    ],
)
