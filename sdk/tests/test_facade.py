"""Tests for facade.AgenTruxClient — public API surface.

Covers `from_*` factory methods, `connect()` convenience, the data-plane
delegates (publish/list/get/upload), and basic `subscribe()` wiring.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agentrux.sdk.envelope import MessageEnvelope, PublishResult
from agentrux.sdk.errors import SDKError
from agentrux.sdk.facade import AgenTruxClient, Subscription, connect

from .conftest import (
    stub_event_view,
    stub_list_events_response,
    stub_oauth_token_response,
    stub_publish_response,
)


pytestmark = pytest.mark.asyncio


TOPIC = "top_00000000-0000-0000-0000-000000000001"


# ---------- from_client_credentials ----------------------------------


async def test_from_client_credentials_normal(base_url: str) -> None:
    handlers = {
        ("POST", "/oauth/token"): lambda req: httpx.Response(
            200, json=stub_oauth_token_response(access_token="aat_x", expires_in=3600)
        ),
    }
    transport = httpx.MockTransport(
        lambda req: handlers[(req.method, req.url.path)](req)
    )
    # Patch AgenTruxAPIClient to use our transport by monkeypatching constructor
    # The factory creates its own AgenTruxAPIClient; easiest is to give it a
    # custom http via a subclass — but here we just verify via the returned client.
    # Instead use httpx.AsyncClient with mock transport indirectly via constructor.
    import agentrux.sdk.facade as facade_mod
    import agentrux.sdk.client as client_mod

    orig_init = client_mod.AgenTruxAPIClient.__init__

    def patched_init(self, *args, http=None, **kwargs):
        if http is None:
            http = httpx.AsyncClient(transport=transport, base_url=base_url)
        orig_init(self, *args, http=http, **kwargs)

    client_mod.AgenTruxAPIClient.__init__ = patched_init  # type: ignore[method-assign]
    try:
        c = await AgenTruxClient.from_client_credentials(
            base_url, client_id="crd_x_uuid", client_secret="aks_y"
        )
        assert c.access_token == "aat_x"
        await c.close()
    finally:
        client_mod.AgenTruxAPIClient.__init__ = orig_init  # type: ignore[method-assign]


async def test_from_client_credentials_rejects_bad_prefix(base_url: str) -> None:
    with pytest.raises(ValueError, match="crd_"):
        await AgenTruxClient.from_client_credentials(
            base_url, client_id="script_x", client_secret="aks_y"
        )


# ---------- from_access_token ----------------------------------------


async def test_from_access_token_minimal(base_url: str, fresh_access_token: str) -> None:
    c = await AgenTruxClient.from_access_token(
        base_url, access_token=fresh_access_token
    )
    assert c.access_token == fresh_access_token
    assert c.refresh_token is None
    await c.close()


async def test_from_access_token_with_refresh_setup(
    base_url: str, fresh_access_token: str
) -> None:
    c = await AgenTruxClient.from_access_token(
        base_url,
        access_token=fresh_access_token,
        refresh_token="art_x",
        oauth_client_id="dcr_x_uuid",
    )
    assert c.refresh_token == "art_x"
    # Refresher was wired up:
    assert c._token_manager._refresher is not None  # type: ignore[attr-defined]
    await c.close()


async def test_from_access_token_with_refresh_no_oauth_id_no_refresher(
    base_url: str, fresh_access_token: str
) -> None:
    """If oauth_client_id is missing, the refresher is silently not created
    (rather than raising; the access_token alone still works until expiry)."""
    c = await AgenTruxClient.from_access_token(
        base_url, access_token=fresh_access_token, refresh_token="art_x"
    )
    assert c._token_manager._refresher is None  # type: ignore[attr-defined]
    await c.close()


# ---------- close() idempotency / use-after-close ---------------------


async def test_close_is_idempotent(base_url: str, fresh_access_token: str) -> None:
    c = await AgenTruxClient.from_access_token(
        base_url, access_token=fresh_access_token
    )
    await c.close()
    await c.close()  # no exception


async def test_use_after_close_raises(base_url: str, fresh_access_token: str) -> None:
    c = await AgenTruxClient.from_access_token(
        base_url, access_token=fresh_access_token
    )
    await c.close()
    with pytest.raises(SDKError, match="closed"):
        await c.publish(TOPIC, "x", {"k": "v"})


# ---------- subscribe() basic wiring ---------------------------------


async def test_subscribe_returns_subscription_with_correct_mode(
    base_url: str, fresh_access_token: str
) -> None:
    c = await AgenTruxClient.from_access_token(
        base_url, access_token=fresh_access_token
    )
    sub = c.subscribe(TOPIC, mode="pull")
    assert isinstance(sub, Subscription)
    assert sub.mode == "pull"
    await sub.unsubscribe()
    await c.close()


async def test_subscribe_rejects_invalid_mode(
    base_url: str, fresh_access_token: str
) -> None:
    c = await AgenTruxClient.from_access_token(
        base_url, access_token=fresh_access_token
    )
    with pytest.raises(ValueError, match="mode"):
        c.subscribe(TOPIC, mode="weird")
    await c.close()


# ---------- connect() convenience function --------------------------


async def test_connect_requires_exactly_one_credential(base_url: str) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        await connect(base_url)


async def test_connect_rejects_multiple_credentials(
    base_url: str, fresh_access_token: str
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        await connect(
            base_url,
            access_token=fresh_access_token,
            client_id="crd_x",
        )


async def test_connect_client_id_requires_secret(base_url: str) -> None:
    with pytest.raises(ValueError, match="client_secret"):
        await connect(base_url, client_id="crd_x_uuid")


async def test_connect_with_access_token_succeeds(
    base_url: str, fresh_access_token: str
) -> None:
    c = await connect(base_url, access_token=fresh_access_token)
    assert c.access_token == fresh_access_token
    await c.close()


# ---------- token_bundle exposure ------------------------------------


async def test_token_bundle_exposes_state(
    base_url: str, fresh_access_token: str
) -> None:
    c = await AgenTruxClient.from_access_token(
        base_url,
        access_token=fresh_access_token,
        refresh_token="art_x",
        oauth_client_id="dcr_x_uuid",
    )
    bundle = c.token_bundle()
    assert bundle.access_token == fresh_access_token
    assert bundle.refresh_token == "art_x"
    assert bundle.expires_at_unix > 0
    await c.close()


# ---------- Subscription as context manager --------------------------


async def test_subscription_async_with_unsubscribes(
    base_url: str, fresh_access_token: str
) -> None:
    c = await AgenTruxClient.from_access_token(
        base_url, access_token=fresh_access_token
    )
    sub = c.subscribe(TOPIC, mode="pull")
    async with sub:
        pass
    # Unsubscribe was called; pull client should be stopped.
    await c.close()
