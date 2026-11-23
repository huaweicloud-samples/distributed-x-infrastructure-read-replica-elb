"""
Huawei Cloud service access layer.

Design note: why raw REST instead of an SDK
-------------------------------------------
The FunctionGraph Python runtime ships a fixed set of non-standard libraries
(dateutil, requests, httplib2, numpy, redis, obsclient, smnsdk). The
``huaweicloudsdk*`` packages are **not** among them, so using them would mean
shipping tens of MB of dependencies inside the deployment package. Instead this
module talks to the ELB v3 and RDS v3 REST APIs directly with ``requests``,
authenticating with the agency token returned by ``context.getToken()``. OBS is
the one exception: the runtime does provide ``obsclient``, and OBS requires an
AK/SK signature rather than a token, so the SDK is used there.

All credentials come from the function's agency at runtime. Nothing is stored
in code, in environment variables or in Terraform state.
"""

import json
import time
from typing import Any, Dict, List, Optional, Set

import requests

from common import logger, precondition

# Mirrors the 云 sample's RETRY_CONFIG (max_attempts=3, adaptive mode).
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 10

# ELB returns at most 2000 items per page; stay well below and paginate.
MEMBER_PAGE_SIZE = 500
MAX_MEMBER_PAGES = 50

# The batch APIs accept up to 200 members per call.
MEMBER_BATCH_SIZE = 200

RETRYABLE_STATUS = (429, 500, 502, 503, 504)


class HuaweiCloudServicesError(RuntimeError):
    """Raised when a Huawei Cloud API call fails after all retries."""


class HuaweiCloudServices:
    """
    Provides common methods to interact with Huawei Cloud services
    (OBS, ELB v3, RDS v3).
    """

    def __init__(
        self,
        region: str,
        bucket: str,
        context: Any,
        project_id: str = "",
        cloud_domain: str = "myhuaweicloud.com",
    ) -> None:
        precondition(region, "region is required")
        precondition(bucket, "bucket is required")
        precondition(context is not None, "function context is required")

        self.region = region
        self.bucket = bucket
        self.cloud_domain = cloud_domain
        self._context = context

        self.elb_endpoint = f"https://elb.{region}.{cloud_domain}"
        self.rds_endpoint = f"https://rds.{region}.{cloud_domain}"
        self.iam_endpoint = f"https://iam.{region}.{cloud_domain}"
        self.obs_endpoint = f"https://obs.{region}.{cloud_domain}"

        self._token: Optional[str] = None
        self._obs_client: Any = None
        self.project_id = project_id or self._resolve_project_id()

    # ------------------------------------------------------------------
    # Authentication helpers
    # ------------------------------------------------------------------
    @property
    def token(self) -> str:
        """
        Agency token used for ELB/RDS calls.

        ``context.getToken()`` needs an agency configured on the function; the
        runtime caches the value for 10 minutes and it stays valid for 24 h, so
        caching it for the lifetime of this object is safe.
        """
        if self._token is None:
            token = self._context.getToken()
            if not token:
                raise HuaweiCloudServicesError(
                    "context.getToken() returned nothing. Check that an agency is "
                    "attached to the function."
                )
            self._token = token
        return self._token

    def _resolve_project_id(self) -> str:
        """
        Resolve the project ID of the current region.

        Tried in order:
        1. ``context.getProjectID()`` - available on most runtime versions.
        2. IAM ``GET /v3/auth/projects``, matching the project whose name equals
           the region. This always works with a valid agency token.

        The result is only computed once per cold start.
        """
        getter = getattr(self._context, "getProjectID", None)
        if callable(getter):
            try:
                project_id = getter()
                if project_id:
                    logger.debug(f"Project ID resolved from context: {project_id}")
                    return str(project_id)
            except Exception as e:  # pragma: no cover - runtime specific
                logger.warning(f"context.getProjectID() unavailable: {e}")

        logger.info("Falling back to IAM to resolve the project ID")
        response = self._request(
            "GET", f"{self.iam_endpoint}/v3/auth/projects", service="IAM"
        )
        for project in response.get("projects", []):
            if project.get("name") == self.region:
                logger.debug(f"Project ID resolved from IAM: {project.get('id')}")
                return str(project["id"])

        raise HuaweiCloudServicesError(
            f"Could not resolve the project ID for region {self.region}. "
            f"Set the PROJECT_ID environment variable explicitly."
        )

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        url: str,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        service: str = "API",
    ) -> Dict[str, Any]:
        """
        Issue an authenticated request with bounded retries.

        Retries on connection errors and on the transient status codes that
        Huawei Cloud uses for throttling and gateway failures. 4xx responses
        other than 429 are treated as permanent and raised immediately.
        """
        headers = {
            "X-Auth-Token": self.token,
            "Content-Type": "application/json;charset=utf8",
        }
        payload = None if body is None else json.dumps(body)

        last_error: Optional[str] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=headers,
                    data=payload,
                    params=params,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning(
                    f"{service} {method} {url} failed on attempt "
                    f"{attempt}/{MAX_ATTEMPTS}: {last_error}"
                )
                if attempt == MAX_ATTEMPTS:
                    break
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue

            if 200 <= response.status_code < 300:
                if not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError:
                    return {}

            last_error = f"HTTP {response.status_code}: {response.text[:500]}"
            if response.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                logger.warning(
                    f"{service} {method} {url} returned a retryable error on attempt "
                    f"{attempt}/{MAX_ATTEMPTS}: {last_error}"
                )
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue
            break

        raise HuaweiCloudServicesError(f"{service} {method} {url} failed: {last_error}")

    # ------------------------------------------------------------------
    # OBS - 状态读写
    # ------------------------------------------------------------------
    @property
    def obs_client(self) -> Any:
        """Lazily build the OBS client from the agency's temporary credentials."""
        if self._obs_client is None:
            try:
                from obs import ObsClient
            except ImportError as e:  # pragma: no cover - runtime specific
                raise HuaweiCloudServicesError(
                    "The obsclient library is missing. It is preinstalled in the "
                    "FunctionGraph Python runtime; install esdk-obs-python to run "
                    "this code locally."
                ) from e

            self._obs_client = ObsClient(
                access_key_id=self._context.getSecurityAccessKey(),
                secret_access_key=self._context.getSecuritySecretKey(),
                security_token=self._context.getSecurityToken(),
                server=self.obs_endpoint,
            )
        return self._obs_client

    def write_content_to_obs(self, content: str, object_key: str) -> None:
        """
        Adds an object to a bucket.
        :param content: object content
        :param object_key: OBS object key
        :raises HuaweiCloudServicesError: on OBS API failure
        """
        response = self.obs_client.putContent(self.bucket, object_key, content=content)
        if response.status >= 300:
            message = (
                f"Failed to write to obs://{self.bucket}/{object_key}: "
                f"status={response.status} code={getattr(response, 'errorCode', None)} "
                f"message={getattr(response, 'errorMessage', None)}"
            )
            logger.error(message)
            raise HuaweiCloudServicesError(message)
        logger.debug(f"Successfully wrote content to obs://{self.bucket}/{object_key}")

    def download_elb_ip_from_obs(self, object_key: str) -> Dict[str, Any]:
        """
        Download object from OBS.
        :param object_key: OBS object key
        :return: Active or pending backend IP state stored in OBS
        :raises HuaweiCloudServicesError: on OBS API failure other than 404
        :raises json.JSONDecodeError: if the state file content is not valid JSON
        """
        response = self.obs_client.getObject(
            self.bucket, object_key, loadStreamInMemory=True
        )

        if response.status == 404:
            logger.info(f"No state file at {object_key}. Expected on first invocation.")
            return {}
        if response.status >= 300:
            message = (
                f"Failed to read obs://{self.bucket}/{object_key}: "
                f"status={response.status} code={getattr(response, 'errorCode', None)} "
                f"message={getattr(response, 'errorMessage', None)}"
            )
            logger.error(message)
            raise HuaweiCloudServicesError(message)

        logger.info(f"Get {object_key} from OBS bucket - {self.bucket}")
        try:
            return json.loads(response.body.buffer)
        except json.JSONDecodeError as e:
            logger.error(f"Corrupted state file at {object_key}: {e}")
            raise

    # ------------------------------------------------------------------
    # ELB v3 - 后端服务器组成员操作
    # ------------------------------------------------------------------
    def _pool_url(self, pool_id: str, suffix: str = "") -> str:
        return (
            f"{self.elb_endpoint}/v3/{self.project_id}/elb/pools/{pool_id}/members"
            f"{suffix}"
        )

    def register_target(self, pool_id: str, new_member_list: List[Dict[str, Any]]) -> bool:
        """
        Register the given members with the given backend server group.

        Register members into the backend server group through the ELB v3
        batch-add API. Failures are logged and reported through the return
        value instead of raising, so the caller can decide not to persist the
        active IP list.

        :param pool_id: ID of the backend server group
        :param new_member_list: list of members from ``build_member_add_list``
        :return: True if all batches registered successfully, False otherwise
        """
        logger.debug(f"Register new_member_list:{new_member_list}")
        if not new_member_list:
            return False

        for batch in _chunk(new_member_list, MEMBER_BATCH_SIZE):
            try:
                self._request(
                    "POST",
                    self._pool_url(pool_id, "/batch-add"),
                    body={"members": batch},
                    service="ELB",
                )
            except HuaweiCloudServicesError as e:
                logger.error(
                    f"Failed to register members with the backend server group. "
                    f"Members: {batch}. Pool: {pool_id}. Error: {e}"
                )
                return False
        return True

    def deregister_target(
        self, pool_id: str, member_list: List[Dict[str, Any]]
    ) -> None:
        """
        Deregister the given members from the given backend server group.

        Remove members from the backend server group through the ELB v3
        batch-delete API. The batch API reports per-member results in
        ``ret_status`` (``successful`` or ``not found``); a ``not found`` is
        benign - it means another invocation already removed the member - so it
        is logged rather than raised.

        :param pool_id: ID of the backend server group
        :param member_list: list of selectors from ``build_member_delete_list``
        """
        logger.debug(f"Deregistering members: {member_list}")
        if not member_list:
            return

        for batch in _chunk(member_list, MEMBER_BATCH_SIZE):
            try:
                response = self._request(
                    "POST",
                    self._pool_url(pool_id, "/batch-delete"),
                    body={"members": batch},
                    service="ELB",
                )
            except HuaweiCloudServicesError as e:
                logger.error(
                    f"Failed to deregister members from the backend server group. "
                    f"Members: {batch}. Pool: {pool_id}. Error: {e}"
                )
                continue

            for result in response.get("members", []):
                if result.get("ret_status") != "successful":
                    logger.warning(
                        f"Member {result.get('id')} was not removed: "
                        f"ret_status={result.get('ret_status')}"
                    )

    def get_ip_target_list_by_pool_id(self, pool_id: str) -> List[str]:
        """
        Get a list of IP addresses currently registered with the given backend
        server group.

        Query the current members of the backend server group via the ELB v3
        ListMembers API. The state machine only needs membership, not health,
        so health is not consulted here.

        :param pool_id: ID of the backend server group
        :return: list of registered IP addresses
        :raises HuaweiCloudServicesError: if the ListMembers API call fails
        """
        registered_ip_list: List[str] = []
        marker: Optional[str] = None

        for _ in range(MAX_MEMBER_PAGES):
            params: Dict[str, Any] = {"limit": MEMBER_PAGE_SIZE}
            if marker:
                params["marker"] = marker

            response = self._request(
                "GET", self._pool_url(pool_id), params=params, service="ELB"
            )
            members = response.get("members", [])
            for member in members:
                address = member.get("address")
                if address:
                    registered_ip_list.append(address)

            marker = (response.get("page_info") or {}).get("next_marker")
            if not marker or len(members) < MEMBER_PAGE_SIZE:
                break
        else:
            logger.warning(
                f"Stopped paginating members of pool {pool_id} after "
                f"{MAX_MEMBER_PAGES} pages"
            )

        logger.debug(
            f"Backend IPs that are currently registered with the backend server group: "
            f"{registered_ip_list}. Total IP count: {len(registered_ip_list)}"
        )
        return registered_ip_list

    # ------------------------------------------------------------------
    # 云数据库 v3 - 只读副本 IP 的发现来源
    # ------------------------------------------------------------------
    def get_replica_private_ip(
        self, instance_id: str, require_active_status: bool = False
    ) -> Set[str]:
        """
        Look up the private IPs of a single RDS read replica instance.

        :param instance_id: RDS read replica instance ID
        :param require_active_status: when True, ignore instances whose status
               is not ACTIVE. Defaults to False so a replica stays in the set
               while it is rebooting or degraded.
        :return: set of private IPs, empty when the instance no longer exists
        """
        response = self._request(
            "GET",
            f"{self.rds_endpoint}/v3/{self.project_id}/instances",
            params={"id": instance_id},
            service="RDS",
        )

        ips: Set[str] = set()
        for instance in response.get("instances", []):
            if instance.get("id") != instance_id:
                continue
            status = instance.get("status")
            if require_active_status and status != "ACTIVE":
                logger.warning(
                    f"Skipping RDS replica {instance_id}: status is {status}, "
                    f"not ACTIVE"
                )
                continue
            for ip in instance.get("private_ips") or []:
                if ip:
                    ips.add(ip)

        if not ips:
            logger.warning(f"No private IP found for RDS replica {instance_id}")
        return ips


def _chunk(items: List[Any], size: int) -> List[List[Any]]:
    """Split a list into chunks of at most ``size`` elements."""
    return [items[i : i + size] for i in range(0, len(items), size)]
