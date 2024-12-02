#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
This script sets up an HTTP server to stream video from a Raspberry Pi camera using the Picamera2 library.

It is based on the official Picamera2 example for streaming video from a Raspberry Pi camera using the MJPEG format.

https://github.com/raspberrypi/picamera2/blob/main/examples/mjpeg_server_2.py

Which is licensed under the BSD 2-Clause License (https://github.com/raspberrypi/picamera2/blob/main/LICENSE)

It provides both a web interface for viewing the stream and an endpoint for fetching the current frame as a JPEG image.
Classes:
    StreamingOutput: A class that buffers the video frames and notifies waiting threads when a new frame is available.
    StreamingHandler: A request handler for serving the MJPEG stream.
    HttpHandler: A request handler for serving the HTML page and current frame as a JPEG image.
    StreamingServer: A server class that supports threading and reuses addresses.
Functions:
    main: The main function that configures the camera, starts recording, and sets up the HTTP servers.
HTML Page:
    The HTML page includes a link to the stream and a link to fetch the current frame.
Endpoints:
    / or /index.html: Serves the HTML page.
    /picture/1/current/: Serves the current frame as a JPEG image.
    /webcam: Serves the MJPEG stream.
Usage:
    Run the script to start the HTTP servers on ports 80 and 8081.
"""

import asyncio
import io
import logging
import socketserver
from http import server
from threading import Condition
from urllib.parse import urlparse

import click

from libcamera import controls

from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

import piexif


PAGE = """\
<html>
<head>
<title>Meltingplot RPi Camera MJPEG streaming</title>
</head>
<body>
<h1>Meltingplot RPi Camera</h1>
<img src="data:image/png;base64,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" width=160 height=66></img>
<p><a href="picture/1/current/">Screenshot URL</a> <span>picture/1/current/</span></p>
<p><a id="streamLink" href="">Stream URL</a> <span>Streaming URL hostname:8081</span></p>
<script type="text/javascript">
    document.addEventListener("DOMContentLoaded", function() {
        var hostname = window.location.hostname;
        var streamLink = document.getElementById("streamLink");
        streamLink.href = "http://" + hostname + ":8081";
    });
</script>
</body>
</html>
"""  # noqa:E501


class StreamingOutput(io.BufferedIOBase):
    """A class that buffers the video frames and notifies waiting threads when a new frame is available."""

    def __init__(self, rotation: int = 0):
        """
        Initialize the streaming output with a frame buffer and condition.

        Args:
            rotation (int): The rotation angle for the JPEG image. Must be one of
                            [0, 90, 180, 270]. Default is 0.

        Raises:
            ValueError: If the rotation value is not one of [0, 90, 180, 270].

        Attributes:
            frame (None): Placeholder for the frame buffer.
            condition (Condition): Condition variable for thread synchronization.
        """
        self.frame = None
        self.condition = Condition()

        # Set the orientation of the JPEG image based on the rotation value
        # more info: http://sylvana.net/jpegcrop/exif_orientation.html
        if rotation == 0:
            orientation = 1
        elif rotation == 90:
            orientation = 6
        elif rotation == 180:
            orientation = 3
        elif rotation == 270:
            orientation = 8
        else:
            raise ValueError("Invalid rotation value")

        exif_data = piexif.dump({
            "0th": {
                piexif.ImageIFD.Orientation: orientation,
            },
        })
        jpeg_app_len = len(exif_data) + 2
        self._jpeg_app1 = b"\xff\xe1" + (jpeg_app_len).to_bytes(2, byteorder="big") + exif_data

    def write(self, buf):
        """Write the buffer to the stream and notify waiting threads."""
        with self.condition:
            self.frame = buf[:2] + self._jpeg_app1 + buf[2:]
            self.condition.notify_all()


class StreamingHandler(server.BaseHTTPRequestHandler):
    """A request handler for serving the MJPEG stream."""

    frame_buffer = None

    def do_GET(self):  # noqa:N802
        """Serve the MJPEG stream."""
        url = urlparse(self.path)
        if url.path == '/' or url.path == '/webcam':
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            try:
                while True:
                    with self.frame_buffer.condition:
                        self.frame_buffer.condition.wait()
                        frame = self.frame_buffer.frame
                    self.wfile.write(b'--FRAME\r\n')
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', len(frame))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b'\r\n')
            except Exception as e:
                logging.warning('Removed streaming client %s: %s', self.client_address, str(e))
        else:
            self.send_error(404)
            self.end_headers()


class HttpHandler(server.BaseHTTPRequestHandler):
    """A request handler for serving the HTML page and current frame as a JPEG image."""

    frame_buffer = None

    def do_GET(self):  # noqa:N802
        """Serve the HTML page or the current frame as a JPEG image."""
        url = urlparse(self.path)

        if url.path == '/' or url.path == '/index.html':
            content = PAGE.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        elif url.path == '/picture/1/current/' or url.path == '/snapshot':
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            try:
                with self.frame_buffer.condition:
                    self.frame_buffer.condition.wait()
                    frame = self.frame_buffer.frame
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', len(frame))
                self.end_headers()
                self.wfile.write(frame)
                self.wfile.write(b'\r\n')
            except Exception as e:
                logging.warning('Removed client %s: %s', self.client_address, str(e))
        else:
            self.send_error(404)
            self.end_headers()


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    """This class extends the HTTPServer class to support threading and reuse addresses."""

    allow_reuse_address = True
    daemon_threads = True


@click.command()
def start():
    """
    Initialize and start the Raspberry Pi camera for video streaming.

    Configure the camera to record video at 1920x1080 resolution,
    set up the streaming output, and start the HTTP and streaming servers on
    specified ports (80 and 8081). Set the autofocus mode to continuous.

    Ensure that the camera recording is stopped properly when the
    servers are no longer running.

    Raise:
        Exception: If there is an error during the server execution or camera operation.
    """
    frame_buffer = StreamingOutput(rotation=180)
    HttpHandler.frame_buffer = frame_buffer
    StreamingHandler.frame_buffer = frame_buffer

    try:
        picam2 = Picamera2()
    except IndexError as e:
        logging.error("Error initializing the camera: %s - is a RPi camera connected?", str(e))
        raise

    picam2.configure(picam2.create_video_configuration(main={"size": (1920, 1080)}))
    picam2.start_recording(MJPEGEncoder(), FileOutput(frame_buffer))
    picam2.set_controls({
        "AfMode": controls.AfModeEnum.Continuous,
        "FrameRate": 10,
    })

    try:

        async def start_server(handler, port):
            address = ('', port)
            server = StreamingServer(address, handler)
            with server:
                await asyncio.get_event_loop().run_in_executor(None, server.serve_forever)

        loop = asyncio.get_event_loop()
        tasks = [start_server(HttpHandler, 80), start_server(StreamingHandler, 8081)]
        loop.run_until_complete(asyncio.gather(*tasks))
    finally:
        picam2.stop_recording()
