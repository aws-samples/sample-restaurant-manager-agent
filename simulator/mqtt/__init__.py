# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Store-side MQTT bridge — outbound transport to cloud agents.

This package makes the simulator reachable from cloud-hosted AI agents
without accepting any inbound network connection. A single outbound TLS
connection to a managed MQTT broker (Amazon MQ with ActiveMQ by default;
AWS IoT Core as an alternative) carries commands from the cloud and
responses from the store.

Modules:
    config      — MQTTConnectionConfig + env loader
    client      — MQTTClient wrapper (reconnect, QoS 1, session persistence)
    dedup       — CorrelationIdCache (LRU + TTL) for idempotency
    publisher   — ResponsePublisher (publish + disconnect buffering)
    dispatcher  — CommandDispatcher (parse, validate, dispatch, respond)
    bridge      — MQTTBridge orchestration class (start/stop lifecycle)
"""
