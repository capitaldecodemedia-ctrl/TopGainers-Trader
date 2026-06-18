"""Shared .env read/write helpers for auth scripts."""

from pathlib import Path

ENV_PATH = Path(__file__).parent.parent / ".env"


def load_env_value(key: str) -> str:
    if not ENV_PATH.exists():
        return ""
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def update_env_value(key: str, value: str):
    """Updates or appends a key=value line in .env."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
