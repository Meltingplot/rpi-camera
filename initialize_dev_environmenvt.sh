sudo apt install -y libatlas-base-dev libcap-dev python3-prctl libcamera-dev
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip3 install picamera2 rpi-libcamera
pip3 install -r requirements_test.txt