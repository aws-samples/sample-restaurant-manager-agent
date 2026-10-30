# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared helpers consumed by the gateway Lambdas and the response handler.

Modules:

* ``tools`` — static tool-name-to-domain map and read-tool classification.
* ``correlation`` — UUID generation, ``CommandResults`` item shape and key
  helpers.
* ``mqtt_client`` — thin wrapper around Paho TLS-MQTT connect with
  credentials resolved from Secrets Manager.
* ``ddb`` — typed helpers for DynamoDB operations on ``CommandResults``
  and ``StoreStateCache``.
"""
