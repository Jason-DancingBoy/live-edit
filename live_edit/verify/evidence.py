# live_edit/verify/evidence.py
"""Evidence model for verify-then-approve."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    UNVERIFIED = "unverified"


@dataclass
class CheckResult:
    id: str
    status: str
    detail: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == CheckStatus.PASS


@dataclass
class Evidence:
    session_id: str
    commit_hash: str
    layers: dict[str, dict] = field(default_factory=dict)
    verify_attempts: int = 0
    decision: str = "human"
    reason: str = ""

    @property
    def overall(self) -> str:
        statuses = [layer.get("status") for layer in self.layers.values()]
        if any(s == CheckStatus.FAIL for s in statuses):
            return CheckStatus.FAIL
        if any(s == CheckStatus.UNVERIFIED for s in statuses):
            return CheckStatus.UNVERIFIED
        return CheckStatus.PASS

    def to_dict(self) -> dict:
        return asdict(self) | {"overall": self.overall}

    @classmethod
    def from_dict(cls, d: dict) -> Evidence:
        return cls(
            session_id=d.get("session_id", ""),
            commit_hash=d.get("commit_hash", ""),
            layers=d.get("layers", {}),
            verify_attempts=d.get("verify_attempts", 0),
            decision=d.get("decision", "human"),
            reason=d.get("reason", ""),
        )
