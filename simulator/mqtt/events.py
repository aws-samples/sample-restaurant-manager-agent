# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Store->cloud event envelopes + publisher (spec 023 Task 3).

The store publishes alert events over its existing outbound MQTT connection to
``stores/{store_id}/events/{domain}``. ``build_equipment_alert_envelope`` is a
pure function (unit-testable without MQTT) that builds the versioned envelope
from the real ``Equipment`` + ``Alert`` models. ``AlertEventPublisher`` adds the
store id and ships it via the ``ResponsePublisher`` (fire-and-forget: it reuses
the response disconnect-buffer and never raises into the caller).
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Protocol

from simulator.models.equipment import Alert, Equipment
from simulator.models.inventory import InventoryItem
from simulator.mqtt.publisher import ResponsePublisher

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
EQUIPMENT_DOMAIN = "equipment"
INVENTORY_DOMAIN = "inventory"


def build_equipment_alert_envelope(store_id: str, equipment: Equipment, alert: Alert) -> dict:
    """Build the versioned alert-event envelope from the real models.

    Self-contained (carries its own ``reading``) so downstream narration needs
    no store round-trip. ``dedup_key`` is ``store#domain#source#condition``.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "domain": EQUIPMENT_DOMAIN,
        "event_type": "alert_raised",
        "severity": alert.severity,
        "source_id": equipment.id,
        "condition": alert.type,
        "reading": {
            "current_temp": equipment.current_temp,
            "target_temp": equipment.target_temp,
            "alert_threshold": equipment.alert_threshold,
            "status": equipment.status,
            "unit": equipment.unit,
        },
        "timestamp": alert.timestamp.isoformat(),
        "dedup_key": f"{store_id}#{EQUIPMENT_DOMAIN}#{equipment.id}#{alert.type}",
    }


def build_inventory_alert_envelope(store_id: str, item: InventoryItem, sim_time) -> dict:
    """Build a low-stock alert-event envelope from an InventoryItem (spec 023 ext).

    Self-contained (carries the reading) so downstream narration needs no store
    round-trip. ``critical`` when out of stock (qty <= 0), else ``warning``.
    """
    out_of_stock = item.current_quantity <= 0
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "domain": INVENTORY_DOMAIN,
        "event_type": "alert_raised",
        "severity": "critical" if out_of_stock else "warning",
        "source_id": item.id,
        "condition": "low_stock",
        "reading": {
            "current_quantity": item.current_quantity,
            "reorder_threshold": item.reorder_threshold,
            "max_quantity": item.max_quantity,
            "unit": item.unit,
        },
        "timestamp": sim_time.isoformat(),
        "dedup_key": f"{store_id}#{INVENTORY_DOMAIN}#{item.id}#low_stock",
    }


class AlertEventSink(Protocol):
    """Structural type the equipment engine depends on to emit alert events."""

    async def publish_equipment_alert(self, equipment: Equipment, alert: Alert) -> None: ...


class AlertEventPublisher:
    """Publishes equipment alert events over the store MQTT bridge.

    Fire-and-forget: delegates to :meth:`ResponsePublisher.publish_event`, which
    publishes at QoS 1 when connected, buffers on disconnect, and never raises.
    """

    def __init__(self, publisher: ResponsePublisher, store_id: str) -> None:
        self._publisher = publisher
        self._store_id = store_id

    async def publish_equipment_alert(self, equipment: Equipment, alert: Alert) -> None:
        envelope = build_equipment_alert_envelope(self._store_id, equipment, alert)
        payload = json.dumps(envelope).encode("utf-8")
        await self._publisher.publish_event(EQUIPMENT_DOMAIN, payload)
        logger.info(
            "alert_event_published store_id=%s equipment=%s condition=%s severity=%s",
            self._store_id, equipment.id, alert.type, alert.severity,
        )

    async def publish_inventory_alert(self, item: InventoryItem, sim_time) -> None:
        """Publish a low-stock inventory alert event (spec 023 ext) to
        ``stores/{id}/events/inventory``. Fire-and-forget like the equipment path."""
        envelope = build_inventory_alert_envelope(self._store_id, item, sim_time)
        payload = json.dumps(envelope).encode("utf-8")
        await self._publisher.publish_event(INVENTORY_DOMAIN, payload)
        logger.info(
            "inventory_alert_published store_id=%s item=%s qty=%s severity=%s",
            self._store_id, item.id, item.current_quantity, envelope["severity"],
        )

    async def publish_ack(self, alert_id: str) -> None:
        """Publish an acknowledgment for a proactive alert to ``events/ack`` so
        the cloud resumes the waiting Step Functions execution (spec 023 Task 7)."""
        payload = json.dumps({
            "event_type": "ack", "store_id": self._store_id, "alert_id": alert_id,
        }).encode("utf-8")
        await self._publisher.publish_event("ack", payload)
        logger.info("alert_ack_published store_id=%s alert_id=%s", self._store_id, alert_id)
