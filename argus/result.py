"""统一返回结构：所有工具均返回该 JSON 结构。"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def new_operation_id() -> str:
    return f"op-{uuid.uuid4().hex[:12]}"


@dataclass
class ToolResult:
    success: bool
    operation_id: str = field(default_factory=new_operation_id)
    data: Any = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def ok(data: Any = None, operation_id: str | None = None, warnings: list[str] | None = None) -> ToolResult:
    return ToolResult(
        success=True,
        data=data,
        operation_id=operation_id or new_operation_id(),
        warnings=warnings or [],
    )


def fail(error: str, code: str = "TOOL_ERROR", operation_id: str | None = None) -> ToolResult:
    return ToolResult(
        success=False,
        error=error,
        error_code=code,
        operation_id=operation_id or new_operation_id(),
    )
