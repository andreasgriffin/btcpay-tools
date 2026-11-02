"""Response models shared by the subscription API and its Python clients."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class SubscriptionPhase(str, Enum):
    """Lifecycle phase; values mirror the Greenfield `SubscriptionPhase` enum."""

    EXPIRED = "expired"
    GRACE = "grace"
    NORMAL = "normal"
    TRIAL = "trial"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> SubscriptionPhase:
        return cls.UNKNOWN

    @classmethod
    def from_dict(cls, subscriber: dict[str, Any]) -> SubscriptionPhase:
        raw = subscriber.get("phase")
        if not isinstance(raw, str):
            return cls.UNKNOWN
        return cls(raw.lower())


class SubscriptionStatusCode(str, Enum):
    ACTIVE = "active"
    TRIAL = "trial"
    GRACE = "grace"
    EXPIRED = "expired"
    SUSPENDED = "suspended"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> SubscriptionStatusCode:
        return cls.UNKNOWN

    @classmethod
    def from_dict(cls, subscriber: dict[str, Any]) -> SubscriptionStatusCode:
        """Order matters: suspension wins, and expiry wins over the labelled phase."""
        if subscriber.get("isSuspended") is True:
            return cls.SUSPENDED
        phase = SubscriptionPhase.from_dict(subscriber)
        is_active = subscriber.get("isActive")
        if phase is SubscriptionPhase.EXPIRED or is_active is False:
            return cls.EXPIRED
        if phase is SubscriptionPhase.NORMAL and is_active is True:
            return cls.ACTIVE
        if phase is SubscriptionPhase.GRACE:
            return cls.GRACE
        if phase is SubscriptionPhase.TRIAL:
            return cls.TRIAL
        return cls.UNKNOWN


class SubscriptionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    plan_id: str
    status: SubscriptionStatusCode
    phase: SubscriptionPhase | None = None
    isActive: bool | None = None
    periodEnd: int | None = None
    trialEnd: int | None = None
    gracePeriodEnd: int | None = None
    processingInvoiceId: str | None = None

    @classmethod
    def from_subscriber(
        cls,
        plan_id: str,
        subscriber: dict[str, Any] | None,
    ) -> SubscriptionStatusResponse:
        if subscriber is None:
            return cls(plan_id=plan_id, status=SubscriptionStatusCode.NOT_FOUND)
        return cls(
            plan_id=plan_id,
            status=SubscriptionStatusCode.from_dict(subscriber),
            phase=SubscriptionPhase.from_dict(subscriber),
            isActive=subscriber.get("isActive"),
            periodEnd=subscriber.get("periodEnd"),
            trialEnd=subscriber.get("trialEnd"),
            gracePeriodEnd=subscriber.get("gracePeriodEnd"),
            processingInvoiceId=subscriber.get("processingInvoiceId"),
        )


class PlanCatalogItem(BaseModel):
    """Public plan metadata from Greenfield; `memberCount` is intentionally omitted."""

    model_config = ConfigDict(extra="ignore")

    plan_id: str
    name: str
    description: str | None = None
    price: str
    currency: str
    recurringType: str
    trialDays: int | None = None
    gracePeriodDays: int | None = None
    status: str
    features: list[str] = Field(default_factory=list)

    @classmethod
    def from_greenfield(cls, plan_id: str, plan: dict[str, Any]) -> PlanCatalogItem:
        return cls.model_validate({**plan, "plan_id": plan_id})


class PlanCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    plans: list[PlanCatalogItem]
