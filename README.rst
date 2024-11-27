Meltingplot RPi Camera
======================

Overview
--------

Meltingplot RPi Camera is a Python project designed to interface with a Raspberry Pi camera module.
It captures images and videos, processes them, and provides various functionalities for image analysis
and manipulation.

Features
--------

- Capture images and videos
- Image processing and analysis
- Integration with Raspberry Pi camera module
- Easy-to-use interface

Installation
------------

To install the required dependencies, run:

.. code-block:: bash

    sudo apt update
    sudo apt upgrade -y
    sudo apt install -y python3-picamera2
    python3 -m venv --system-site-packages venv
    source venv/bin/activate
    pip install meltingplot.rpi_camera
    rpi-camera install

Usage
-----

To start streaming images, run:

.. code-block:: bash

    sudo rpi-camera start

or as a service:

.. code-block:: bash

    sudo systemctl start rpi-camera

Viewing the Camera Feed
-----------------------

You can view the camera feed by opening `http://<ip_address>` in your web browser.

- To access the live stream, go to `http://<ip_address>:8081/`.
- To capture a snapshot, visit `http://<ip_address>/snapshot` or `http://<ip_address>/picture/1/current/`.

Contributing
------------

Contributions are welcome! Please fork the repository and submit a pull request.

License
-------

This project is licensed under the Apache 2.0 License. See the LICENSE file for more details.

Contact
-------

For any questions or inquiries, please contact Tim at info@meltingplot.net