"""AgenTrux Python SDK (経路 B: client_credentials).

SSOT: docs/04_design/sdk/sdk_design.md

CLAUDE.md §パッケージ公開ルール (絶対遵守):
- メインリポ (agentrux/agentrux) から PyPI に publish してはならない
- 公開は agentrux-plugins/sdk/ 経由のみ (パッケージ名 `agentrux-sdk`)

Public API (典型ユーザーが import するもの):
  from agentrux.sdk import AgentRuxClient, SDKConfig
  from agentrux.sdk.errors import (
      AgenTruxError, AuthenticationError, PermissionDeniedError,
      RateLimitError, PayloadTooLargeError, ...,
  )
"""

from __future__ import annotations

from agentrux.sdk.composer import ComposerGroup, iter_composer_groups
from agentrux.sdk.config import SDKConfig
from agentrux.sdk.device_code_setup import (
    DeviceCodeSetupPending,
    DeviceCodeSetupResult,
    setup_via_device_code,
)
from agentrux.sdk.errors import (
    AgenTruxError,
    AuthenticationError,
    ConfigError,
    ConflictError,
    CredentialRotatedError,
    GapDetectedError,
    IdempotencyConflictError,
    ObjectStorageError,
    PayloadTooLargeError,
    PermissionDeniedError,
    RateLimitError,
    ResourceNotFoundError,
    ServerError,
    TemporaryError,
    ValidationError,
)
from agentrux.sdk.facade import AgentRuxClient
from agentrux.sdk.topology_install import (
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
    "AgentRuxClient",
    "SDKConfig",
    "AgenTruxError",
    "ConfigError",
    "AuthenticationError",
    "CredentialRotatedError",
    "PermissionDeniedError",
    "ResourceNotFoundError",
    "ConflictError",
    "IdempotencyConflictError",
    "PayloadTooLargeError",
    "RateLimitError",
    "ValidationError",
    "TemporaryError",
    "ServerError",
    "ObjectStorageError",
    "GapDetectedError",
    # Topology Request Flow v1
    "install_topology",
    "TopologyDeclaration",
    "TopologyTopicSpec",
    "TopologyGrantSpec",
    "InstallPendingInfo",
    "InstallResult",
    "InstallResultGrant",
    "InstallError",
    "InstallDeniedError",
    "InstallTimeoutError",
    "InstallAuthError",
    "InstallAbortedError",
    # Plain Device Code Setup (RFC 8628、 RAR なし、 device_code_setup_v1.md)
    "setup_via_device_code",
    "DeviceCodeSetupPending",
    "DeviceCodeSetupResult",
    # Composer Event Group reader (Phase BT.1.d 部分実装、 composer_event_format.md §3-3)
    "ComposerGroup",
    "iter_composer_groups",
]
