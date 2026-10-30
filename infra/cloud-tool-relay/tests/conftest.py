# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test configuration for the cloud-tool-relay CDK tests.

Adds the project root (``infra/cloud-tool-relay``) to ``sys.path`` so the
tests can import the ``infrastructure`` package without requiring the caller
to set ``PYTHONPATH`` manually.

Also adds ``infra/cloud-tool-relay/lambdas`` so that the gateway Lambda's
``from shared.xxx import ...`` imports resolve correctly in the test
environment (in the Lambda runtime, ``lambdas/`` is the code root and
``shared`` is a top-level package).

Also adds ``infra/cloud-tool-relay/containers`` so that the
``agent_common`` package (used by domain agent containers) is importable
in the test environment.
"""
from __future__ import annotations

import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parents[1])
_lambdas_root = str(Path(__file__).resolve().parents[1] / "lambdas")
_containers_root = str(Path(__file__).resolve().parents[1] / "containers")

if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
if _lambdas_root not in sys.path:
    sys.path.insert(0, _lambdas_root)
if _containers_root not in sys.path:
    sys.path.insert(0, _containers_root)
