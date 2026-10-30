# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test configuration for the simulator-ec2-alb CDK tests.

Adds the project root (``infra/simulator-ec2-alb``) to ``sys.path`` so the
tests can import the ``infrastructure`` package without requiring the caller
to set ``PYTHONPATH`` manually.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
