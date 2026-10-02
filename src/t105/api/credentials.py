"""Credential loading. Secrets are never printed, logged, or committed.

Resolution order for profile P in {"testing", "deployment"}:
  1. env vars ROOSTOO_API_KEY / ROOSTOO_API_SECRET (preferred on EC2, via systemd EnvironmentFile)
  2. files <T105_KEY_DIR>/<P>_api_key and <P>_api_secret
     (T105_KEY_DIR defaults to ./extra_info/api_keys, which is git-ignored)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROFILES = ("testing", "deployment")


@dataclass(frozen=True)
class Credentials:
    profile: str
    api_key: str = field(repr=False)
    secret: str = field(repr=False)

    def fingerprint(self) -> str:
        """Non-reversible short id safe for logs (last 4 chars of key)."""
        return f"{self.profile}:...{self.api_key[-4:]}"


def load_credentials(profile: str, root: Path | None = None) -> Credentials:
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {PROFILES}")
    k, s = os.environ.get("ROOSTOO_API_KEY"), os.environ.get("ROOSTOO_API_SECRET")
    if k and s:
        return Credentials(profile, k.strip(), s.strip())
    key_dir = Path(os.environ.get("T105_KEY_DIR", (root or Path.cwd()) / "extra_info" / "api_keys"))
    kf, sf = key_dir / f"{profile}_api_key", key_dir / f"{profile}_api_secret"
    if not (kf.exists() and sf.exists()):
        raise FileNotFoundError(f"credentials for profile '{profile}' not found in {key_dir}")
    return Credentials(profile, kf.read_text().strip(), sf.read_text().strip())
