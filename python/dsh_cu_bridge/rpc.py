"""Shared request/response plumbing for the dsh-cu bridge.

The bridge answers one newline-delimited JSON object per request on stdin and
writes exactly one newline-delimited JSON object per request on stdout. stdout
is a data channel; every diagnostic goes to stderr, because a library print on
the data channel would desynchronize the client's frame parser.

Part of dsh-cu. Portions are derived from kwin-mcp (MIT,
Copyright (c) 2026 Byeonghoon Yoo) — see NOTICE.
"""

from __future__ import annotations

import json
import sys
from typing import Any


class BridgeError(Exception):
    """One failed operation, routable by the TypeScript tool layer.

    ``code`` is a stable machine-readable identifier (``EIS_UNAVAILABLE``,
    ``NO_SUCH_WINDOW``, ...) so the harness tool surfaces the reason the
    caller can act on instead of a bare message.
    """

    def __init__(self, message: str, code: str = "ERROR", detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail

    def as_payload(self) -> dict[str, Any]:
        """Render this failure as the ``error`` member of a response frame."""
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


def encode(frame: dict[str, Any]) -> str:
    """Serialize one response frame without failing on unexpected values.

    ``default=str`` keeps a stray QVariant/bytes value from turning an
    otherwise successful operation into a serialization error.
    """
    return json.dumps(frame, default=str, ensure_ascii=False)


def emit(frame: dict[str, Any], stream: Any = None) -> None:
    """Write one response frame and flush it."""
    out = stream if stream is not None else sys.stdout
    out.write(encode(frame) + "\n")
    out.flush()


def ok(request_id: Any, result: Any) -> dict[str, Any]:
    """Build a success frame."""
    return {"id": request_id, "ok": True, "result": result}


def failure(request_id: Any, error: BaseException) -> dict[str, Any]:
    """Build a failure frame, keeping a {@link BridgeError} code when there is one."""
    if isinstance(error, BridgeError):
        return {"id": request_id, "ok": False, "error": error.as_payload()}
    return {
        "id": request_id,
        "ok": False,
        "error": {
            "code": type(error).__name__,
            "message": str(error) or type(error).__name__,
        },
    }
