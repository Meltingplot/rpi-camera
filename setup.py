"""Minimal setup.py — all static metadata lives in pyproject.toml.

This file exists only so versioneer can hook into the build commands
(build_py / sdist) to bake the git-derived version into _version.py.
"""
from setuptools import setup

import versioneer

setup(
    version=versioneer.get_version(),
    cmdclass=versioneer.get_cmdclass(),
)
