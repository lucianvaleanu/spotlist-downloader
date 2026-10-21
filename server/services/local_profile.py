import json
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class LocalProfile:
    """stores the single local user's slskd settings encrypted at rest."""

    def __init__(self) -> None:
        self.directory = Path(
            os.getenv("SPOTLIST_DATA_DIR", Path.home() / ".spotlist")
        ).expanduser()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.key_path = self.directory / "profile.key"
        self.profile_path = self.directory / "profile.enc"
        self._cipher = Fernet(self._load_or_create_key())

    def save(self, values: dict[str, Any]) -> None:
        payload = json.dumps(values).encode("utf-8")
        self.profile_path.write_bytes(self._cipher.encrypt(payload))
        self.profile_path.chmod(0o600)

    def load(self) -> dict[str, Any] | None:
        if not self.profile_path.exists():
            return None
        try:
            return json.loads(self._cipher.decrypt(self.profile_path.read_bytes()))
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise RuntimeError("The local profile cannot be decrypted") from exc

    def _load_or_create_key(self) -> bytes:
        configured_key = os.getenv("SPOTLIST_MASTER_KEY")
        if configured_key:
            return configured_key.encode("ascii")

        if self.key_path.exists():
            return self.key_path.read_bytes()

        key = Fernet.generate_key()
        self.key_path.write_bytes(key)
        self.key_path.chmod(0o600)
        return key
