"""SDK 設定 (immutable dataclass).

SSOT: docs/04_design/sdk/sdk_design.md §1 設計原則 (3) 明示的 dependency
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SDKConfig:
    """SDK の起動時設定. constructor で 1 度だけ生成、 in-process で immutable.

    fields:
      endpoint: AgenTrux server base URL (例: "https://api.agentrux.io")
      client_id: script_credential.client_id ("crd_<uuid>")
      client_secret: script_credential.client_secret ("aks_<base64>")
      connect_timeout: connection 確立 timeout (seconds、 default 5)
      read_timeout: response 読み込み timeout (seconds、 default 30)
      refresh_lead_seconds: access_token の expires_at - now <= 本値 で先行再 issue (default 60)
      max_retries: TemporaryError / RateLimitError の最大 retry 回数 (default 3)
      retry_base_seconds: exponential backoff の base (default 0.5)
      user_agent: HTTP User-Agent header (default "agentrux-sdk/0.1.0")
    """

    endpoint: str
    client_id: str
    client_secret: str
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    refresh_lead_seconds: int = 60
    max_retries: int = 3
    retry_base_seconds: float = 0.5
    user_agent: str = "agentrux-sdk/0.1.0"

    def __post_init__(self) -> None:
        from agentrux.sdk.errors import ConfigError

        if not self.endpoint or not self.endpoint.startswith(("http://", "https://")):
            raise ConfigError(f"endpoint must be http(s) URL: {self.endpoint!r}")
        if not self.client_id.startswith("crd_"):
            raise ConfigError(f"client_id must start with 'crd_': {self.client_id!r}")
        if not self.client_secret.startswith("aks_"):
            raise ConfigError("client_secret must start with 'aks_'")
        if self.refresh_lead_seconds < 0:
            raise ConfigError("refresh_lead_seconds must be >= 0")
        if self.max_retries < 0:
            raise ConfigError("max_retries must be >= 0")
