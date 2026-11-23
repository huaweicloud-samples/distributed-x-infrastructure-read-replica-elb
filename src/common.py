"""
Cloud-agnostic helpers: DNS resolution and the IP set arithmetic that drives
the converging state machine.

The set-arithmetic functions below (``get_pending_registration_ip_set``,
``get_invocation_count_per_pending_deregistration_ip``,
``get_pending_deregistration_ip_set``) implement the aggressive-registration /
cautious-deregistration logic that drives the converging state machine.
"""

import logging
import socket
from typing import Dict, List, Optional, Set

# The stdlib resolver does not expose a per-nameserver timeout, so only the
# overall lifetime is meaningful here; the function timeout provides the real
# upper bound.
DNS_RESOLVER_TIMEOUT = 1
DNS_RESOLVER_LIFETIME = 10

# DNS record type constant
DNS_RECORD_TYPE_A = "A"

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def precondition(pre_condition: object, error_message: str) -> None:
    """
    Raise ValueError when pre-condition is False
    :param pre_condition: pre-condition statement
    :param error_message: error message passed to the exception
    """
    if not pre_condition:
        logger.error(f"Pre-condition failed: {error_message}")
        raise ValueError(error_message)


def dns_lookup(domain_name: str, record_type: str) -> List[str]:
    """
    Get DNS lookup results using the system default DNS resolver.

    实现说明
    --------
    ``dns.resolver.Resolver`` from dnspython is not available in the
    FunctionGraph Python runtime, and vendoring it would add ~2 MB to the
    deployment package that Terraform carries base64-encoded in state.
    ``socket.getaddrinfo`` is used instead: it returns every A record bound to
    the name, which is all this workload needs, since each RDS read replica
    endpoint carries exactly one A record.

    The stdlib resolver lacks dnspython's ``rotate`` (irrelevant, the caller
    aggregates into a set) and the per-nameserver timeout (the function timeout
    bounds it).

    Also note that on Huawei Cloud an RDS private domain name only resolves
    from inside its VPC, so this code path requires the function to be attached
    to the VPC. See ``docs/how-it-works.md`` for why ``rds_api`` is the default.

    :param domain_name: DNS name to resolve
    :param record_type: DNS record type - only "A" is supported
    :return: list of resolved IPv4 addresses
    :raises socket.gaierror: when the name cannot be resolved
    """
    if record_type != DNS_RECORD_TYPE_A:
        raise ValueError(
            f"The stdlib resolver only supports {DNS_RECORD_TYPE_A} records, "
            f"got '{record_type}'"
        )

    logger.info(f"Resolving query {domain_name} (type: {record_type})")

    # Let exceptions propagate to caller for retry logic
    addr_info = socket.getaddrinfo(
        domain_name, None, socket.AF_INET, socket.SOCK_STREAM
    )
    lookup_result_list = sorted({item[4][0] for item in addr_info})

    logger.debug(
        f"DNS lookup returned {len(lookup_result_list)} result(s): {lookup_result_list}"
    )
    return lookup_result_list


def dns_lookup_with_retry(
    domain_name: str, record_type: str, total_retry_count: int
) -> Set[str]:
    """
    Get DNS lookup results with retry.

    :param domain_name: DNS name to resolve
    :param record_type: DNS record type (e.g. "A")
    :param total_retry_count: Maximum number of lookup attempts on failure
    :return: Set of resolved IP addresses (typically 1 IP for RDS replicas)
    """
    dns_lookup_result_set: Set[str] = set()
    attempt = 1

    while attempt <= total_retry_count:
        try:
            logger.info(
                f"DNS lookup attempt {attempt}/{total_retry_count} for {domain_name}"
            )
            lookup_result_per_attempt = dns_lookup(domain_name, record_type)

            if lookup_result_per_attempt:
                dns_lookup_result_set.update(lookup_result_per_attempt)
                logger.debug(
                    f"DNS lookup succeeded on attempt {attempt}. "
                    f"Total IPs resolved: {len(dns_lookup_result_set)}. "
                    f"IPs: {dns_lookup_result_set}"
                )
                # Success - no need to retry for 云数据库 (stable IPs)
                break
            else:
                logger.warning(f"DNS lookup returned empty result on attempt {attempt}")

        except Exception as e:
            logger.error(
                f"DNS lookup failed on attempt {attempt}/{total_retry_count} "
                f"for {domain_name}: {type(e).__name__}: {e}"
            )

            if attempt >= total_retry_count:
                logger.error(
                    f"All {total_retry_count} DNS lookup attempts failed for {domain_name}. "
                    f"Returning empty result."
                )
                # Return empty set after all retries exhausted
                return dns_lookup_result_set

        attempt += 1

    return dns_lookup_result_set


def get_rds_replica_ips_from_dns(
    dns_name_list: List[str], record_type: str, max_lookup_per_invocation: int
) -> Set[str]:
    """
    Resolve multiple RDS replica DNS names and aggregate all IPs.

    :param dns_name_list: List of RDS replica DNS names
    :param record_type: DNS record type (typically "A")
    :param max_lookup_per_invocation: Max DNS lookup retries per name
    :return: Set of all resolved IP addresses across all replicas
    """
    aggregated_ips: Set[str] = set()

    for dns_name in dns_name_list:
        try:
            logger.info(f"Resolving RDS DNS name: {dns_name}")
            ips = dns_lookup_with_retry(
                dns_name, record_type, max_lookup_per_invocation
            )
            if ips:
                logger.debug(f"Resolved {len(ips)} IP(s) for {dns_name}: {ips}")
                aggregated_ips.update(ips)
            else:
                logger.warning(f"No IPs resolved for DNS name: {dns_name}")
        except Exception as e:
            logger.error(f"Failed to resolve DNS name {dns_name}: {e}")
            continue

    logger.info(f"Total aggregated IPs from all DNS names: {len(aggregated_ips)}")
    return aggregated_ips


def get_pending_registration_ip_set(
    ip_from_discovery_set: Set[str], ip_from_pool_set: Set[str]
) -> Set[str]:
    """
    # Get a set of IPs that are pending for registration:
    # Pending registration IPs that meet all the following conditions:
    # 1. IPs that are currently reported by discovery
    # 2. Those IPs must have not been registered yet
    :param ip_from_pool_set: a set of IPs currently registered with the backend server group
    :param ip_from_discovery_set: a set of IPs currently reported by discovery
    """
    pending_registration_ip_set = ip_from_discovery_set - ip_from_pool_set
    return pending_registration_ip_set


def get_invocation_count_per_pending_deregistration_ip(
    ip_from_discovery_set: Set[str],
    ip_from_pool_set: Set[str],
    active_ip_set_from_previous_invocation: Set[str],
    pending_ip_dict_from_previous_invocation: Dict[str, int],
) -> Dict[str, int]:
    """
    Get a mapping of pending deregistration IP and the count of invocations that this IP has been detected
    :param ip_from_discovery_set: a set of IPs currently reported by discovery
    :param ip_from_pool_set: a set of IPs currently registered with the backend server group
    :param active_ip_set_from_previous_invocation: a set of active IPs from the previous invocation
    :param pending_ip_dict_from_previous_invocation: a dict of pending IPs and their invocation count
    :return: mapping of pending deregistration IPs and the invocation count that the IPs have been detected. e.g.
    {'172.16.2.245': 1, '172.16.3.178': 1}
    """
    # Raw pending registration IPs are the ones that: (Without considering INVOCATIONS_BEFORE_DEREGISTRATION)
    # 1. In the active IP list from the previous invocation but no longer reported by discovery
    # 2. Currently registered but no longer reported by discovery

    pending_ip_from_previous_invocation_set = (
        set(pending_ip_dict_from_previous_invocation.keys())
        if pending_ip_dict_from_previous_invocation
        else set()
    )

    # IPs that are in the active list from the previous invocation while no longer discovered
    ip_in_previous_active_ip_not_in_discovery = (
        active_ip_set_from_previous_invocation - ip_from_discovery_set
    )
    logger.debug(
        f"IPs are previously active but no longer discovered: "
        f"{ip_in_previous_active_ip_not_in_discovery}"
    )

    # IPs that are currently registered but no longer discovered
    ip_in_pool_not_in_discovery = ip_from_pool_set - ip_from_discovery_set
    logger.debug(
        f"IPs are currently in the backend server group but no longer discovered: "
        f"{ip_in_pool_not_in_discovery}"
    )

    # We keep tracking for how many invocations a pending deregistration IP has been detected.
    # The deregistration API is only called when the pending IP's invocation count is higher
    # than INVOCATIONS_BEFORE_DEREGISTRATION
    invocation_count_per_pending_deregistration_ip: Dict[str, int] = {}
    pending_ip_from_current_invocation_set = (
        ip_in_previous_active_ip_not_in_discovery | ip_in_pool_not_in_discovery
    )
    logger.debug(
        f"Pending deregistration IPs from current invocation (without considering "
        f"INVOCATIONS_BEFORE_DEREGISTRATION) - {pending_ip_from_current_invocation_set}"
    )

    if pending_ip_from_previous_invocation_set:
        new_pending_ip_set = (
            pending_ip_from_current_invocation_set
            - pending_ip_from_previous_invocation_set
        )
        logger.debug(
            f"IPs that are detected as pending deregistration for the first time - {new_pending_ip_set}"
        )
        existing_pending_ip_set = (
            pending_ip_from_current_invocation_set - new_pending_ip_set
        )
        logger.debug(
            f"IPs that have already been detected as pending deregistration from previous "
            f"invocation - {existing_pending_ip_set}"
        )
        invalid_pending_ip_set = (
            pending_ip_from_previous_invocation_set
            - pending_ip_from_current_invocation_set
        )
        logger.debug(
            f"IPs that were detected as pending deregistration but no longer considered as "
            f"pending - {invalid_pending_ip_set}"
        )

        # Set the new pending IP invocation count to 1
        for new_pending_ip in new_pending_ip_set:
            invocation_count_per_pending_deregistration_ip[new_pending_ip] = 1

        # Increase the invocation count for the IPs that are already in the previous pending IP list
        for existing_pending_ip in existing_pending_ip_set:
            invocation_count_per_pending_deregistration_ip[existing_pending_ip] = (
                pending_ip_dict_from_previous_invocation[existing_pending_ip] + 1
            )

        return invocation_count_per_pending_deregistration_ip

    logger.info("No pending deregistration IP found from the previous invocations")
    for pending_ip in pending_ip_from_current_invocation_set:
        invocation_count_per_pending_deregistration_ip[pending_ip] = 1

    return invocation_count_per_pending_deregistration_ip


def get_pending_deregistration_ip_set(
    invocation_count_per_pending_deregistration_ip: Dict[str, int],
    invocation_before_deregistration: int,
) -> Set[str]:
    """
    Get a set of IPs that are pending deregistration
    :param invocation_before_deregistration: invocation count that has to be reached first
           before calling the deregistration API
    :param invocation_count_per_pending_deregistration_ip: mapping of pending deregistration IPs
           and the invocation count that the IPs have been detected. e.g. {'172.16.2.245': 1}
    :return: a set of IPs that are pending deregistration. e.g. {'1.1.1.1', '2.2.2.2'}
    """
    pending_deregistration_ip_set: Set[str] = set()
    for ip, invocation_count in invocation_count_per_pending_deregistration_ip.items():
        if invocation_count >= invocation_before_deregistration:
            pending_deregistration_ip_set.add(ip)
    logger.debug(
        f"Pending deregistration IPs for the current invocation - {pending_deregistration_ip_set}"
    )
    return pending_deregistration_ip_set


def build_member_add_list(
    ip_list: List[str],
    protocol_port: int,
    subnet_cidr_id: Optional[str] = None,
    name_prefix: str = "rds-rr",
) -> List[Dict[str, object]]:
    """
    Build the request body for ELB BatchCreateMembers.

    The registration direction is governed by a single ``subnet_cidr_id``
    input:

    * When the backend lives inside the load balancer's own VPC, pass the
      backend subnet ID via ``SUBNET_CIDR_ID`` so the load balancer resolves
      it locally.
    * When the backend is outside the load balancer's VPC, leave
      ``subnet_cidr_id`` empty - an IP-type / cross-VPC backend requires no
      subnet ID, and the load balancer must have ``cross_vpc_backend = true``.

    :param ip_list: list of IPs to register
    :param protocol_port: the port the backend listens on
    :param subnet_cidr_id: backend subnet ID, or empty/None for a cross-VPC backend
    :param name_prefix: prefix used to name members, for operator traceability
    :return: list of member objects accepted by the batch-add API
    """
    member_list: List[Dict[str, object]] = []
    for ip in sorted(ip_list):
        member: Dict[str, object] = {
            "address": ip,
            "protocol_port": protocol_port,
            "weight": 1,
            "name": f"{name_prefix}-{ip.replace('.', '-')}",
        }
        # 注意：华为云 IP 类型后端服务器组（type="ip"）注册成员时
        # 不支持 subnet_cidr_id 字段（ELB.8902）。IP 池按 IP 直接路由，
        # 子网由 ELB 的 cross_vpc_backend 属性在负载均衡器层面控制，
        # 成员级无需也不能带 subnet_cidr_id，故此处恒不注入。
        member_list.append(member)
    return member_list


def build_member_delete_list(
    ip_list: List[str], protocol_port: int
) -> List[Dict[str, object]]:
    """
    Build the request body for ELB BatchDeleteMembers.

    The batch-delete API accepts either ``id`` alone or ``address`` plus
    ``protocol_port``, and rejects the two forms mixed together, so no other
    field may be present here. Members are addressed by IP and port rather
    than by the member ID that ELB generates.

    :param ip_list: list of IPs to deregister
    :param protocol_port: the port the backend listens on
    :return: list of member selectors accepted by the batch-delete API
    """
    return [
        {"address": ip, "protocol_port": protocol_port} for ip in sorted(ip_list)
    ]
