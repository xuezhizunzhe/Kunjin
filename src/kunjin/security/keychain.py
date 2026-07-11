from __future__ import annotations

import subprocess
from typing import Optional

from kunjin.logging import redact_secrets


class CredentialStoreError(RuntimeError):
    pass


class KeychainTokenStore:
    def __init__(
        self,
        service: str = "com.kunjin.yangjibao",
        account: str = "default",
    ) -> None:
        self.service = service
        self.account = account

    def _run(self, command: list) -> subprocess.CompletedProcess:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )

    def save(self, token: str) -> None:
        if not token:
            raise CredentialStoreError("refusing to save an empty token")
        try:
            self._run(
                [
                    "/usr/bin/security",
                    "add-generic-password",
                    "-U",
                    "-s",
                    self.service,
                    "-a",
                    self.account,
                    "-w",
                    token,
                ]
            )
        except subprocess.CalledProcessError as exc:
            raise CredentialStoreError(redact_secrets(exc.stderr or "keychain save failed")) from exc

    def load(self) -> Optional[str]:
        try:
            result = self._run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    self.service,
                    "-a",
                    self.account,
                    "-w",
                ]
            )
            return result.stdout.strip() or None
        except subprocess.CalledProcessError as exc:
            if exc.returncode == 44:
                return None
            raise CredentialStoreError(redact_secrets(exc.stderr or "keychain load failed")) from exc

    def delete(self) -> None:
        try:
            self._run(
                [
                    "/usr/bin/security",
                    "delete-generic-password",
                    "-s",
                    self.service,
                    "-a",
                    self.account,
                ]
            )
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 44:
                raise CredentialStoreError(
                    redact_secrets(exc.stderr or "keychain delete failed")
                ) from exc

