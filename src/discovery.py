"""
Read replica IP discovery.

On Huawei Cloud, an RDS instance's private domain name
(``<id>.internal.<region>.mysql.rds.myhuaweicloud.com``) only resolves from
inside the VPC. Resolving DNS names alone would therefore force the function
into the VPC and add a NAT gateway so it can still reach the ELB and OBS
endpoints.

Two strategies are provided:

``rds_api`` (default)
    Ask the RDS API for the private IPs of the configured read replica
    instances. No VPC attachment, no NAT gateway, no DNS TTL caching window,
    and deleted instances disappear from the answer immediately.

``dns``
    Resolve each replica's private DNS name via socket resolution. Requires the
    function to be attached to the VPC.

Both strategies return the same thing - a set of IP strings - so everything
downstream of this module is identical between the two.
"""

from typing import Any, Set

from common import (
    DNS_RECORD_TYPE_A,
    get_rds_replica_ips_from_dns,
    logger,
)
from constant import DISCOVERY_MODE_DNS, DISCOVERY_MODE_RDS_API


def get_replica_ip_set(env: Any, hw_service: Any) -> Set[str]:
    """
    Discover the current read replica IPs using the configured strategy.

    :param env: the ``FunctionEnv`` configuration singleton
    :param hw_service: a ``HuaweiCloudServices`` instance
    :return: set of replica IP addresses, possibly empty
    """
    if env.DISCOVERY_MODE == DISCOVERY_MODE_RDS_API:
        return _get_ip_from_rds_api(env, hw_service)
    if env.DISCOVERY_MODE == DISCOVERY_MODE_DNS:
        return _get_ip_from_dns(env)
    raise ValueError(f"Unsupported discovery mode: {env.DISCOVERY_MODE}")


def _get_ip_from_rds_api(env: Any, hw_service: Any) -> Set[str]:
    """
    Query the RDS API once per configured replica instance ID.

    The loop queries each replica independently with a per-instance
    ``try``/``continue``: one unreachable replica must never hide the others,
    because a partial answer combined with cautious deregistration still
    converges, whereas an exception would skip the whole invocation.
    """
    aggregated_ips: Set[str] = set()

    for instance_id in env.RDS_REPLICA_ID_LIST:
        try:
            logger.info(f"Querying RDS read replica: {instance_id}")
            ips = hw_service.get_replica_private_ip(
                instance_id, env.RDS_REQUIRE_ACTIVE_STATUS
            )
            if ips:
                logger.debug(f"Resolved {len(ips)} IP(s) for {instance_id}: {ips}")
                aggregated_ips.update(ips)
        except Exception as e:
            logger.error(f"Failed to query RDS replica {instance_id}: {e}")
            continue

    logger.info(f"Total aggregated IPs from the RDS API: {len(aggregated_ips)}")
    return aggregated_ips


def _get_ip_from_dns(env: Any) -> Set[str]:
    """Resolve every configured replica DNS name and aggregate the A records."""
    return get_rds_replica_ips_from_dns(
        env.RDS_REPLICA_DNS_LIST,
        DNS_RECORD_TYPE_A,
        env.MAX_LOOKUP_PER_INVOCATION,
    )
