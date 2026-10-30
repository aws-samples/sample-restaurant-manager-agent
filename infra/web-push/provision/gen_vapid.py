#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generate a VAPID keypair for Web Push and optionally store it (spec 025 Task 1d;
SSM public-key param added in spec 028).

Keypair generation is shared with the rotation Lambda via ``lambdas/rotator/keygen.py``
(single source of truth, so provisioning and rotation cannot drift). The push-sender
Lambda reads ``{"private_key","subject"}`` from the secret; the store backend reads the
application-server PUBLIC key from the SSM parameter (spec 028) -- the private key never
leaves the cloud.

Usage:
  python gen_vapid.py --subject mailto:ops@example.com            # print only
  python gen_vapid.py --subject mailto:ops@example.com --put \\
      --secret-id restaurant-sim/web-push/vapid \\
      --public-key-param /restaurant-sim/web-push/vapid-public-key --region us-west-2
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Import the shared keygen from the rotator Lambda dir (single source of truth).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lambdas", "rotator"))
from keygen import generate_vapid  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate VAPID keys for Web Push.")
    ap.add_argument("--subject", default="mailto:ops@example.com",
                    help="VAPID 'sub' claim (mailto: or https: contact)")
    ap.add_argument("--put", action="store_true",
                    help="store the keys in Secrets Manager + the public key in SSM")
    ap.add_argument("--secret-id", default="restaurant-sim/web-push/vapid")
    ap.add_argument("--public-key-param", default="/restaurant-sim/web-push/vapid-public-key",
                    help="SSM parameter the store backend reads for the current public key")
    ap.add_argument("--region", default="us-west-2")
    args = ap.parse_args()

    keys = generate_vapid(args.subject)
    print("VAPID public key (served to the browser via the SSM param below):")
    print(keys["public_key"])

    if args.put:
        import boto3
        boto3.client("secretsmanager", region_name=args.region).put_secret_value(
            SecretId=args.secret_id, SecretString=json.dumps(keys)
        )
        boto3.client("ssm", region_name=args.region).put_parameter(
            Name=args.public_key_param, Value=keys["public_key"], Type="String", Overwrite=True
        )
        print(f"stored private_key + subject in secret {args.secret_id}")
        print(f"stored public_key in SSM parameter {args.public_key_param}")
    else:
        print("(dry run; re-run with --put to store in Secrets Manager + SSM)")


if __name__ == "__main__":
    main()
