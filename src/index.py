"""
This function discovers RDS read replica IP addresses and populates an ELB
backend server group (IP type) with them.

A seven-step state machine keeps the backend server group in sync with the
replica set, using aggressive registration, cautious deregistration and a
mass-deregistration circuit breaker.

WARNING: This function performs multiple discovery calls per invocation. It is
not guaranteed that all RDS read replica IPs will be detected by a single
invocation. However, the result converges when more invocations are triggered.
This function performs registration aggressively and deregistration cautiously.

Configure these environment variables on the function (Terraform inputs)
1.  DISCOVERY_MODE - "rds_api" (default) or "dns"
2.  RDS_REPLICA_IDS - Comma-separated RDS read replica instance IDs (rds_api mode)
3.  RDS_REPLICA_DNS_NAMES - Comma-separated replica DNS names (dns mode)
4.  RDS_LISTENER_PORT - The traffic listener port of the RDS instances
5.  OBS_BUCKET - Bucket to track changes between invocations
6.  ELB_POOL_ID - The ID of the ELB backend server group
7.  MAX_LOOKUP_PER_INVOCATION - The max times of DNS lookup per invocation
8.  INVOCATIONS_BEFORE_DEREGISTRATION - The number of required invocations before
    an IP is deregistered
9.  STATE_PREFIX - OBS key prefix for state files
10. MAX_DEREGISTRATION_PERCENT - Safety threshold for mass deregistration (default: 50)
11. REGION - The region the resources live in
12. SUBNET_CIDR_ID - Backend subnet ID; leave empty for a cross-VPC backend
"""

import json
import json as json_module
import logging
import sys
from typing import Any, Dict, Optional, Set, Tuple

from constant import FunctionEnv
from discovery import get_replica_ip_set
from hw_services import HuaweiCloudServices
from common import (
    logger,
    precondition,
    get_pending_registration_ip_set,
    get_invocation_count_per_pending_deregistration_ip,
    get_pending_deregistration_ip_set,
    build_member_add_list,
    build_member_delete_list,
)


def _configure_logging() -> None:
    """
    Send the module loggers to stdout, which FunctionGraph forwards to LTS.

    The FunctionGraph runtime captures stdout into LTS, so the root logger is
    wired to an explicit stdout handler here. ``context.getLogger()`` is
    deliberately not used: it would require threading the context through
    every module and would break the ``from common import logger`` idiom.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def validate_environment_variable() -> None:
    """
    Validate required environment variables have valid values.
    """
    error_message = "MAX_LOOKUP_PER_INVOCATION is required to be a positive number"
    precondition(FunctionEnv.MAX_LOOKUP_PER_INVOCATION > 0, error_message)

    error_message = (
        "INVOCATIONS_BEFORE_DEREGISTRATION is required to be a positive number"
    )
    precondition(FunctionEnv.INVOCATIONS_BEFORE_DEREGISTRATION > 0, error_message)


def get_ip_from_discovery(hw_service: HuaweiCloudServices) -> Optional[Set[str]]:
    """
    Get RDS read replica node IP addresses. Exit if no IP was discovered.

    :param hw_service: Huawei Cloud service object
    :return: a set of RDS read replica node IP addresses, or None if no IPs found
    """
    ip_from_discovery_set = get_replica_ip_set(FunctionEnv, hw_service)
    logger.debug(
        f"RDS replica IPs from discovery: {ip_from_discovery_set}. "
        f"Total IP count: {len(ip_from_discovery_set)}"
    )

    if not ip_from_discovery_set:
        source = (
            FunctionEnv.RDS_REPLICA_IDS
            if FunctionEnv.DISCOVERY_MODE == "rds_api"
            else FunctionEnv.RDS_REPLICA_DNS_NAMES
        )
        logger.error(
            f"No IP discovered for RDS replicas - {source}. "
            f"Skipping backend server group changes"
        )
        return None

    return ip_from_discovery_set


def get_ip_from_previous_invocation(
    hw_service: HuaweiCloudServices,
) -> Tuple[Dict[str, Any], Dict[str, int], Set[str]]:
    """
    Get active and pending IP from OBS. The IPs were collected from the previous
    invocation.

    :param hw_service: Huawei Cloud service object
    :return: tuple of (active_ip_dict, pending_ip_dict, active_ip_set)
    """
    active_ip_dict_from_previous_invocation = hw_service.download_elb_ip_from_obs(
        FunctionEnv.ACTIVE_IP_LIST_KEY
    )
    pending_ip_dict_from_previous_invocation = hw_service.download_elb_ip_from_obs(
        FunctionEnv.PENDING_IP_LIST_KEY
    )

    logger.debug(
        f"Active IPs from previous invocation: {active_ip_dict_from_previous_invocation}"
    )
    logger.debug(
        f"Pending IPs from previous invocation: {pending_ip_dict_from_previous_invocation}"
    )
    active_ip_set_from_previous_invocation = set(
        active_ip_dict_from_previous_invocation.get("IPList", [])
    )
    return (
        active_ip_dict_from_previous_invocation,
        pending_ip_dict_from_previous_invocation,
        active_ip_set_from_previous_invocation,
    )


def should_skip_deregistration(
    pending_deregistration_count: int,
    registered_target_count: int,
    max_deregistration_percent: int,
) -> bool:
    """
    Safety check: skip deregistration if it would remove too many members.
    Returns True if deregistration should be skipped.

    :param pending_deregistration_count: number of IPs pending deregistration
    :param registered_target_count: number of currently registered members
    :param max_deregistration_percent: maximum allowed deregistration percentage
    :return: True if deregistration should be skipped
    """
    if registered_target_count == 0:
        return False
    deregistration_percent = (
        pending_deregistration_count / registered_target_count
    ) * 100
    return deregistration_percent > max_deregistration_percent


def update_backend_server_group(
    pending_registration_ip_set: Set[str],
    pending_deregistration_ip_set: Set[str],
    hw_service: HuaweiCloudServices,
    registered_target_count: int,
) -> Tuple[bool, bool]:
    """
    Update the backend server group by registering new active IPs and
    deregistering pending IPs whose invocation count is higher than
    INVOCATIONS_BEFORE_DEREGISTRATION.

    :param pending_registration_ip_set: a set of IPs that are pending registration
    :param pending_deregistration_ip_set: a set of IPs that are pending deregistration
    :param hw_service: Huawei Cloud service object
    :param registered_target_count: number of currently registered members
    :return: tuple of (is_registered, skipped_deregistration)
    """
    is_registered = False
    skipped_deregistration = False

    if pending_registration_ip_set:
        pending_registration_member_list = build_member_add_list(
            list(pending_registration_ip_set),
            FunctionEnv.RDS_LISTENER_PORT,
            FunctionEnv.SUBNET_CIDR_ID,
        )
        is_registered = hw_service.register_target(
            FunctionEnv.ELB_POOL_ID, pending_registration_member_list
        )
    else:
        logger.info(
            "No pending registration IP found. Skipping ELB member registration..."
        )

    # Deregister members with circuit breaker check
    if pending_deregistration_ip_set:
        if should_skip_deregistration(
            len(pending_deregistration_ip_set),
            registered_target_count,
            FunctionEnv.MAX_DEREGISTRATION_PERCENT,
        ):
            logger.warning(
                f"Circuit breaker triggered: pending deregistration count "
                f"({len(pending_deregistration_ip_set)}) exceeds "
                f"{FunctionEnv.MAX_DEREGISTRATION_PERCENT}% of registered members "
                f"({registered_target_count}). Skipping deregistration."
            )
            skipped_deregistration = True
        else:
            pending_deregistration_member_list = build_member_delete_list(
                list(pending_deregistration_ip_set),
                FunctionEnv.RDS_LISTENER_PORT,
            )
            hw_service.deregister_target(
                FunctionEnv.ELB_POOL_ID, pending_deregistration_member_list
            )
    else:
        logger.info(
            "No pending deregistration IP found. Skipping ELB member deregistration..."
        )

    return is_registered, skipped_deregistration


def log_invocation_summary(
    request_id: str,
    discovered_ip_count: int,
    registered_count: int,
    deregistered_count: int,
    skipped_deregistration: bool,
) -> None:
    """
    Emit structured JSON log entry summarizing the invocation.

    :param request_id: request ID for correlation
    :param discovered_ip_count: number of IPs reported by discovery
    :param registered_count: number of IPs registered in this invocation
    :param deregistered_count: number of IPs deregistered in this invocation
    :param skipped_deregistration: whether deregistration was skipped by the circuit breaker
    """
    summary = {
        "event": "invocation_summary",
        "request_id": request_id,
        "discovery_mode": FunctionEnv.DISCOVERY_MODE,
        "discovered_ips": discovered_ip_count,
        "registered": registered_count,
        "deregistered": deregistered_count,
        "deregistration_skipped": skipped_deregistration,
    }
    logger.info(json_module.dumps(summary))


def handler(event: Any, context: Any) -> None:
    """
    Main FunctionGraph handler.
    This is invoked by the TIMER trigger.
    """
    _configure_logging()

    # Bind the runtime context so that configuration can also be read through
    # context.getUserData(), then validate it
    FunctionEnv.bind(context)
    validate_environment_variable()

    # Initialize Huawei Cloud service clients
    hw_service = HuaweiCloudServices(
        region=FunctionEnv.REGION,
        bucket=FunctionEnv.OBS_BUCKET,
        context=context,
        project_id=FunctionEnv.PROJECT_ID,
        cloud_domain=FunctionEnv.CLOUD_DOMAIN,
    )

    # ---- Step 1 -----
    # Discover the current replica IPs
    logger.info(
        f"\n>>>>Step-1: Discover replica IPs (mode: {FunctionEnv.DISCOVERY_MODE})<<<<"
    )
    ip_from_discovery_set = get_ip_from_discovery(hw_service)

    if ip_from_discovery_set is None:
        return None

    # ---- Step 2 -----
    # Get IPs that are currently registered with the ELB backend server group
    logger.info("\n>>>>Step-2: Get IPs from the backend server group<<<<")
    ip_from_pool_set = set(
        hw_service.get_ip_target_list_by_pool_id(FunctionEnv.ELB_POOL_ID)
    )
    logger.debug(
        f"ELB IPs from backend server group ({FunctionEnv.ELB_POOL_ID}): "
        f"{ip_from_pool_set}. Total IP count: {len(ip_from_pool_set)}"
    )

    active_ip_from_discovery_meta_data = {
        "LoadBalancerName": FunctionEnv.STATE_PREFIX,
        "TimeStamp": FunctionEnv.TIME,
        "IPList": list(ip_from_discovery_set),
        "IPCount": len(ip_from_discovery_set),
    }
    logger.debug(
        f"Meta data of active IPs discovered in the current invocation: "
        f"{active_ip_from_discovery_meta_data}"
    )

    # ---- Step 3 -----
    # Get the active and pending IPs that were collected from the previous invocation
    logger.info(
        "\n>>>>Step-3: Get active and pending IPs from OBS (previous invocation)<<<<"
    )
    (
        active_ip_dict_from_previous_invocation,
        pending_ip_dict_from_previous_invocation,
        active_ip_set_from_previous_invocation,
    ) = get_ip_from_previous_invocation(hw_service)

    # ---- Step 4 -----
    # Get IPs that are pending for registration
    logger.info("\n>>>>Step-4: Get IPs that are pending for registration<<<<")
    pending_registration_ip_set = get_pending_registration_ip_set(
        ip_from_discovery_set, ip_from_pool_set
    )
    logger.debug(
        f"Pending registration IPs for the current invocation - {pending_registration_ip_set}"
    )

    # ---- Step 5 -----
    # Get IPs that are pending for deregistration and their invocation count
    logger.info(
        "\n>>>>Step-5: Get IPs that are pending for deregistration and their invocation count<<<<"
    )
    invocation_count_per_pending_deregistration_ip = (
        get_invocation_count_per_pending_deregistration_ip(
            ip_from_discovery_set,
            ip_from_pool_set,
            active_ip_set_from_previous_invocation,
            pending_ip_dict_from_previous_invocation,
        )
    )
    pending_deregistration_ip_set = get_pending_deregistration_ip_set(
        invocation_count_per_pending_deregistration_ip,
        FunctionEnv.INVOCATIONS_BEFORE_DEREGISTRATION,
    )

    # ---- Step 6 -----
    # Update IP members in the backend server group (registration and deregistration)
    logger.info(
        "\n>>>>Step-6: Update IP members in the backend server group "
        "(registration and deregistration)<<<<"
    )
    logger.info(
        f"SUBNET_CIDR_ID is set to: '{FunctionEnv.SUBNET_CIDR_ID}' "
        f"(empty means cross-VPC backend)"
    )
    is_registered, skipped_deregistration = update_backend_server_group(
        pending_registration_ip_set,
        pending_deregistration_ip_set,
        hw_service,
        len(ip_from_pool_set),
    )

    # ---- Step 7 -----
    # Upload the active and pending IPs from the current invocation to OBS
    logger.info(
        "\n>>>>Step-7: Upload the active and pending IP from the current invocation to OBS<<<<"
    )
    # Only upload the current active IP to OBS when the registration API succeeded.
    # The next invocation will skip the IPs that have already been registered
    if is_registered:
        logger.debug(f"Upload active IP to OBS: {active_ip_from_discovery_meta_data}")
        hw_service.write_content_to_obs(
            json.dumps(active_ip_from_discovery_meta_data),
            FunctionEnv.ACTIVE_IP_LIST_KEY,
        )
    else:
        logger.info(
            f"No IPs were registered. Skip uploading active IP to OBS: "
            f"{active_ip_from_discovery_meta_data}"
        )

    logger.debug(
        f"Upload pending deregistration IP to OBS: "
        f"{invocation_count_per_pending_deregistration_ip}"
    )
    hw_service.write_content_to_obs(
        json.dumps(invocation_count_per_pending_deregistration_ip),
        FunctionEnv.PENDING_IP_LIST_KEY,
    )

    # Structured logging summary
    deregistered_count = (
        0
        if skipped_deregistration or not pending_deregistration_ip_set
        else len(pending_deregistration_ip_set)
    )
    log_invocation_summary(
        request_id=context.getRequestID(),
        discovered_ip_count=len(ip_from_discovery_set),
        registered_count=len(pending_registration_ip_set) if is_registered else 0,
        deregistered_count=deregistered_count,
        skipped_deregistration=skipped_deregistration,
    )
