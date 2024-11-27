#!/bin/bash

set -e

yapf --style .style.yapf -r -i meltingplot/
#yapf --style .style.yapf -r -i tests/