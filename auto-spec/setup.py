#!/usr/bin/env python3
"""
Setup script for development installation.
"""

from setuptools import setup, find_packages

setup(
    name="auto-spec",
    version="1.0.0",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "auto-spec=auto_spec.cli:main",
        ],
    },
)
