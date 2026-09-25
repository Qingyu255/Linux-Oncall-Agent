"""Create local lab credentials without displaying their values."""

import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1] / ".local/lab/secrets"
root.mkdir(parents=True, exist_ok=True, mode=0o700)
for name in ("target_token", "agent_token", "admin_token"):
    path = root / name
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32))
    os.chmod(path, 0o600)
print("Local credentials ready; values not displayed.")
