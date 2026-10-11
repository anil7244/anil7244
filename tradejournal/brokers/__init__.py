"""Broker API clients. Each module exposes login helpers and fetch_fills()."""
from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from .. import config


def load_token(broker: str) -> str | None:
    path = config.tokens_path()
    if not path.exists():
        return None
    return json.loads(path.read_text()).get(broker, {}).get("access_token")


def save_token(broker: str, token: str, **extra) -> None:
    path = config.tokens_path()
    data = json.loads(path.read_text()) if path.exists() else {}
    data[broker] = {"access_token": token, "saved_at": datetime.now().isoformat(timespec="seconds"), **extra}
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)


def extract_param(value: str, name: str) -> str:
    """Accept either the bare value or the full redirect URL the broker sent you to."""
    value = value.strip()
    if value.startswith("http"):
        params = parse_qs(urlparse(value).query)
        if name not in params:
            raise ValueError(f"'{name}' not found in URL")
        return params[name][0]
    return value


class AuthError(RuntimeError):
    pass
