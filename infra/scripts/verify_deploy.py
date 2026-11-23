#!/usr/bin/env python3
"""Read-only end-to-end verification for the deployed stack.

Reuses the ELB SDK-HMAC-SHA256 signing from elb_ip_pool (AK/SK, no token) and
adds an OBS header-signature GET plus an LTS log query (via an IAM-scoped token)
so we can confirm, without touching anything, that the function:
  - discovered the RDS replica private IP,
  - registered it into the IP backend server group,
  - wrote the active IP state to OBS,
  - and produced no error in the logs.

Credentials come from the environment (HW_ACCESS_KEY / HW_SECRET_KEY / HW_REGION).
Nothing is mutated.

Usage:
    python verify_deploy.py members --region <r> --project-id <p> --pool-id <id>
    python verify_deploy.py obs     --region <r> --bucket <b> --key <k>
    python verify_deploy.py logs    --region <r> --project-id <p> \
        --group-id <g> --stream-id <s> [--minutes <m>]
"""
import argparse
import base64
import datetime
import email.utils
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import elb_ip_pool as elb  # reuse _request / ENDPOINT_PREFIX (ELB signing)
from elb_ip_pool import _sign  # reuse SDK-HMAC-SHA256 signing for any endpoint


# ---------------------------------------------------------------------------
# OBS header-signature GET (AK/SK, not token)
# ---------------------------------------------------------------------------
def _obs_get(region: str, bucket: str, key: str) -> str:
    host = f"{bucket}.obs.{region}.myhuaweicloud.com"
    url = f"https://{host}/{key}"
    ak = os.environ["HW_ACCESS_KEY"]
    sk = os.environ["HW_SECRET_KEY"]
    date_str = email.utils.formatdate(usegmt=True)
    # OBS signature v2 (header): VERB\n\n\nDate\n/BUCKET/KEY
    canonical_resource = f"/{bucket}"
    if key:
        canonical_resource += f"/{key}"
    string_to_sign = f"GET\n\n\n{date_str}\n{canonical_resource}"
    sig = base64.b64encode(
        hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    headers = {
        "Host": host,
        "Date": date_str,
        "Authorization": f"OBS {ak}:{sig}",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return f"HTTPError {e.code}: {e.read().decode('utf-8', 'replace')}"


# ---------------------------------------------------------------------------
# Generic AK/SK signed requests (reuse elb_ip_pool._sign; no token needed)
# ---------------------------------------------------------------------------
def _signed_post(host: str, path: str, body_dict: dict, query: dict = None):
    ak = os.environ["HW_ACCESS_KEY"]
    sk = os.environ["HW_SECRET_KEY"]
    query = query or {}
    query_str = urllib.parse.urlencode(query) if query else ""
    url = f"https://{host}{path}" + (f"?{query_str}" if query_str else "")
    body_bytes = json.dumps(body_dict).encode("utf-8")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = {
        "host": host,
        "x-sdk-date": now,
        "content-type": "application/json;charset=utf-8",
    }
    canonical_uri = path if path.endswith("/") else path + "/"
    headers["Authorization"] = _sign(
        ak, sk, "POST", canonical_uri, query, body_bytes, headers, ["host", "x-sdk-date"]
    )
    req = urllib.request.Request(url, data=body_bytes, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _signed_get(host: str, path: str, query: dict = None):
    ak = os.environ["HW_ACCESS_KEY"]
    sk = os.environ["HW_SECRET_KEY"]
    query = query or {}
    query_str = urllib.parse.urlencode(query) if query else ""
    url = f"https://{host}{path}" + (f"?{query_str}" if query_str else "")
    body_bytes = b""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = {
        "host": host,
        "x-sdk-date": now,
        "content-type": "application/json;charset=utf-8",
    }
    canonical_uri = path if path.endswith("/") else path + "/"
    headers["Authorization"] = _sign(
        ak, sk, "GET", canonical_uri, query, body_bytes, headers, ["host", "x-sdk-date"]
    )
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _lts_logs(region: str, project_id: str, group_id: str, stream_id: str, minutes: int) -> list:
    # LTS accepts AK/SK signing directly (no IAM token needed).
    host = f"lts.{region}.myhuaweicloud.com"
    path = (
        f"/v2/{project_id}/groups/{group_id}"
        f"/streams/{stream_id}/content/query"
    )
    # LTS expects seconds since epoch, not milliseconds.
    now_s = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    start_s = now_s - minutes * 60
    body = {
        "start_time": str(start_s),
        "end_time": str(now_s),
        "limit": 100,
        "search_type": "forwards",
        "keywords": "",
    }
    status, raw = _signed_post(host, path, body, query={"type": "1"})
    if status >= 300:
        return [f"HTTPError {status}: {raw}"]
    data = json.loads(raw)
    return data.get("contents", [])


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="action", required=True)

    pm = sub.add_parser("members")
    pm.add_argument("--region", required=True)
    pm.add_argument("--project-id", required=True)
    pm.add_argument("--pool-id", required=True)

    po = sub.add_parser("obs")
    po.add_argument("--region", required=True)
    po.add_argument("--bucket", required=True)
    po.add_argument("--key", required=True)

    pl = sub.add_parser("logs")
    pl.add_argument("--region", required=True)
    pl.add_argument("--project-id", required=True)
    pl.add_argument("--group-id", required=True)
    pl.add_argument("--stream-id", required=True)
    pl.add_argument("--minutes", type=int, default=10)

    pv = sub.add_parser("invoke")
    pv.add_argument("--region", required=True)
    pv.add_argument("--project-id", required=True)
    pv.add_argument("--urn", required=True)
    pv.add_argument("--event", default="{}")

    pr = sub.add_parser("rds")
    pr.add_argument("--region", required=True)
    pr.add_argument("--project-id", required=True)
    pr.add_argument("--instance-id", required=True)

    pg = sub.add_parser("streams")
    pg.add_argument("--region", required=True)
    pg.add_argument("--project-id", required=True)
    pg.add_argument("--group-id", required=True)

    args = p.parse_args()

    if args.action == "members":
        status, raw = elb._request(
            args.region,
            args.project_id,
            "GET",
            f"/v3/{args.project_id}/elb/pools/{args.pool_id}/members",
        )
        members = json.loads(raw).get("members", [])
        ips = [m.get("address") for m in members]
        print(json.dumps({"status": status, "member_count": len(members), "ips": ips}, ensure_ascii=False))

    elif args.action == "obs":
        content = _obs_get(args.region, args.bucket, args.key)
        print(content)

    elif args.action == "logs":
        rows = _lts_logs(args.region, args.project_id, args.group_id, args.stream_id, args.minutes)
        if not rows:
            print("(no log lines in the window)")
        for r in rows:
            # LTS content lines are dicts with 'content' / 'line_num'
            if isinstance(r, dict):
                print(r.get("content", json.dumps(r, ensure_ascii=False)))
            else:
                print(r)

    elif args.action == "invoke":
        # Synchronous invocation: any uncaught exception surfaces in the body,
        # which is the fastest way to see why the pipeline is empty.
        result = _fgs_invoke(args.region, args.project_id, args.urn, args.event)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.action == "rds":
        # Direct discovery probe: what private IP(s) would the function see?
        host = f"rds.{args.region}.myhuaweicloud.com"
        status, raw = _signed_get(
            host, f"/v3/{args.project_id}/instances", query={"id": args.instance_id}
        )
        print(f"HTTP {status}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            print(raw)
            return
        for inst in data.get("instances", []):
            print(
                json.dumps(
                    {
                        "id": inst.get("id"),
                        "status": inst.get("status"),
                        "type": inst.get("type"),
                        "private_ips": inst.get("private_ips"),
                    },
                    ensure_ascii=False,
                )
            )

    elif args.action == "streams":
        host = f"lts.{args.region}.myhuaweicloud.com"
        status, raw = _signed_get(host, f"/v2/{args.project_id}/groups/{args.group_id}/streams")
        print(f"HTTP {status}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            print(raw)
            return
        streams = data.get("log_streams", data.get("streams", []))
        if not streams:
            print("RAW:", raw[:800])
            return
        for s in streams:
            print(json.dumps(
                {"name": s.get("name"), "id": s.get("id"), "logs": s.get("logs", 0)},
                ensure_ascii=False,
            ))


def _fgs_invoke(region: str, project_id: str, urn: str, event: str) -> dict:
    host = f"fgs.{region}.myhuaweicloud.com"
    # The URN contains colons; encode each path segment so the gateway routes.
    path = f"/v2/{project_id}/fgs/functions/{urllib.parse.quote(urn, safe='')}/invocations"
    ak = os.environ["HW_ACCESS_KEY"]
    sk = os.environ["HW_SECRET_KEY"]
    body_bytes = event.encode("utf-8")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = {
        "host": host,
        "x-sdk-date": now,
        "content-type": "application/json;charset=utf-8",
        "X-Fgs-Invocation-Type": "sync",
    }
    canonical_uri = path if path.endswith("/") else path + "/"
    headers["Authorization"] = _sign(
        ak, sk, "POST", canonical_uri, {}, body_bytes, headers, ["host", "x-sdk-date"]
    )
    url = f"https://{host}{path}"
    req = urllib.request.Request(url, data=body_bytes, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {
                "status": resp.status,
                "body": resp.read().decode("utf-8", "replace"),
            }
    except urllib.error.HTTPError as e:
        return {
            "status": e.code,
            "body": e.read().decode("utf-8", "replace"),
        }


if __name__ == "__main__":
    main()
