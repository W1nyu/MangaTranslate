"""Local runtime paths shared by the server and model installation scripts."""
from __future__ import annotations

import os
from pathlib import Path


LOCAL_SERVER_ROOT = Path(__file__).resolve().parents[1]
ARGOS_ROOT = LOCAL_SERVER_ROOT / ".argos"


def load_local_env(path: Path | None = None) -> None:
    """Load a small local ``.env`` file without executing it or overriding the shell.

    API keys belong to the local FastAPI process, not the browser extension.  A
    dependency-free parser keeps the setup command simple and deliberately only
    accepts ``NAME=value`` lines.  Existing process environment variables win so
    deployment tooling can override a value safely.
    """
    env_path = path or LOCAL_SERVER_ROOT / ".env"
    if not env_path.is_file():
        return

    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        # A missing/unreadable optional config file must not prevent the offline
        # Argos mode from starting.
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def configure_local_runtime() -> None:
    """Keep Argos state inside this project instead of a protected user-profile folder."""
    locations = {
        "XDG_CONFIG_HOME": ARGOS_ROOT / "config",
        "XDG_DATA_HOME": ARGOS_ROOT / "data",
        "XDG_CACHE_HOME": ARGOS_ROOT / "cache",
    }
    for name, path in locations.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    load_local_env()


configure_local_runtime()
