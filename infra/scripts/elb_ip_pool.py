#!/usr/bin/env python3
"""Create / delete an Huawei Cloud ELB IP-type backend pool via the v3 REST API.

Why this exists
---------------
The huaweicloud Terraform provider pinned in this project (<= 1.96.0) does not
expose the pool attribute `ip_target_enable`, which Huawei Cloud requires for
any IP-type backend pool. The provider therefore fails with ELB.8902 when
creating a `type = "ip"` pool.

This script is the workaround: it calls the ELB v3 API directly, signing the
request with the AK/SK (SDK-HMAC-SHA256). No token exchange is needed.

Credentials come from the environment (HW_ACCESS_KEY / HW_SECRET_KEY), which
Terraform injects at apply time. Nothing sensitive is written to disk.

Usage (invoked by a terraform_data local-exec):
    python elb_ip_pool.py create       --region <r> --project-id <p> --lb-id <l> --name <n>
    python elb_ip_pool.py patch-lb     --region <r> --project-id <p> --lb-id <l>
    python elb_ip_pool.py delete       --region <r> --project-id <p> --lb-id <l> --name <n> [--pool-id <id>]

The `create` step must be preceded by `patch-lb`: Huawei Cloud requires the
load-balancer itself to carry `ip_target_enable=true` (NOT the pool) before an
IP-type pool may be bound to it. Our Terraform wrapper runs `patch-lb` first.
"""
import argparse
import datetime
import hashlib
import hmac
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT_PREFIX = "elb"  # regional endpoint, e.g. elb.cn-south-1.myhuaweicloud.com


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sign(ak: str, sk: str, method: str, url_path: str, query: dict,
          body: bytes, headers: dict, signed_headers: list) -> str:
    """Build the SDK-HMAC-SHA256 Authorization header value."""
    canonical_query = "&".join(f"{k}={query[k]}" for k in sorted(query)) if query else ""
    canonical_headers = "".join(f"{h}:{headers[h].strip()}\n" for h in sorted(signed_headers))
    signed_headers_str = ";".join(sorted(signed_headers))
    payload_hash = _sha256_hex(body)
    canonical_request = "\n".join(
        [method, url_path, canonical_query, canonical_headers, signed_headers_str, payload_hash]
    )
    t = headers["x-sdk-date"]
    string_to_sign = "\n".join(
        ["SDK-HMAC-SHA256", t, _sha256_hex(canonical_request.encode("utf-8"))]
    )
    signature = hmac.new(
        sk.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return (
        f"SDK-HMAC-SHA256 Access={ak}, "
        f"SignedHeaders={signed_headers_str}, Signature={signature}"
    )


def _request(region: str, project_id: str, method: str, path: str, query=None, body=None):
    host = f"{ENDPOINT_PREFIX}.{region}.myhuaweicloud.com"
    query = query or {}
    # 查询串单独传给签名函数——绝不能塞进 CanonicalURI，否则签名对不上。
    query_str = urllib.parse.urlencode(query) if query else ""
    url = f"https://{host}{path}" + (f"?{query_str}" if query_str else "")
    body_bytes = body if isinstance(body, (bytes, bytearray)) else (
        body.encode("utf-8") if isinstance(body, str) else b""
    )
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = {
        "host": host,
        "x-sdk-date": now,
        "content-type": "application/json;charset=utf-8",
    }
    ak = os.environ["HW_ACCESS_KEY"]
    sk = os.environ["HW_SECRET_KEY"]
    # 实际请求路径不带尾部斜杠（否则路由 404）；但 API 网关会把 URI 规范化补上
    # 斜杠再验签，因此签名用的 CanonicalURI 必须带尾部斜杠。
    canonical_uri = path if path.endswith("/") else path + "/"
    headers["Authorization"] = _sign(
        ak, sk, method, canonical_uri, query, body_bytes, headers, ["host", "x-sdk-date"]
    )
    req = urllib.request.Request(
        url, data=body_bytes or None, method=method, headers=headers
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        print(f"HTTPError {e.code}: {detail}", file=sys.stderr)
        raise


def _find_pool(region: str, project_id: str, lb_id: str, name: str):
    query = {"name": name}
    if lb_id:
        query["loadbalancer_id"] = lb_id
    status, raw = _request(region, project_id, "GET", f"/v3/{project_id}/elb/pools", query)
    for p in json.loads(raw).get("pools", []):
        if p.get("name") == name:
            return p["id"]
    return None


def create_pool(region: str, project_id: str, lb_id: str, name: str, meta_file=None):
    existing = _find_pool(region, project_id, lb_id, name)
    if existing:
        # Idempotent: reuse the pool created on a previous apply.
        pool_id = existing
    else:
        body = json.dumps({
            "pool": {
                "name": name,
                "description": (
                    "IP-type backend pool for RDS read replicas. Managed via the ELB "
                    "REST API because the pinned Terraform provider lacks ip_target_enable."
                ),
                "protocol": "TCP",
                "lb_algorithm": "ROUND_ROBIN",
                "loadbalancer_id": lb_id,
                "type": "ip",
            }
        })
        status, raw = _request(region, project_id, "POST", f"/v3/{project_id}/elb/pools", body=body)
        if status not in (200, 201):
            print(f"unexpected status {status}", file=sys.stderr)
            sys.exit(1)
        pool_id = json.loads(raw)["pool"]["id"]

    # Persist the parameters so the destroy-time provisioner (which cannot
    # reference other Terraform resources) can delete the pool later.
    if meta_file:
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(
                {"region": region, "project_id": project_id,
                 "lb_id": lb_id, "name": name, "pool_id": pool_id},
                f,
            )
    print(pool_id)


def delete_pool(region: str, project_id: str, lb_id: str, name: str, pool_id=None):
    pool_id = pool_id or _find_pool(region, project_id, lb_id, name)
    if not pool_id:
        print("pool not found, nothing to delete", file=sys.stderr)
        return
    _request(region, project_id, "DELETE", f"/v3/{project_id}/elb/pools/{pool_id}")
    print(f"deleted pool {pool_id}")


def patch_lb_ip_target(region: str, project_id: str, lb_id: str):
    """在负载均衡器上开启 IP 类型后端（ip_target_enable=true）。

    华为云 ELB 要求：当后端服务器组是 IP 类型（type=ip）时，其挂载的
    负载均衡器必须 ip_target_enable=true。该字段 provider 1.96.0 不支持，
    故用 REST PATCH 补齐。
    """
    body = json.dumps({"loadbalancer": {"ip_target_enable": True}})
    status, raw = _request(
        region, project_id, "PUT", f"/v3/{project_id}/elb/loadbalancers/{lb_id}", body=body
    )
    if status not in (200, 201, 202, 204):
        print(f"unexpected status {status}: {raw.decode('utf-8', 'replace')}", file=sys.stderr)
        sys.exit(1)
    print(f"patched LB {lb_id}: ip_target_enable=true")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="action", required=True)

    pc = sub.add_parser("create")
    pc.add_argument("--region", required=True)
    pc.add_argument("--project-id", required=True)
    pc.add_argument("--lb-id", required=True)
    pc.add_argument("--name", required=True)
    pc.add_argument("--meta-file", default=None,
                    help="Write pool parameters here for the destroy provisioner.")

    pd = sub.add_parser("delete")
    pd.add_argument("--meta-file", default=None,
                    help="Read pool parameters from this file (preferred).")
    pd.add_argument("--region", default=None)
    pd.add_argument("--project-id", default=None)
    pd.add_argument("--lb-id", default=None)
    pd.add_argument("--name", default=None)
    pd.add_argument("--pool-id", default=None)

    pp = sub.add_parser("patch-lb")
    pp.add_argument("--region", required=True)
    pp.add_argument("--project-id", required=True)
    pp.add_argument("--lb-id", required=True)

    args = p.parse_args()
    if args.action == "create":
        create_pool(args.region, args.project_id, args.lb_id, args.name, args.meta_file)
    elif args.action == "patch-lb":
        patch_lb_ip_target(args.region, args.project_id, args.lb_id)
    else:
        if args.meta_file and os.path.exists(args.meta_file):
            with open(args.meta_file, encoding="utf-8") as f:
                m = json.load(f)
            delete_pool(m["region"], m["project_id"], m["lb_id"], m["name"], m.get("pool_id"))
        elif args.meta_file:
            # meta 文件被引用但不存在（例如上一轮 create 在写 meta 前就失败了）：
            # 说明池子从未创建成功，销毁阶段无需任何操作。否则 delete_pool 会
            # 用一堆 None 参数去查 API 直接崩，导致 terraform_data 重建被卡死。
            print(f"meta file {args.meta_file} absent; pool was never created, nothing to delete",
                  file=sys.stderr)
        else:
            delete_pool(args.region, args.project_id, args.lb_id, args.name, args.pool_id)


if __name__ == "__main__":
    main()
