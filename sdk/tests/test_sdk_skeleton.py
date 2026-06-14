"""SDK Phase 5.2 skeleton tests — import 可能性 + class 存在 + ConfigError 検証."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_public_api_importable() -> None:
    """from agentrux_sdk import AgentRuxClient, SDKConfig が動く."""
    from agentrux_sdk import AgentRuxClient, SDKConfig

    assert AgentRuxClient is not None
    assert SDKConfig is not None


def test_error_hierarchy() -> None:
    """全 SDK 例外が AgenTruxError を継承."""
    from agentrux_sdk.errors import (
        AgenTruxError,
        AuthenticationError,
        ConfigError,
        ConflictError,
        CredentialRotatedError,
        IdempotencyConflictError,
        ObjectStorageError,
        PayloadTooLargeError,
        PermissionDeniedError,
        RateLimitError,
        ResourceNotFoundError,
        RetentionMissError,
        ServerError,
        TemporaryError,
        ValidationError,
    )

    for exc_cls in [
        ConfigError, AuthenticationError, CredentialRotatedError,
        PermissionDeniedError, ResourceNotFoundError, ConflictError,
        IdempotencyConflictError, PayloadTooLargeError, RateLimitError,
        ValidationError, TemporaryError, ServerError, ObjectStorageError,
        RetentionMissError,
    ]:
        assert issubclass(exc_cls, AgenTruxError)
    # IdempotencyConflictError は ConflictError も継承
    assert issubclass(IdempotencyConflictError, ConflictError)
    # ServerError / ObjectStorageError は TemporaryError も継承
    assert issubclass(ServerError, TemporaryError)
    assert issubclass(ObjectStorageError, TemporaryError)


def test_sdkconfig_validates_endpoint() -> None:
    """endpoint が http(s) でないと ConfigError."""
    from agentrux_sdk import SDKConfig
    from agentrux_sdk.errors import ConfigError

    with pytest.raises(ConfigError, match="endpoint must be http"):
        SDKConfig(
            endpoint="ftp://invalid",
            client_id="crd_xxx",
            client_secret="aks_xxx",
        )


def test_sdkconfig_validates_client_id_prefix() -> None:
    """client_id は crd_ prefix 必須."""
    from agentrux_sdk import SDKConfig
    from agentrux_sdk.errors import ConfigError

    with pytest.raises(ConfigError, match="crd_"):
        SDKConfig(
            endpoint="https://api.example.com",
            client_id="invalid-id",
            client_secret="aks_xxx",
        )


def test_sdkconfig_validates_client_secret_prefix() -> None:
    """client_secret は aks_ prefix 必須 (生の secret は error message に含めない)."""
    from agentrux_sdk import SDKConfig
    from agentrux_sdk.errors import ConfigError

    with pytest.raises(ConfigError, match="aks_") as ei:
        SDKConfig(
            endpoint="https://api.example.com",
            client_id="crd_xxx",
            client_secret="invalid-secret",
        )
    # secret 自体は message に漏らさない (mask)
    assert "invalid-secret" not in str(ei.value)


def test_sdkconfig_validates_negative_lead() -> None:
    """refresh_lead_seconds < 0 は ConfigError."""
    from agentrux_sdk import SDKConfig
    from agentrux_sdk.errors import ConfigError

    with pytest.raises(ConfigError, match="refresh_lead_seconds"):
        SDKConfig(
            endpoint="https://api.example.com",
            client_id="crd_xxx",
            client_secret="aks_xxx",
            refresh_lead_seconds=-1,
        )


def test_agentrux_client_construct_and_close() -> None:
    """AgentRuxClient が SDKConfig を経由して構築できる + aclose 可能."""
    import asyncio

    from agentrux_sdk import AgentRuxClient

    async def _run() -> None:
        client = AgentRuxClient(
            endpoint="https://api.example.com",
            client_id="crd_xxx",
            client_secret="aks_xxx",
        )
        assert client.config.endpoint == "https://api.example.com"
        await client.aclose()

    asyncio.run(_run())


def test_pipeline_legacy_function_raises_notimplemented() -> None:
    """pipeline.run_pipeline (5.2 skeleton 用の関数) は backward-compat で
    NotImplementedError を維持。 本実装は Pipeline class 経由 (test_sdk_pipeline.py).
    """
    import asyncio

    from agentrux_sdk import pipeline

    async def _check() -> None:
        with pytest.raises(NotImplementedError):
            await pipeline.run_pipeline()

    asyncio.run(_check())
