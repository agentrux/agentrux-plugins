"""AgenTrux Python SDK (経路 B: client_credentials).

SSOT: docs/04_design/sdk/sdk_design.md

CLAUDE.md §パッケージ公開ルール (絶対遵守):
- メインリポ (agentrux/agentrux) から PyPI に publish してはならない
- 公開は agentrux-plugins/sdk/ 経由のみ (パッケージ名 `agentrux-sdk`)

Public API (典型ユーザーが import するもの):
  from agentrux_sdk import AgentRuxClient, SDKConfig
  from agentrux_sdk.errors import (
      AgenTruxError, AuthenticationError, PermissionDeniedError,
      RateLimitError, PayloadTooLargeError, ...,
  )
"""

from __future__ import annotations

from agentrux_sdk.composer import ComposerGroup, iter_composer_groups
from agentrux_sdk.config import SDKConfig
from agentrux_sdk.device_code_setup import (
    DeviceCodeSetupPending,
    DeviceCodeSetupResult,
    setup_via_device_code,
)
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
from agentrux_sdk.facade import AgentRuxClient
from agentrux_sdk.topology_install import (
    InstallAbortedError,
    InstallAuthError,
    InstallDeniedError,
    InstallError,
    InstallPendingInfo,
    InstallResult,
    InstallResultGrant,
    InstallTimeoutError,
    TopologyDeclaration,
    TopologyGrantSpec,
    TopologyTopicSpec,
    install_topology,
)

__all__ = [
    "AgenTruxError",
    "AgentRuxClient",
    "AuthenticationError",
    # Composer Event Group reader (Phase BT.1.d 部分実装、 composer_event_format.md §3-3)
    "ComposerGroup",
    "ConfigError",
    "ConflictError",
    "CredentialRotatedError",
    "DeviceCodeSetupPending",
    "DeviceCodeSetupResult",
    "IdempotencyConflictError",
    "InstallAbortedError",
    "InstallAuthError",
    "InstallDeniedError",
    "InstallError",
    "InstallPendingInfo",
    "InstallResult",
    "InstallResultGrant",
    "InstallTimeoutError",
    "ObjectStorageError",
    "PayloadTooLargeError",
    "PermissionDeniedError",
    "RateLimitError",
    "ResourceNotFoundError",
    "RetentionMissError",
    "SDKConfig",
    "ServerError",
    "TemporaryError",
    "TopologyDeclaration",
    "TopologyGrantSpec",
    "TopologyTopicSpec",
    "ValidationError",
    # Topology Request Flow v1
    "install_topology",
    "iter_composer_groups",
    # Plain Device Code Setup (RFC 8628、 RAR なし、 device_code_setup_v1.md)
    "setup_via_device_code",
]
