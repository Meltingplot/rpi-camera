#!/bin/bash

set -e

source yapf-module.sh
source flake8-module.sh

if [ -d "dist" ]; then
    rm dist/*
fi
pip install --upgrade build
python3 -m build
#python3 setup.py bdist_wheel
twine check dist/*.whl