from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"


@dataclass
class CheckResult:
    name: str
    status: str
    severity: str = "low"
    message: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)
    remediation: str = ""
    elapsed_seconds: float = 0.0

    @classmethod
    def pass_(cls, name: str, message: str = "ok", **kwargs: Any) -> "CheckResult":
        return cls(name=name, status=PASS, message=message, **kwargs)

    @classmethod
    def fail(
        cls,
        name: str,
        message: str,
        *,
        severity: str = "high",
        details: list[dict[str, Any]] | None = None,
        remediation: str = "",
    ) -> "CheckResult":
        return cls(
            name=name,
            status=FAIL,
            severity=severity,
            message=message,
            details=details or [],
            remediation=remediation,
        )

    @classmethod
    def warn(
        cls,
        name: str,
        message: str,
        *,
        severity: str = "medium",
        details: list[dict[str, Any]] | None = None,
        remediation: str = "",
    ) -> "CheckResult":
        return cls(
            name=name,
            status=WARN,
            severity=severity,
            message=message,
            details=details or [],
            remediation=remediation,
        )

    @classmethod
    def skip(cls, name: str, message: str, remediation: str = "") -> "CheckResult":
        return cls(name=name, status=SKIP, severity="low", message=message, remediation=remediation)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "severity": self.severity,
            "message": self.message,
            "details": self.details,
            "remediation": self.remediation,
            "elapsed_seconds": self.elapsed_seconds,
        }
