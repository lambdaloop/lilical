from __future__ import annotations

from lilical.storage.secrets import SecretsStore
from lilical.models.account import Account


def build_backend_factory(secrets: SecretsStore):
    def factory(account: Account):
        raise NotImplementedError(f"Backend for {account.kind} not yet implemented")
    return factory
