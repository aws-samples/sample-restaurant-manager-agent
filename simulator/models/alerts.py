# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Alert inbox models (spec 023 Task 6).

The store receives policy-approved notifications from the cloud on
``stores/{store_id}/notifications/+`` and keeps them in an in-app inbox so the
always-on display can render active/recent alerts (the durable delivery floor).
``AlertNotification`` is lenient (ignores unknown keys) so the cloud notification
payload can evolve without breaking the store.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AlertNotification(BaseModel):
    """A proactive alert notification received from the cloud."""

    model_config = ConfigDict(extra="ignore")

    alert_id: str | None = None
    store_id: str | None = None
    severity: str | None = None
    domain: str | None = None
    source_id: str | None = None
    condition: str | None = None
    dedup_key: str | None = None
    message: str | None = None
    reading: dict | None = None
    timestamp: str | None = None
    acknowledged: bool = False


class AlertInboxState(BaseModel):
    """In-app inbox of received alert notifications (newest last)."""

    entries: list[AlertNotification] = []
