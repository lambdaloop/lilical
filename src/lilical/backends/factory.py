from __future__ import annotations

from lilical.backends.base import Backend
from lilical.models.account import Account
from lilical.storage.secrets import SecretsStore


def build_backend_factory(secrets: SecretsStore):
    def factory(account: Account) -> Backend:
        if account.kind == "caldav":
            from lilical.backends.caldav import CalDavBackend
            secret = secrets.get(account.id) or {}
            return CalDavBackend(
                account_id=account.id,
                server_url=account.server_url or "",
                username=account.identity,
                password=secret.get("password", ""),
            )
        raise NotImplementedError(f"Backend for {account.kind} not yet implemented")
    return factory
