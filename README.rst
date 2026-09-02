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

Embedding in Another Website
----------------------------

The stream drops straight into an ``<iframe>`` or an ``<img>`` on another site —
no configuration needed, because the server sends neither ``X-Frame-Options``
nor a ``frame-ancestors`` policy:

.. code-block:: html

    <img src="http://<ip_address>/webcam">

Drawing the stream into a ``<canvas>`` is different: without CORS the canvas
becomes *tainted* and ``getImageData()`` / ``toDataURL()`` throw. Start the
server with the embedding site's origin allowed:

.. code-block:: bash

    rpi-camera start --cors-origin https://ops.example.com   # or '*' for any site
    rpi-camera install --cors-origin https://ops.example.com # bake it into the service

and load the stream with ``crossorigin`` on the embedding page:

.. code-block:: html

    <img id="cam" crossorigin="anonymous" src="http://<ip_address>/webcam">
    <canvas id="view" width="1920" height="1080"></canvas>
    <script>
      const cam = document.getElementById('cam');
      const ctx = document.getElementById('view').getContext('2d');
      setInterval(() => ctx.drawImage(cam, 0, 0), 100);  // pixels are now readable
    </script>

The same setting makes ``fetch()`` work against ``/snapshot`` and the ``/api/``
endpoints, so a remote dashboard can read the camera state and drive the
controls. ``--cors-origin`` takes a comma-separated list of origins, or ``*``
for any origin; it is off by default. Note that a page served over HTTPS cannot
embed this camera at all — browsers block the mixed ``http://`` content.

Contributing
------------

Contributions are welcome! Please fork the repository and submit a pull request.

License
-------

This project is licensed under the Apache 2.0 License. See the LICENSE file for more details.

Contact
-------

For any questions or inquiries, please contact Tim at info@meltingplot.net