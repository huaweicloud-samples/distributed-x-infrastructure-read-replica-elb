# distributed-x-infrastructure-read-replica-elb · 云数据库只读副本统一入口（华为云实践）

[![License: MIT No Attribution](https://img.shields.io/badge/License-MIT--No--Attribution-green.svg)](LICENSE)
[![Huawei Cloud](https://img.shields.io/badge/Huawei%20Cloud-Best%20Practice-blue.svg)](https://www.huaweicloud.com)
[![Terraform](https://img.shields.io/badge/terraform-validated-success.svg)](https://www.terraform.io)
## 1. 简介 / Overview

### 简介

用**独享型 ELB（四层）+ IP 类型后端服务器组**为 云数据库 只读副本提供一个统一的只读入口，并让后端清单**自动跟随副本的增减与 IP 变化**——应用侧只连一个固定的内网地址，副本的扩缩与重建对它完全透明。
整套方案由一个每分钟触发一次的 **FunctionGraph 函数**维护：它发现当前在册的只读副本 IP，与 ELB 后端组的实际成员比对，采用「激进注册、审慎摘除」策略保持一致，并把中间状态持久化到 **OBS**，便于跨执行记忆。
- 本实践的代码与文档均按华为云原生产品构建，不依赖任何外部云 SDK 或服务。

云数据库 只读副本各自有独立的内网 IP 和域名，没有统一入口。常见做法是在应用里维护副本清单自己做轮询——但副本一扩缩就要改配置、重启应用。

本实践在副本前放一个负载均衡器，并让「后端清单自己跟上副本变化」，从而：

- 应用只需配置一个固定内网地址，副本扩缩/故障重建/规格变更对它透明；
- 新增副本约 1 分钟内自动进入流量，摘掉的副本经审慎确认后才退出；
- 不依赖任何人工运维动作。

---

## 2. 目录

- [1. 简介 / Overview](#1-简介--overview)
- [2. 目录](#2-目录)
- [3. 架构图](#3-架构图)
- [4. 方案亮点](#4-方案亮点)
- [5. 涉及云服务](#5-涉及云服务)
- [6. 前置条件](#6-前置条件)
- [7. 快速开始](#7-快速开始)
- [8. 分步部署](#8-分步部署)
- [9. 使用方法 / 验证](#9-使用方法--验证)
- [10. 清理资源](#10-清理资源)
- [11. 详细说明](#11-详细说明)
- [12. 依赖](#12-依赖)
- [13. FAQ / 故障排除](#13-faq--故障排除)
- [14. 贡献指南](#14-贡献指南)
- [15. 许可证](#15-许可证)
- [16. 联系方式 / 维护者](#16-联系方式--维护者)


```
distributed-x-infrastructure-read-replica-elb/
├── docs/ # 华为云实践文档
│ ├── architecture.md # 架构说明
│ ├── how-it-works.md # 运行原理与调参
│ ├── deployment-guide.md # 部署指导书
├── infra/ # Terraform（huaweicloud provider）
│ ├── versions.tf
│ ├── variables.tf
│ ├── main.tf
│ ├── outputs.tf
│ └── terraform.tfvars.example
├── src/ # 函数代码（Python）
│ ├── index.py # 入口与七步状态机
│ ├── discovery.py # 两种发现方式的分发
│ ├── hw_services.py # ELB / RDS / OBS 访问层
│ ├── common.py # DNS 解析与集合运算
│ └── constant.py # 环境配置
└── README.md # 本文件
```

---

## 3. 架构图

```
┌──────────────────────────┐
│ FunctionGraph 定时触发 │
│ 每 1 分钟一次 │
└────────────┬─────────────┘
│
▼
┌───────────────────────────────────────────────────────────────────┐
│ 注册函数（Python） │
│ ① 发现副本 IP ──── rds_api 模式：RDS v3 查 private_ips（默认） │
│ └── dns 模式：解析副本内网域名 │
│ ② 读后端服务器组当前成员 ③ 读 OBS 上一轮状态 │
│ ④ 算出该加/该减的 ⑤ 激进注册 / 审慎摘除 ⑥ 状态回写 OBS │
└───┬───────────────┬────────────────┬──────────────────┬───────────┘
│ │ │ │
▼ ▼ ▼ ▼
┌─────────┐ ┌────────────┐ ┌────────────┐ ┌─────────────┐
│ RDS │ │ 弹性负载 │ │ 对象存储 │ │ 云日志服务 │
│ 只读副本 │ │ 均衡 ELB │ │ OBS │ │ LTS │
└─────────┘ └─────┬──────┘ └────────────┘ └─────────────┘
│ TCP 3306
▼
┌──────────────────┐
│ IP 类型后端组 │ ← 成员完全由函数维护
│ 10.0.1.11:3306 │
└──────────────────┘
▲
┌────────┴─────────┐
│ 应用侧 │
└──────────────────┘
```

组件、两种发现方式对比、同/跨 VPC、子网双 ID、权限模型、数据保护等细节见 [docs/architecture.md](./docs/architecture.md)。

---

## 4. 方案亮点

| 模式 | 怎么做 | 是否需 VPC/NAT | 适用场景 |
|-|-|-|-|
| `rds_api`（默认） | 调 云数据库 v3 接口读 `private_ips` | 否 | 通用；成本最低、无 DNS 缓存 |
| `dns` | 解析副本内网域名（A 记录） | 需接入 VPC + 出网通道 | 运维以域名为准、或副本 ID 不便下发 |

默认走 `rds_api`：函数不接入 VPC、不挂 NAT，且在册副本 IP 变更无 TTL 延迟。两种模式下，七步状态机与审慎摘除逻辑完全一致。详见 [docs/architecture.md §两种副本发现方式](./docs/architecture.md)。

---

## 5. 涉及云服务

- RDS（云数据库）
- ELB（弹性负载均衡）
- FunctionGraph（函数工作流）
- OBS（对象存储服务）
- LTS（日志服务）

---

## 6. 前置条件

- 华为云账号（已实名、余额充足）
- 已注册华为云并开通 RDS / ELB / FunctionGraph / OBS / LTS 服务
- 本地工具：Terraform ≥ 1.5、Python 3.8+（函数代码）

---

## 7. 快速开始

1. **准备凭据与工具**

```bash
export HW_ACCESS_KEY="<你的AK>"
export HW_SECRET_KEY="<你的SK>"
# 安装并初始化 Terraform（huaweicloud provider）
```

2. **填写变量**

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
# 编辑 terraform.tfvars：至少填 region / vpc_id / subnet_id / rds_replica_ids
```

3. **校验与预览**

```bash
terraform init
terraform validate # 已通过
terraform plan # 确认 ELB 规格命中、无资源冲突
```

4. **部署**

确认资源清单后执行 `terraform apply` 完成部署。

5. **验证与收工**

按 [docs/deployment-guide.md §五、验证](./docs/deployment-guide.md) 逐项验证；演示结束后 `terraform destroy` 并 API 直查确认无保底型资源残留。

---

## 8. 分步部署

1. 准备 AK/SK 环境变量与 Terraform 工具。
2. 复制 `terraform.tfvars.example` 为 `terraform.tfvars` 并填写变量。
3. 执行 `terraform init` 初始化 provider。
4. 执行 `terraform validate` 校验语法。
5. 执行 `terraform plan` 预览资源清单。
6. 确认清单后执行 `terraform apply` 部署。
7. 验证后及时 `terraform destroy` 清理。

---

## 9. 使用方法 / 验证

### 使用方法与验证

**1. 查看输出**

```bash
terraform output
```
记下 `loadbalancer_private_ip`、`pool_id`、`function_urn`。

**2. 手工触发一次函数**

控制台 → 函数工作流 → 找到 `<name_prefix>-registrar-<suffix>` → 测试 → 事件模板选空事件 → 执行。也可以直接等待，定时器每分钟触发一次。

**3. 确认后端已注册**

```bash
hcloud ELB ListMembers --pool_id=<pool_id> --cli-region=<region> \
--jmes-filter="members[].{ip:address,port:protocol_port,status:operating_status}"
```
期望看到每个只读副本的私有 IP，端口与 `rds_listener_port` 一致。

**4. 确认状态文件已写入**

```bash
obsutil ls obs://<state_bucket>/<state_prefix>/
obsutil cat obs://<state_bucket>/<state_prefix>/active_ip.json
```
`active_ip.json` 中的 `IPList` 应与上一步成员列表一致。

**5. 查看日志**

云日志服务 → 日志组 `<name_prefix>-log-group-<suffix>` → 日志流 → 搜索 `invocation_summary`。正常稳态摘要示例（副本无变化时增删均为 0）：

```json
{"event":"invocation_summary","discovery_mode":"rds_api","discovered_ips":2,
"registered":0,"deregistered":0,"deregistration_skipped":false}
```

**6. 端到端连接测试**

从同 VPC 的一台机器：

```bash
mysql -h <loadbalancer_private_ip> -P 3306 -u <user> -p -e "SELECT @@hostname;"
```
多次连接，`@@hostname` 应在不同副本之间轮换。

**7. 验证自动跟随（可选）**

新增一个只读副本，将其实例 ID 加入 `rds_replica_ids` 后 `terraform apply`，等 1–2 分钟再查成员列表——新副本 IP 应已出现。反向验证：删除一个副本，等待约 `schedule × invocations_before_deregistration`（默认约 3 分钟），其 IP 应从成员列表消失。

部署完成后请按本节步骤自行验证，确认业务功能可用，方可视为交付完成。

---

## 10. 清理资源

演示结束后 `terraform destroy` 并 API 直查确认无保底型资源残留。

---

## 11. 详细说明

- 架构说明：[docs/architecture.md](./docs/architecture.md)
- 运行原理与参数调优：[docs/how-it-works.md](./docs/how-it-works.md)
- 部署指导书：[docs/deployment-guide.md](./docs/deployment-guide.md)

---

## 12. 依赖

- 华为云 Terraform Provider (`huaweicloud`)
- 华为云原生服务：RDS / ELB / FunctionGraph / OBS / LTS

---

## 13. FAQ / 故障排除

### FAQ

- **函数报 `context.getToken() returned nothing`？** 委托未正确挂到函数。检查 `terraform output agency_name`，再到控制台确认函数的委托配置。
- **注册接口返回 400，提示子网相关错误？** 同/跨 VPC 配置错误。副本与负载均衡器不在同一 VPC 时必须 `cross_vpc_backend = true`；在同一 VPC 时必须让 `backend_subnet_id` 指向副本真正所在的子网。
- **`dns` 模式解析不到域名？** 函数未接入 VPC，或接入的 VPC 不是副本所在 VPC。云数据库内网域名仅在其所属 VPC 内可解析。
- **成员注册成功但健康检查一直异常？** 数据库安全组未放行来自负载均衡器后端子网网段的 `rds_listener_port`。
- **每次 apply 都想改动 ELB 规格？** `elb_flavor_name` 在当前区域不存在或命中多条。用 `terraform console` 查看 `data.huaweicloud_elb_flavors.l4`，按实际目录填写。
- **副本 IP 变了但后端没跟上？** 先看 LTS 中 `discovered_ips` 是否正常。若为 0，是发现链路问题（权限、实例 ID 过期、域名不可解析）；若正常但 `registered` 持续非 0，是注册未生效，检查委托的 ELB 权限。

详细 FAQ 另见 [docs/deployment-guide.md](./docs/deployment-guide.md)。

---

## 14. 贡献指南

欢迎提交 Issue 与 PR。请遵循仓库内现有目录结构与代码风格。

---

## 15. 许可证

本实践采用 **MIT No Attribution** 许可证，详见仓库根目录 [`LICENSE`](LICENSE) 文件。

---

## 16. 联系方式 / 维护者
| 项 | 内容 |
|---|---|
| **创建者** | [dgc-git](https://github.com/dgc-git) |
| **维护者** | [dgc-git](https://github.com/dgc-git) |
| **联系方式** | GitHub Issues / PR（见[第 14 节](#14-贡献指南)） |
