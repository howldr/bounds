"""The two wire shapes. Both frozen — a request must not be mutable between
the check and the dispatch, or the check means nothing."""

from dataclasses import dataclass, field
from typing import Any, Literal

TriggerType = Literal["scheduled", "event", "nl"]


@dataclass(frozen=True)
class ActionRequest:
    idempotency_key: str  # caller-supplied; hash(trigger_source + payload)
    trigger_type: TriggerType
    trigger_source: str
    requested_by: str
    agent: str
    action: str  # 'send_email'
    params: dict[str, Any] = field(default_factory=dict)
    data_touched: list[str] = field(default_factory=list)
    model_tier_used: str = "local"
    tenant: str = "default"  # the isolation boundary above agents
    correlation_id: str | None = None  # one thread of work across many actions


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str | None = None  # populated iff not allow
    boundary_version: int | None = None
    credential_ref: str | None = None  # only surfaced on allow
    # A would-be allow waiting on a named human. hold implies not allow;
    # everything that would deny still denies — only requests that cleared
    # every other gate are worth a human's attention.
    hold: bool = False
