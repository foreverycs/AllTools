"""Request-ID middleware helpers (correlation for logs and responses)."""

from __future__ import annotations

import contextvars
import secrets
from typing import Optional

REQUEST_ID_HEADER = "X-Request-ID"

_request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "toolkit_request_id", default=None
)


def new_request_id() -> str:
    return secrets.token_hex(8)


def set_request_id(value: Optional[str]) -> contextvars.Token:
    return _request_id_var.set(value)


def get_request_id() -> Optional[str]:
    return _request_id_var.get()


def reset_request_id(token: contextvars.Token) -> None:
    _request_id_var.reset(token)
