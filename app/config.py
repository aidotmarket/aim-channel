"""
AIM Data Application Configuration
=====================================

PURPOSE:
    Pydantic-Settings based configuration for the AIM Data backend.
    All settings can be overridden via environment variables (AIM_DATA_ prefix).

UPDATED:
    S94 (2026-02-07) - BQ-066 Sub-task 1: Added SECRET_KEY with Fernet
        auto-generation for API key encryption at rest.
    S130 (2026-02-13) - BQ-127: added local auth secrets and feature flags.
"""

import logging
import os
import re
from urllib.parse import urlparse

from pydantic_settings import BaseSettings
from pydantic import AliasChoices, Field, field_validator
from typing import List, Optional
from cryptography.fernet import Fernet
import psutil

logger = logging.getLogger(__name__)

_DEFAULT_AI_MARKET_URL = "https://ai-market-backend-production.up.railway.app"
_DEFAULT_DATA_VERIFICATION_PAYMENT_HANDOFF_URL = (
    "https://ai.market/dashboard/data-verification/payment-method"
)
_DATA_VERIFICATION_PAYMENT_HANDOFF_URL_ERROR = (
    "payment handoff URL must be HTTPS without credentials or use the "
    "http://localhost:13000 test origin"
)
_IAM_PRINCIPAL_ARN_RE = re.compile(r"^arn:aws:iam::\d{12}:(role|user)/.+$")


def _env_alias(field_name: str, *extra_names: str) -> AliasChoices:
    env_name = field_name.upper()
    return AliasChoices(f"AIM_DATA_{env_name}", f"VECTORAIZ_{env_name}", *extra_names)


# ---------------------------------------------------------------------------
# VZ-PERF-P1: Dynamic resource detection
# ---------------------------------------------------------------------------
def _detect_cpu_workers() -> int:
    """Auto-detect concurrent workers: max(2, min(cores // 4, 8))."""
    try:
        cores = os.cpu_count() or 4
        return max(2, min(cores // 4, 8))
    except Exception:
        return 2


def _detect_worker_memory_mb() -> int:
    """Auto-detect per-worker memory: max(2048, min(total_ram // 8, 16384))."""
    try:
        total_mb = psutil.virtual_memory().total // (1024 * 1024)
        return max(2048, min(total_mb // 8, 16384))
    except Exception:
        return 2048


def _detect_duckdb_memory_mb() -> int:
    """Auto-detect DuckDB memory budget: max(512, min(total_ram // 4, 32768))."""
    try:
        total_mb = psutil.virtual_memory().total // (1024 * 1024)
        return max(512, min(total_mb // 4, 32768))
    except Exception:
        return 512


_DETECTED_CPU_WORKERS = _detect_cpu_workers()
_DETECTED_WORKER_MEM = _detect_worker_memory_mb()
_DETECTED_DUCKDB_MEM = _detect_duckdb_memory_mb()

logger.info(
    "VZ-PERF: detected resources — workers=%d, worker_mem=%dMB, duckdb_mem=%dMB",
    _DETECTED_CPU_WORKERS, _DETECTED_WORKER_MEM, _DETECTED_DUCKDB_MEM,
)


def _generate_fernet_key() -> str:
    """Generate a Fernet-compatible key for encryption at rest.

    WARNING: Auto-generated keys are ephemeral — they change on each restart.
    In production, set AIM_DATA_SECRET_KEY env var to a persistent Fernet key.
    Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    """
    return Fernet.generate_key().decode()


class Settings(BaseSettings):
    """AIM Data settings."""

    app_name: str = Field(default="AIM Data", validation_alias=_env_alias("app_name"))
    debug: bool = Field(default=False, validation_alias=_env_alias("debug"))  # S100: Default OFF for production safety
    mode: str = Field(default="connected", validation_alias=_env_alias("mode"))

    # BQ-127: Local auth secrets (C1 — separate from SECRET_KEY)
    apikey_hmac_secret: Optional[str] = Field(
        default=None,
        validation_alias=_env_alias("apikey_hmac_secret"),
        description="HMAC for local API key hashing.",
    )
    local_auth_secret: Optional[str] = Field(default=None, validation_alias=_env_alias("local_auth_secret"))    # JWT signing key (Phase 2, not used yet)

    # Feature flags for connected ai.market capabilities.
    allai_enabled: bool = Field(
        default=True,
        validation_alias=_env_alias("allai_enabled"),
    )
    marketplace_enabled: bool = Field(default=True, validation_alias=_env_alias("marketplace_enabled"))

    # ai.market platform integration
    ai_market_url: str = Field(
        default=_DEFAULT_AI_MARKET_URL,
        validation_alias=_env_alias("ai_market_url"),
    )
    data_verification_payment_handoff_url: str = Field(
        default=_DEFAULT_DATA_VERIFICATION_PAYMENT_HANDOFF_URL,
        validation_alias=AliasChoices(
            "AIM_DATA_PAYMENT_SETUP_URL",
            "VECTORAIZ_PAYMENT_SETUP_URL",
        ),
    )
    auth_enabled: bool = Field(
        default=True,
        validation_alias=_env_alias("auth_enabled"),
        description="S100: Default ON. Set AIM_DATA_AUTH_ENABLED=false only for local dev.",
    )
    auth_cache_ttl: int = Field(default=300, validation_alias=_env_alias("auth_cache_ttl")) # 5 minutes in seconds

    # Service-to-service auth (for internal endpoints on ai-market-backend)
    internal_api_key: Optional[str] = Field(
        default=None,
        validation_alias=_env_alias("internal_api_key"),
    )
    ai_market_aws_account_id: str = Field(
        default="000000000000",
        validation_alias=_env_alias("ai_market_aws_account_id", "AI_MARKET_AWS_ACCOUNT_ID", "AIM_DATA_AWS_ACCOUNT_ID"),
        description="12-digit AWS account ID allowed to assume seller S3 roles.",
    )
    ai_market_assume_role_principal_arn: Optional[str] = Field(
        default=None,
        validation_alias=_env_alias(
            "ai_market_assume_role_principal_arn",
            "AI_MARKET_ASSUME_ROLE_PRINCIPAL_ARN",
            "AIM_DATA_AWS_PRINCIPAL_ARN",
            "VECTORAIZ_AWS_PRINCIPAL_ARN",
        ),
        description="Optional IAM role/user ARN allowed to assume seller S3 roles.",
    )
    
    # Encryption key for API keys at rest (BQ-066)
    # If not set, auto-generates a Fernet key.
    # WARNING: Auto-generated keys are ephemeral — encrypted data is lost on restart.
    # In production, always set AIM_DATA_SECRET_KEY to a persistent Fernet key.
    secret_key: Optional[str] = Field(default=None, validation_alias=_env_alias("secret_key"))

    # BQ-125: Previous SECRET_KEY for dual-decrypt during key rotation.
    # Set AIM_DATA_PREVIOUS_SECRET_KEY during transition period, remove after re-encryption.
    previous_secret_key: Optional[str] = Field(default=None, validation_alias=_env_alias("previous_secret_key"))

    # BQ-102: Device identity keystore
    # Passphrase for encrypting private keys in the local keystore.
    # REQUIRED in production — startup will fail without it.
    keystore_passphrase: Optional[str] = Field(
        default=None,
        validation_alias=_env_alias("keystore_passphrase"),
        description="Passphrase for encrypting private keys in the local keystore. REQUIRED in production. SecretStr-equivalent via env var.",
    )
    # Path to keystore file — defaults to persistent data volume for Docker.
    keystore_path: str = Field(default="/data/keystore.json", validation_alias=_env_alias("keystore_path"))

    # Co-Pilot metering (BQ-073)
    # Markup rate applied to Anthropic wholesale cost.
    # 2.0 = 200% of wholesale → e.g. $0.01 wholesale → $0.02 customer cost.
    copilot_markup_rate: float = Field(default=2.0, validation_alias=_env_alias("copilot_markup_rate"))
    # Minimum cost per query in cents (ensures even tiny queries incur a charge)
    copilot_min_cost_cents: int = Field(default=1, validation_alias=_env_alias("copilot_min_cost_cents"))
    # Estimated cost of an average Co-Pilot query in cents (for pre-flight checks)
    copilot_estimated_query_cost_cents: int = Field(default=3, validation_alias=_env_alias("copilot_estimated_query_cost_cents"))
    
    # DuckDB settings
    duckdb_threads: int = Field(default=8, validation_alias=_env_alias("duckdb_threads"))
    data_directory: str = Field(default="/data", validation_alias=_env_alias("data_directory"))
    allowed_raw_file_dirs: List[str] = Field(default_factory=list, validation_alias=_env_alias("allowed_raw_file_dirs"))
    
    # Upload settings
    upload_directory: str = Field(default="/data/uploads", validation_alias=_env_alias("upload_directory"))
    processed_directory: str = Field(default="/data/processed", validation_alias=_env_alias("processed_directory"))
    chunk_size: int = Field(default=1024 * 1024, validation_alias=_env_alias("chunk_size"))  # 1MB chunks for streaming
    raw_file_import_directory: str = Field(default="/data/import", validation_alias=_env_alias("raw_file_import_directory"))
    raw_file_upload_max_size_mb: int = Field(default=500, validation_alias=_env_alias("raw_file_upload_max_size_mb"))
    
    # Document processing (optional premium)
    unstructured_api_key: Optional[str] = Field(default=None, validation_alias=_env_alias("unstructured_api_key"))

    # Stripe billing (BQ-098)
    stripe_secret_key: Optional[str] = Field(default=None, validation_alias=_env_alias("stripe_secret_key"))
    stripe_price_id: Optional[str] = Field(default=None, validation_alias=_env_alias("stripe_price_id"))
    stripe_webhook_secret: Optional[str] = Field(default=None, validation_alias=_env_alias("stripe_webhook_secret"))
    billing_markup_rate: float = Field(default=2.0, validation_alias=_env_alias("billing_markup_rate"))
    
    # Public URL for this AIM Data instance (used in OpenAPI specs for Custom GPT Actions)
    public_url: str = Field(default="https://vectoraiz-backend-production.up.railway.app", validation_alias=_env_alias("public_url"))

    # BQ-MCP-RAG: External LLM Connectivity (§4.5)
    connectivity_enabled: bool = Field(
        default=False,
        validation_alias=_env_alias("connectivity_enabled"),
        description="Off by default - customer must opt in.",
    )
    connectivity_bind_host: str = Field(default="127.0.0.1", validation_alias=_env_alias("connectivity_bind_host"))  # Loopback only by default
    connectivity_max_tokens: int = Field(default=10, validation_alias=_env_alias("connectivity_max_tokens"))
    connectivity_rate_limit_rpm: int = Field(default=30, validation_alias=_env_alias("connectivity_rate_limit_rpm"))       # Per-token requests/min
    connectivity_rate_limit_sql_rpm: int = Field(default=10, validation_alias=_env_alias("connectivity_rate_limit_sql_rpm"))   # Per-token SQL requests/min
    connectivity_rate_limit_global_rpm: int = Field(default=120, validation_alias=_env_alias("connectivity_rate_limit_global_rpm"))
    connectivity_rate_limit_auth_fail: int = Field(default=5, validation_alias=_env_alias("connectivity_rate_limit_auth_fail"))  # Auth failures/min per IP before block
    connectivity_max_concurrent: int = Field(default=3, validation_alias=_env_alias("connectivity_max_concurrent"))        # Per-token concurrency cap
    connectivity_sql_timeout_s: int = Field(default=10, validation_alias=_env_alias("connectivity_sql_timeout_s"))
    connectivity_sql_max_rows: int = Field(default=500, validation_alias=_env_alias("connectivity_sql_max_rows"))
    connectivity_sql_memory_mb: int = Field(default=256, validation_alias=_env_alias("connectivity_sql_memory_mb"))
    connectivity_sql_max_length: int = Field(default=4096, validation_alias=_env_alias("connectivity_sql_max_length"))

    # BQ-VZ-LARGE-FILES: Streaming/chunked processing for large files
    large_file_threshold_mb: int = Field(default=50, validation_alias=_env_alias("large_file_threshold_mb"))             # Files above this use streaming path
    fallback_max_size_mb: int = Field(default=200, validation_alias=_env_alias("fallback_max_size_mb"))              # Max file size (MB) for in-memory fallback on streaming failure
    process_worker_memory_limit_mb: int = Field(default=_DETECTED_WORKER_MEM, validation_alias=_env_alias("process_worker_memory_limit_mb"))  # Per-worker memory cap (auto-detected)
    process_worker_timeout_s: int = Field(default=300, validation_alias=_env_alias("process_worker_timeout_s"))          # 5 min per file
    process_worker_grace_period_s: int = Field(default=60, validation_alias=_env_alias("process_worker_grace_period_s"))      # Seconds for checkpoint flush after SIGTERM
    process_worker_max_concurrent: int = Field(default=_DETECTED_CPU_WORKERS, validation_alias=_env_alias("process_worker_max_concurrent"))  # Max parallel workers (auto-detected)
    duckdb_memory_limit_mb: int = Field(default=_DETECTED_DUCKDB_MEM, validation_alias=_env_alias("duckdb_memory_limit_mb"))  # DuckDB in-memory budget (auto-detected)
    max_upload_size_gb: int = Field(default=1000, validation_alias=_env_alias("max_upload_size_gb"))               # Safety valve only — local app, disk is the real limit
    streaming_queue_maxsize: int = Field(default=32, validation_alias=_env_alias("streaming_queue_maxsize"))            # Backpressure queue depth
    streaming_batch_target_rows: int = Field(default=10000, validation_alias=_env_alias("streaming_batch_target_rows"))     # Target rows per RecordBatch
    parquet_row_group_size_mb: int = Field(default=64, validation_alias=_env_alias("parquet_row_group_size_mb"))           # Target row group size for ParquetWriter

    # BQ-VZ-DB-CONNECT: Database extraction limits
    db_extract_max_rows: int = Field(default=5_000_000, validation_alias=_env_alias("db_extract_max_rows"))  # Max rows per extraction (M3)

    # BQ-VZ-SERIAL-CLIENT: Serial activation & metering
    serial: Optional[str] = Field(
        default=None,
        validation_alias=_env_alias("serial"),
        description="Device serial number for X-Serial header.",
    )
    aimarket_url: str = Field(default=_DEFAULT_AI_MARKET_URL, validation_alias=_env_alias("aimarket_url"))  # ai-market serial authority base URL
    app_version: str = Field(
        default_factory=lambda: os.environ.get("AIM_DATA_VERSION") or os.environ.get("VECTORAIZ_VERSION", "dev"),
        validation_alias=_env_alias("app_version", "AIM_DATA_VERSION", "VECTORAIZ_VERSION"),
    )
    serial_data_dir: str = Field(default="/data", validation_alias=_env_alias("serial_data_dir"))  # Directory for serial.json + pending_usage.jsonl

    fts_enabled: bool = Field(default=True, validation_alias=_env_alias("fts_enabled"))

    # CORS
    cors_origins: List[str] = Field(
        default=["http://localhost:5173", "http://localhost:3000", "http://localhost:8080", "https://vectoraiz-frontend-production.up.railway.app", "https://dev.vectoraiz.com", "https://vectoraiz.com", "https://www.vectoraiz.com", "https://vectoraiz-website-production.up.railway.app"],
        validation_alias=_env_alias("cors_origins"),
    )
    
    class Config:
        env_file = ".env"
        env_prefix = "AIM_DATA_"
        extra = "ignore"

    @field_validator("ai_market_aws_account_id")
    @classmethod
    def _validate_ai_market_aws_account_id(cls, value: str) -> str:
        if not value.isdigit() or len(value) != 12:
            raise ValueError("AI_MARKET_AWS_ACCOUNT_ID must be a 12-digit string")
        return value

    @field_validator("ai_market_assume_role_principal_arn", mode="before")
    @classmethod
    def _validate_ai_market_assume_role_principal_arn(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("AI_MARKET_ASSUME_ROLE_PRINCIPAL_ARN must be an IAM role or user ARN")
        value = value.strip()
        if not value:
            return None
        if not _IAM_PRINCIPAL_ARN_RE.match(value):
            raise ValueError("AI_MARKET_ASSUME_ROLE_PRINCIPAL_ARN must be an IAM role or user ARN")
        return value

    @field_validator("ai_market_url", mode="before")
    @classmethod
    def _coalesce_blank_ai_market_url(cls, value):
        # A present-but-empty env var (e.g. a blank AIM_DATA_AI_MARKET_URL emitted
        # by the installer compose) is selected by AliasChoices and would shadow a
        # populated VECTORAIZ_AI_MARKET_URL or the default, blanking the auth base
        # URL and breaking ai.market sign-in. Treat blank/whitespace as absent.
        if value is None or (isinstance(value, str) and not value.strip()):
            return (
                (os.environ.get("AIM_DATA_AI_MARKET_URL") or "").strip()
                or (os.environ.get("VECTORAIZ_AI_MARKET_URL") or "").strip()
                or _DEFAULT_AI_MARKET_URL
            )
        return value.strip() if isinstance(value, str) else value

    @field_validator("data_verification_payment_handoff_url")
    @classmethod
    def _validate_data_verification_payment_handoff_url(cls, value: str) -> str:
        try:
            if not value or any(character.isspace() for character in value):
                raise ValueError
            parsed = urlparse(value)
            host = parsed.hostname
            parsed.port  # Validate an explicitly supplied port while parsing.
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(_DATA_VERIFICATION_PAYMENT_HANDOFF_URL_ERROR) from exc

        has_credentials = parsed.username is not None or parsed.password is not None
        valid_https = parsed.scheme == "https" and bool(host) and not has_credentials
        valid_test_http = parsed.scheme == "http" and parsed.netloc == "localhost:13000"
        if not (valid_https or valid_test_http):
            raise ValueError(_DATA_VERIFICATION_PAYMENT_HANDOFF_URL_ERROR)
        return value

    def model_post_init(self, __context) -> None:
        if not self.allowed_raw_file_dirs:
            default_raw_dir = os.environ.get("AIM_DATA_DATA_DIR") or os.environ.get("VECTORAIZ_DATA_DIR") or self.data_directory
            self.allowed_raw_file_dirs = [default_raw_dir]

    @property
    def duckdb_memory_limit(self) -> str:
        """Derive DuckDB memory limit string from duckdb_memory_limit_mb."""
        mb = self.duckdb_memory_limit_mb
        if mb >= 1024 and mb % 1024 == 0:
            return f"{mb // 1024}GB"
        return f"{mb}MB"

    def get_secret_key(self) -> str:
        """Return the SECRET_KEY, auto-generating if not set.
        
        Uses Fernet.generate_key() for auto-generation so the key is always
        valid for Fernet encryption/decryption. Logs a warning when auto-generating
        since the key won't survive restarts.
        
        Returns:
            A Fernet-compatible key string.
        """
        if self.secret_key:
            return self.secret_key
        
        # Auto-generate and cache on instance
        logger.warning(
            "SECRET_KEY not set — auto-generating ephemeral Fernet key. "
            "Encrypted data will be LOST on restart. "
            "Set AIM_DATA_SECRET_KEY in production: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
        self.secret_key = _generate_fernet_key()
        return self.secret_key


settings = Settings()

logger.info(
    "Resource detection: %d CPU cores, %.1f GB RAM -> %d workers @ %d MB, DuckDB %d MB, batch %d rows",
    os.cpu_count() or 0,
    psutil.virtual_memory().total / (1024**3),
    settings.process_worker_max_concurrent,
    settings.process_worker_memory_limit_mb,
    settings.duckdb_memory_limit_mb,
    settings.streaming_batch_target_rows,
)

logger.info("AIM Data operating mode: connected")
