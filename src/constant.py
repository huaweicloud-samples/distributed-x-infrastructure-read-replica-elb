"""
Environment configuration for the FunctionGraph function.

Configuration is read lazily on first access from environment variables (with
``context.getUserData`` as a fallback), then cached for the lifetime of the
function instance. Region is passed in explicitly via Terraform because the
FunctionGraph runtime does not inject one.

Variables
---------
``DISCOVERY_MODE``      ``rds_api`` (default) or ``dns``.
``RDS_REPLICA_IDS``     Comma-separated RDS read replica instance IDs, required
                        by ``rds_api`` mode.
``PROJECT_ID``          Optional. Resolved at runtime when left empty.
``CLOUD_DOMAIN``        Optional. ``myhuaweicloud.com`` by default; override for
                        non-Chinese-mainland regions (e.g. ``myhuaweicloud.eu``).
"""

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DISCOVERY_MODE_RDS_API = "rds_api"
DISCOVERY_MODE_DNS = "dns"
_VALID_DISCOVERY_MODES = (DISCOVERY_MODE_RDS_API, DISCOVERY_MODE_DNS)

DEFAULT_CLOUD_DOMAIN = "myhuaweicloud.com"


class _FunctionEnvMeta:
    """Lazy environment configuration. Reads env vars on first property access."""

    def __init__(self) -> None:
        self._initialized: bool = False
        self._cache: Dict[str, Any] = {}
        self._context_overlay: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Sourcing
    # ------------------------------------------------------------------
    def bind(self, context: Any) -> None:
        """
        Make ``context.getUserData()`` available as a fallback source.

        FunctionGraph exposes function environment variables both through
        ``os.environ`` and through ``context.getUserData(key)``. ``os.environ``
        is checked first so that unit tests can drive the module without a
        context object; the overlay only fills in what is missing.
        """
        self._context_overlay = {}
        if context is None or not hasattr(context, "getUserData"):
            return
        for key in _ALL_KEYS:
            if key in os.environ:
                continue
            try:
                value = context.getUserData(key)
            except Exception:  # pragma: no cover - defensive, runtime specific
                value = None
            if value is not None and value != "":
                self._context_overlay[key] = str(value)

    def _raw(self, key: str) -> Optional[str]:
        if key in os.environ:
            return os.environ[key]
        return self._context_overlay.get(key)

    def _required(self, key: str) -> str:
        value = self._raw(key)
        if value is None or value == "":
            raise KeyError(
                f"Required environment variable {key} is not set on the function"
            )
        return value

    def _optional(self, key: str, default: str) -> str:
        value = self._raw(key)
        return default if value is None or value == "" else value

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------
    def _ensure_initialized(self) -> None:
        if self._initialized:
            return

        mode = self._optional("DISCOVERY_MODE", DISCOVERY_MODE_RDS_API).strip().lower()
        if mode not in _VALID_DISCOVERY_MODES:
            raise ValueError(
                f"DISCOVERY_MODE must be one of {_VALID_DISCOVERY_MODES}, got '{mode}'"
            )
        self._cache["DISCOVERY_MODE"] = mode

        self._cache["RDS_LISTENER_PORT"] = int(self._required("RDS_LISTENER_PORT"))
        self._cache["STATE_PREFIX"] = self._required("STATE_PREFIX")
        self._cache["OBS_BUCKET"] = self._required("OBS_BUCKET")
        self._cache["ELB_POOL_ID"] = self._required("ELB_POOL_ID")
        self._cache["REGION"] = self._required("REGION")

        self._cache["MAX_LOOKUP_PER_INVOCATION"] = int(
            self._required("MAX_LOOKUP_PER_INVOCATION")
        )
        self._cache["INVOCATIONS_BEFORE_DEREGISTRATION"] = int(
            self._required("INVOCATIONS_BEFORE_DEREGISTRATION")
        )
        self._cache["MAX_DEREGISTRATION_PERCENT"] = int(
            self._optional("MAX_DEREGISTRATION_PERCENT", "50")
        )

        self._cache["SUBNET_CIDR_ID"] = self._optional("SUBNET_CIDR_ID", "")
        self._cache["PROJECT_ID"] = self._optional("PROJECT_ID", "")
        self._cache["CLOUD_DOMAIN"] = self._optional(
            "CLOUD_DOMAIN", DEFAULT_CLOUD_DOMAIN
        )
        self._cache["RDS_REQUIRE_ACTIVE_STATUS"] = (
            self._optional("RDS_REQUIRE_ACTIVE_STATUS", "false").lower() == "true"
        )

        # ---- Mode-specific inputs -------------------------------------
        if mode == DISCOVERY_MODE_RDS_API:
            replica_ids = self._required("RDS_REPLICA_IDS")
            id_list = [i.strip() for i in replica_ids.split(",") if i.strip()]
            if not id_list:
                raise ValueError(
                    "RDS_REPLICA_IDS must contain at least one read replica instance ID"
                )
            self._cache["RDS_REPLICA_IDS"] = replica_ids
            self._cache["RDS_REPLICA_ID_LIST"] = id_list
            self._cache["RDS_REPLICA_DNS_NAMES"] = self._optional(
                "RDS_REPLICA_DNS_NAMES", ""
            )
            self._cache["RDS_REPLICA_DNS_LIST"] = []
        else:
            dns_names = self._required("RDS_REPLICA_DNS_NAMES")
            dns_list = [name.strip() for name in dns_names.split(",") if name.strip()]
            if not dns_list:
                raise ValueError(
                    "RDS_REPLICA_DNS_NAMES must contain at least one valid DNS name"
                )
            self._cache["RDS_REPLICA_DNS_NAMES"] = dns_names
            self._cache["RDS_REPLICA_DNS_LIST"] = dns_list
            self._cache["RDS_REPLICA_IDS"] = self._optional("RDS_REPLICA_IDS", "")
            self._cache["RDS_REPLICA_ID_LIST"] = []

        # ---- Derived values -------------------------------------------
        prefix = self._cache["STATE_PREFIX"]
        self._cache["ACTIVE_IP_LIST_KEY"] = f"{prefix}/active_ip.json"
        self._cache["PENDING_IP_LIST_KEY"] = f"{prefix}/pending_ip.json"

        self._initialized = True

    def reset(self) -> None:
        """Reset cached state. Used in tests to re-read environment variables."""
        self._initialized = False
        self._cache.clear()
        self._context_overlay.clear()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def TIME(self) -> str:
        """Always returns current UTC time (not cached)."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @property
    def DISCOVERY_MODE(self) -> str:
        self._ensure_initialized()
        return self._cache["DISCOVERY_MODE"]

    @property
    def RDS_REPLICA_DNS_NAMES(self) -> str:
        self._ensure_initialized()
        return self._cache["RDS_REPLICA_DNS_NAMES"]

    @property
    def RDS_REPLICA_DNS_LIST(self) -> List[str]:
        self._ensure_initialized()
        return self._cache["RDS_REPLICA_DNS_LIST"]

    @property
    def RDS_REPLICA_IDS(self) -> str:
        self._ensure_initialized()
        return self._cache["RDS_REPLICA_IDS"]

    @property
    def RDS_REPLICA_ID_LIST(self) -> List[str]:
        self._ensure_initialized()
        return self._cache["RDS_REPLICA_ID_LIST"]

    @property
    def RDS_REQUIRE_ACTIVE_STATUS(self) -> bool:
        self._ensure_initialized()
        return self._cache["RDS_REQUIRE_ACTIVE_STATUS"]

    @property
    def RDS_LISTENER_PORT(self) -> int:
        self._ensure_initialized()
        return self._cache["RDS_LISTENER_PORT"]

    @property
    def STATE_PREFIX(self) -> str:
        self._ensure_initialized()
        return self._cache["STATE_PREFIX"]

    @property
    def OBS_BUCKET(self) -> str:
        self._ensure_initialized()
        return self._cache["OBS_BUCKET"]

    @property
    def ELB_POOL_ID(self) -> str:
        self._ensure_initialized()
        return self._cache["ELB_POOL_ID"]

    @property
    def MAX_LOOKUP_PER_INVOCATION(self) -> int:
        self._ensure_initialized()
        return self._cache["MAX_LOOKUP_PER_INVOCATION"]

    @property
    def INVOCATIONS_BEFORE_DEREGISTRATION(self) -> int:
        self._ensure_initialized()
        return self._cache["INVOCATIONS_BEFORE_DEREGISTRATION"]

    @property
    def MAX_DEREGISTRATION_PERCENT(self) -> int:
        self._ensure_initialized()
        return self._cache["MAX_DEREGISTRATION_PERCENT"]

    @property
    def SUBNET_CIDR_ID(self) -> str:
        self._ensure_initialized()
        return self._cache["SUBNET_CIDR_ID"]

    @property
    def REGION(self) -> str:
        self._ensure_initialized()
        return self._cache["REGION"]

    @property
    def PROJECT_ID(self) -> str:
        self._ensure_initialized()
        return self._cache["PROJECT_ID"]

    @property
    def CLOUD_DOMAIN(self) -> str:
        self._ensure_initialized()
        return self._cache["CLOUD_DOMAIN"]

    @property
    def ACTIVE_IP_LIST_KEY(self) -> str:
        self._ensure_initialized()
        return self._cache["ACTIVE_IP_LIST_KEY"]

    @property
    def PENDING_IP_LIST_KEY(self) -> str:
        self._ensure_initialized()
        return self._cache["PENDING_IP_LIST_KEY"]


_ALL_KEYS = (
    "DISCOVERY_MODE",
    "RDS_REPLICA_IDS",
    "RDS_REPLICA_DNS_NAMES",
    "RDS_REQUIRE_ACTIVE_STATUS",
    "RDS_LISTENER_PORT",
    "STATE_PREFIX",
    "OBS_BUCKET",
    "ELB_POOL_ID",
    "MAX_LOOKUP_PER_INVOCATION",
    "INVOCATIONS_BEFORE_DEREGISTRATION",
    "MAX_DEREGISTRATION_PERCENT",
    "SUBNET_CIDR_ID",
    "REGION",
    "PROJECT_ID",
    "CLOUD_DOMAIN",
)

FunctionEnv = _FunctionEnvMeta()
