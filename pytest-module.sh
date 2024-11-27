#!/bin/bash

set -e

pytest --cov-config .coveragerc --cov meltingplot tests/ -vv