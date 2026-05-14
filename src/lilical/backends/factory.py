from __future__ import annotations

from lilical.backends.base import Backend
from lilical.models.account import Account
from lilical.storage.secrets import SecretsStore


def build_backend_factory(secrets: SecretsStore):
    def factory(account: Account) -> Backend:
        secret = secrets.get(account.id) or {}
        if account.kind == "caldav":
            from lilical.backends.caldav import CalDavBackend
            return CalDavBackend(
                account_id=account.id,
                server_url=account.server_url or "",
                username=account.identity,
                password=secret.get("password", ""),
            )
        if account.kind == "google":
            from lilical.backends.google import GoogleBackend
            return GoogleBackend(
                account_id=account.id,
                token_json=secret.get("token"),
            )
        if account.kind == "graph":
            from lilical.backends.graph import GraphBackend
            return GraphBackend(
                account_id=account.id,
                token_json=secret.get("token"),
            )
        raise NotImplementedError(f"Backend for {account.kind} not yet implemented")
    return factory
