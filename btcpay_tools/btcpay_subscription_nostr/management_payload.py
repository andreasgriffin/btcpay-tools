from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ManagementPayload:
    management_url: str

    @classmethod
    def from_json(cls, raw: Any) -> ManagementPayload:
        if not isinstance(raw, dict):
            raise RuntimeError("Management payload must be a JSON object")
        management_url = raw.get("management_url")
        if not isinstance(management_url, str) or not management_url:
            raise RuntimeError("Management payload is missing management_url")
        return cls(management_url=management_url)

    def to_json(self) -> dict[str, str]:
        return {"management_url": self.management_url}
