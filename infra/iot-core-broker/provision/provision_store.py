# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-store IoT provisioning + secure cert delivery (spec 020 Task 2, R1.3=A).

For each store:
  - thing name == store_id (so client_id == thing name; thing-policy variables resolve).
  - attach the shared fleet policy (RestaurantStoreIotPolicy, thing-variable scoped).
  - create an X.509 cert and store the bundle (cert/key/CA) in Secrets Manager at
    ``restaurant-sim/iot/<store_id>/cert`` so the EC2 simulator fetches it at boot
    (R4.2 -- never baked into the image or repo).

Usage:
    python provision/provision_store.py --store-id sim-store-001
    python provision/provision_store.py --store-id sim-store-001 --teardown
"""
from __future__ import annotations

import argparse
import json
import urllib.request

import boto3

from policy import POLICY_NAME, build_policy_json

REGION = "us-west-2"
ROOT_CA_URL = "https://www.amazontrust.com/repository/AmazonRootCA1.pem"

# HTTPS-only opener for the root-CA fetch. ``urlopen``'s default opener also
# registers file/ftp/data handlers; this one cannot fetch anything but https.
_HTTPS_OPENER = urllib.request.OpenerDirector()
for _handler in (
    urllib.request.ProxyHandler(),
    urllib.request.HTTPSHandler(),
    urllib.request.HTTPRedirectHandler(),
    urllib.request.HTTPDefaultErrorHandler(),
    urllib.request.HTTPErrorProcessor(),
    urllib.request.UnknownHandler(),  # without it, open() of an unhandled scheme returns None
):
    _HTTPS_OPENER.add_handler(_handler)


def _secret_name(store_id: str) -> str:
    return f"restaurant-sim/iot/{store_id}/cert"


def provision(store_id: str) -> None:
    iot = boto3.client("iot", region_name=REGION)
    sm = boto3.client("secretsmanager", region_name=REGION)
    account = boto3.client("sts").get_caller_identity()["Account"]
    endpoint = iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]

    iot.create_thing(thingName=store_id)
    try:
        iot.create_policy(policyName=POLICY_NAME, policyDocument=build_policy_json(REGION, account))
    except iot.exceptions.ResourceAlreadyExistsException:
        pass  # shared fleet policy already exists

    cert = iot.create_keys_and_certificate(setAsActive=True)
    cert_arn = cert["certificateArn"]
    iot.attach_policy(policyName=POLICY_NAME, target=cert_arn)
    iot.attach_thing_principal(thingName=store_id, principal=cert_arn)

    with _HTTPS_OPENER.open(ROOT_CA_URL) as r:
        ca_pem = r.read().decode()

    bundle = {
        "endpoint": endpoint,
        "certPem": cert["certificatePem"],
        "privateKey": cert["keyPair"]["PrivateKey"],
        "caPem": ca_pem,
        "clientId": store_id,  # == thing name (R1.3 A)
    }
    name = _secret_name(store_id)
    try:
        sm.create_secret(Name=name, SecretString=json.dumps(bundle))
    except sm.exceptions.ResourceExistsException:
        sm.put_secret_value(SecretId=name, SecretString=json.dumps(bundle))

    print(json.dumps({
        "store_id": store_id, "thing": store_id, "client_id": store_id,
        "policy": POLICY_NAME, "cert_arn": cert_arn,
        "endpoint": endpoint, "secret": name,
    }, indent=2))


def teardown(store_id: str) -> None:
    iot = boto3.client("iot", region_name=REGION)
    sm = boto3.client("secretsmanager", region_name=REGION)
    try:
        principals = iot.list_thing_principals(thingName=store_id).get("principals", [])
    except iot.exceptions.ResourceNotFoundException:
        principals = []
    for cert_arn in principals:
        cid = cert_arn.split("/")[-1]
        for fn in (
            lambda: iot.detach_thing_principal(thingName=store_id, principal=cert_arn),
            lambda: iot.detach_policy(policyName=POLICY_NAME, target=cert_arn),
            lambda: iot.update_certificate(certificateId=cid, newStatus="INACTIVE"),
            lambda: iot.delete_certificate(certificateId=cid, forceDelete=True),
        ):
            try: fn()
            except Exception as e: print("skip", e)
    try: iot.delete_thing(thingName=store_id)
    except Exception as e: print("del thing", e)
    try: sm.delete_secret(SecretId=_secret_name(store_id), ForceDeleteWithoutRecovery=True)
    except Exception as e: print("del secret", e)
    print(f"teardown complete for {store_id} (shared policy {POLICY_NAME} left intact)")


def update_policy() -> None:
    """Push the current policy document as a new DEFAULT version of the shared
    fleet policy (spec 023 Task 2 adds the events/notifications grants).

    Creates the policy if absent; otherwise sets a new default version, pruning
    the oldest non-default version(s) first to stay under the 5-version IoT
    limit. Additive change -- existing commands/responses grants are preserved.
    """
    iot = boto3.client("iot", region_name=REGION)
    account = boto3.client("sts").get_caller_identity()["Account"]
    doc = build_policy_json(REGION, account)
    try:
        iot.create_policy(policyName=POLICY_NAME, policyDocument=doc)
        print(f"created policy {POLICY_NAME}")
        return
    except iot.exceptions.ResourceAlreadyExistsException:
        pass

    versions = iot.list_policy_versions(policyName=POLICY_NAME)["policyVersions"]
    non_default = sorted(
        (v for v in versions if not v["isDefaultVersion"]),
        key=lambda v: v["createDate"],
    )
    while len(versions) >= 5 and non_default:
        oldest = non_default.pop(0)
        iot.delete_policy_version(policyName=POLICY_NAME, policyVersionId=oldest["versionId"])
        versions = [v for v in versions if v["versionId"] != oldest["versionId"]]

    new_version = iot.create_policy_version(
        policyName=POLICY_NAME, policyDocument=doc, setAsDefault=True
    )
    print(f"updated policy {POLICY_NAME} -> default version {new_version['policyVersionId']}")


def _require_store(store_id: str | None) -> str:
    if not store_id:
        raise SystemExit("--store-id is required for this action")
    return store_id


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--store-id")
    p.add_argument("--teardown", action="store_true")
    p.add_argument(
        "--update-policy",
        action="store_true",
        help="update the shared fleet policy to a new default version (no --store-id needed)",
    )
    a = p.parse_args()
    if a.update_policy:
        update_policy()
    elif a.teardown:
        teardown(_require_store(a.store_id))
    else:
        provision(_require_store(a.store_id))
