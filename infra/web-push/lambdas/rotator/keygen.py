# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared VAPID keypair generation (spec 028).

Single source of truth for the EC P-256 keypair in the raw base64url format
pywebpush expects, used by BOTH the rotation Lambda (``main.py``) and the
provisioning script (``provision/gen_vapid.py``) so the two cannot drift:
  - private_key: base64url(32-byte EC P-256 private scalar)
  - public_key : base64url(65-byte uncompressed public point, 0x04 || X || Y)

Pure function, no AWS or I/O -- unit-testable without the cloud.
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric import ec


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_vapid(subject: str) -> dict:
    """Return {private_key, public_key, subject} in raw base64url VAPID format."""
    priv = ec.generate_private_key(ec.SECP256R1())
    scalar = priv.private_numbers().private_value.to_bytes(32, "big")
    nums = priv.public_key().public_numbers()
    point = b"\x04" + nums.x.to_bytes(32, "big") + nums.y.to_bytes(32, "big")
    return {"private_key": _b64url(scalar), "public_key": _b64url(point), "subject": subject}
