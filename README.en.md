# distributed-x-infrastructure-read-replica-elb

[![License: MIT No Attribution](https://img.shields.io/badge/License-MIT--No--Attribution-green.svg)](LICENSE)
[![Huawei Cloud](https://img.shields.io/badge/Huawei%20Cloud-Best%20Practice-blue.svg)](https://www.huaweicloud.com)
[![Terraform](https://img.shields.io/badge/terraform-validated-success.svg)](https://www.terraform.io)

## 1. Overview

### Overview

A **dedicated ELB (Layer 4) + IP-type backend server group** provides a unified read-only entry point for database read replicas, and lets the backend list **automatically follow replica additions/removals and IP changes** — the application side only connects to one fixed private address, and replica scaling and rebuilds are completely transparent to it.

The whole solution is maintained by a **FunctionGraph function** triggered once per minute: it discovers the IPs of currently registered read replicas, compares them with the actual members of the ELB backend group, keeps them consistent using an "aggressive registration, cautious deregistration" strategy, and persists intermediate state to **OBS** for cross-invocation memory.
- The code and documentation of this practice are built on Huawei Cloud native products and do not depend on any external cloud SDK or service.

Each database read replica has its own private IP and domain name, with no unified entry point. A common approach is to maintain a replica list in the application and do round-robin yourself — but scaling replicas requires changing configuration and restarting the application.

This practice places a load balancer in front of the replicas and makes "the backend list follow replica changes by itself", so that:

- The application only needs one fixed private address; replica scaling, failure rebuilds, and specification changes are transparent to it;
- New replicas automatically enter traffic within about 1 minute, and removed replicas exit only after cautious confirmation;
- No manual operations are required.

---

## 2. Table of Contents

- [1. Overview](#1-overview)
- [2. Table of Contents](#2-table-of-contents)
- [3. Architecture Diagram](#3-architecture-diagram)
- [4. Highlights](#4-highlights)
- [5. Cloud Services Used](#5-cloud-services-used)
- [6. Prerequisites](#6-prerequisites)
- [7. Quick Start](#7-quick-start)
- [8. Step-by-Step Deployment](#8-step-by-step-deployment)
- [9. Usage / Verification](#9-usage--verification)
- [10. Cleanup](#10-cleanup)
- [11. Detailed Explanation](#11-detailed-explanation)
- [12. Dependencies](#12-dependencies)
- [13. FAQ / Troubleshooting](#13-faq--troubleshooting)
- [14. Contributing](#14-contributing)
- [15. License](#15-license)
- [16. Contact / Maintainers](#16-contact--maintainers)

```
distributed-x-infrastructure-read-replica-elb/
├── docs/ # Huawei Cloud practice documentation
│ ├── architecture.md # Architecture description
│ ├── how-it-works.md # How it works and tuning
│ ├── deployment-guide.md # Deployment guide
├── infra/ # Terraform (huaweicloud provider)
│ ├── versions.tf
│ ├── variables.tf
│ ├── main.tf
│ ├── outputs.tf
│ └── terraform.tfvars.example
├── src/ # Function code (Python)
│ ├── index.py # Entry point and seven-step state machine
│ ├── discovery.py # Dispatch between the two discovery methods
│ ├── hw_services.py # ELB / RDS / OBS access layer
│ ├── common.py # DNS resolution and set operations
│ └── constant.py # Environment configuration
└── README.md # This file
```

---

## 3. Architecture Diagram

```
┌────────────────────────────┐
│ FunctionGraph scheduled    │
│ trigger                    │
│ Once every 1 minute        │
└────────────┬───────────────┘
│
▼
┌───────────────────────────────────────────────────────────────────┐
│ Registrar function (Python)                                       │
│ ① Discover replica IPs ──── rds_api mode: RDS v3 private_ips      │
│                          └── dns mode: resolve replica intranet   │
│                              domain names                         │
│ ② Read current backend group members                              │
│ ③ Read previous state from OBS                                    │
│ ④ Compute adds/removes  ⑤ Aggressive register / Cautious removal  │
│ ⑥ Write state back to OBS                                         │
└───┬───────────────┬────────────────┬──────────────────┬───────────┘
│ │ │ │
▼ ▼ ▼ ▼
┌─────────┐ ┌────────────┐ ┌────────────┐ ┌─────────────┐
│ RDS     │ │ Elastic    │ │ Object     │ │ Cloud Log   │
│ read    │ │ Load Bal.  │ │ Storage    │ │ Service     │
│ replicas│ │ ELB        │ │ OBS        │ │ LTS         │
└─────────┘ └─────┬──────┘ └────────────┘ └─────────────┘
│ TCP 3306
▼
┌──────────────────┐
│ IP-type backend  │ ← Members fully maintained by the function
│ group            │
│ 10.0.1.11:3306   │
└──────────────────┘
▲
┌────────┴─────────┐
│ Application side │
└──────────────────┘
```

For details on components, comparison of the two discovery methods, same/cross VPC, dual subnet IDs, the permission model, and data protection, see [docs/architecture.md](./docs/architecture.md).

---

## 4. Highlights

| Mode | How it works | VPC/NAT required | Suitable scenarios |
|---|---|---|---|
| `rds_api` (default) | Call the database v3 API to read `private_ips` | No | General purpose; lowest cost and no DNS caching |
| `dns` | Resolve replica intranet domain names (A records) | Requires VPC access + an internet egress channel | Operations based on domain names, or when replica IDs are inconvenient to distribute |

The default is `rds_api`: the function does not join a VPC or attach NAT, and there is no TTL delay when a registered replica's IP changes. In both modes, the seven-step state machine and the cautious deregistration logic are exactly the same. See [docs/architecture.md §Two replica discovery methods](./docs/architecture.md) for details.

---

## 5. Cloud Services Used

- RDS (Relational Database Service)
- ELB (Elastic Load Balance)
- FunctionGraph
- OBS (Object Storage Service)
- LTS (Log Tank Service)

---

## 6. Prerequisites

- A Huawei Cloud account (real-name verified, with sufficient balance)
- RDS / ELB / FunctionGraph / OBS / LTS services enabled
- Local tools: Terraform >= 1.5, Python 3.8+ (for the function code)

---

## 7. Quick Start

1. **Prepare credentials and tools**

```bash
export HW_ACCESS_KEY="<your AK>"
export HW_SECRET_KEY="<your SK>"
# Install and initialize Terraform (huaweicloud provider)
```

2. **Fill in variables**

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: fill in at least region / vpc_id / subnet_id / rds_replica_ids
```

3. **Validate and preview**

```bash
terraform init
terraform validate # passed
terraform plan # confirm ELB flavor matches and there are no resource conflicts
```

4. **Deploy**

After confirming the resource list, run `terraform apply` to complete deployment.

5. **Verify and finish**

Verify item by item following [docs/deployment-guide.md §5 Verification](./docs/deployment-guide.md); after the demo, run `terraform destroy` and check via the API that no residual resources remain.

---

## 8. Step-by-Step Deployment

1. Prepare the AK/SK environment variables and the Terraform tool.
2. Copy `terraform.tfvars.example` to `terraform.tfvars` and fill in the variables.
3. Run `terraform init` to initialize the provider.
4. Run `terraform validate` to validate the syntax.
5. Run `terraform plan` to preview the resource list.
6. After confirming the list, run `terraform apply` to deploy.
7. After verification, run `terraform destroy` promptly to clean up.

---

## 9. Usage / Verification

### Usage and Verification

**1. View outputs**

```bash
terraform output
```
Note down `loadbalancer_private_ip`, `pool_id`, and `function_urn`.

**2. Trigger the function manually**

Console → FunctionGraph → find `<name_prefix>-registrar-<suffix>` → Test → select the empty event template → Execute. You can also simply wait, as the timer triggers once per minute.

**3. Confirm the backend is registered**

```bash
hcloud ELB ListMembers --pool_id=<pool_id> --cli-region=<region> \
--jmes-filter="members[].{ip:address,port:protocol_port,status:operating_status}"
```
You should see the private IP of each read replica, with a port matching `rds_listener_port`.

**4. Confirm the state file has been written**

```bash
obsutil ls obs://<state_bucket>/<state_prefix>/
obsutil cat obs://<state_bucket>/<state_prefix>/active_ip.json
```
The `IPList` in `active_ip.json` should match the member list from the previous step.

**5. View logs**

Cloud Log Service → log group `<name_prefix>-log-group-<suffix>` → log stream → search for `invocation_summary`. A normal steady-state summary looks like this (with no replica changes, adds and removes are both 0):

```json
{"event":"invocation_summary","discovery_mode":"rds_api","discovered_ips":2,
"registered":0,"deregistered":0,"deregistration_skipped":false}
```

**6. End-to-end connection test**

From a machine in the same VPC:

```bash
mysql -h <loadbalancer_private_ip> -P 3306 -u <user> -p -e "SELECT @@hostname;"
```
Connect several times; `@@hostname` should rotate among the different replicas.

**7. Verify automatic following (optional)**

Add a new read replica, add its instance ID to `rds_replica_ids`, run `terraform apply`, then check the member list again after 1–2 minutes — the new replica's IP should be present. Reverse verification: delete a replica and wait about `schedule × invocations_before_deregistration` (about 3 minutes by default); its IP should disappear from the member list.

After deployment, follow these steps to verify that the business functionality works before considering the delivery complete.

---

## 10. Cleanup

After the demo, run `terraform destroy` and check via the API that no residual resources remain.

---

## 11. Detailed Explanation

- Architecture description: [docs/architecture.md](./docs/architecture.md)
- How it works and parameter tuning: [docs/how-it-works.md](./docs/how-it-works.md)
- Deployment guide: [docs/deployment-guide.md](./docs/deployment-guide.md)

---

## 12. Dependencies

- Huawei Cloud Terraform Provider (`huaweicloud`)
- Huawei Cloud native services: RDS / ELB / FunctionGraph / OBS / LTS

---

## 13. FAQ / Troubleshooting

- **The function reports `context.getToken() returned nothing`?** The agency is not correctly attached to the function. Check `terraform output agency_name`, then confirm the function's agency configuration in the console.
- **The registration API returns 400 with a subnet-related error?** Same/cross VPC is misconfigured. When the replicas and the load balancer are not in the same VPC, `cross_vpc_backend = true` is required; when they are in the same VPC, `backend_subnet_id` must point to the subnet where the replicas actually reside.
- **The `dns` mode cannot resolve domain names?** The function is not connected to a VPC, or the VPC it is connected to is not the one where the replicas reside. Database intranet domain names are only resolvable within their own VPC.
- **Members are registered but health checks keep failing?** The database security group does not allow `rds_listener_port` from the load balancer backend subnet CIDR.
- **Every apply wants to change the ELB flavor?** `elb_flavor_name` does not exist in the current region or matches multiple entries. Use `terraform console` to inspect `data.huaweicloud_elb_flavors.l4` and fill in the actual catalog value.
- **A replica IP changed but the backend did not follow?** First check whether `discovered_ips` in LTS is normal. If it is 0, the discovery chain has a problem (permissions, stale instance ID, unresolvable domain name); if it is normal but `registered` remains non-zero, registration did not take effect — check the agency's ELB permissions.

For more FAQs, see [docs/deployment-guide.md](./docs/deployment-guide.md).

---

## 14. Contributing

Issues and PRs are welcome. Please follow the existing directory structure and code style in the repository.

---

## 15. License

This practice is licensed under **MIT No Attribution**; see the [`LICENSE`](LICENSE) file in the repository root.

---

## 16. Contact / Maintainers
| Item | Content |
|---|---|
| **Creator** | [dgc-git](https://github.com/dgc-git) |
| **Maintainer** | [dgc-git](https://github.com/dgc-git) |
| **Contact** | GitHub Issues / PR (see [Section 14](#14-contributing)) |
