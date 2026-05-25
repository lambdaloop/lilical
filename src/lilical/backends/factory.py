from __future__ import annotations

from lilical.backends.base import Backend
from lilical.models.account import Account
from lilical.storage.event_store import EventStore
from lilical.storage.secrets import SecretsStore


def build_backend_factory(secrets: SecretsStore, store: EventStore | None = None):
    def factory(account: Account) -> Backend:
        secret = secrets.get(account.id) or {}

        def _save_google_token(token_json: str) -> None:
            secrets.set(account.id, {"token": token_json})

        def _save_graph_cache(cache_json: str) -> None:
            current = secrets.get(account.id) or {}
            current["msal_cache"] = cache_json
            secrets.set(account.id, current)

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
                on_token_refreshed=_save_google_token,
                identity=account.identity,
            )
        if account.kind == "graph":
            from lilical.backends.graph import GraphBackend

            return GraphBackend(  # type: ignore[reportReturnType]
                account_id=account.id,
                token_cache_json=secret.get("msal_cache"),
                on_token_refreshed=_save_graph_cache,
                include_contacts=bool(account.include_contacts),
            )
        if account.kind == "subscription":
            if store is None:
                raise RuntimeError(
                    "subscription backend requires an EventStore; "
                    "pass `store=` to build_backend_factory"
                )
            from lilical.backends.subscription import SubscriptionBackend

            return SubscriptionBackend(account_id=account.id, store=store)  # type: ignore[reportReturnType]
        raise NotImplementedError(f"Backend for {account.kind} not yet implemented")

    return factory
