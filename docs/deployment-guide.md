# 部署指导书

## 前置条件

| 项 | 要求 |
|-|-|
| Terraform | ≥ 1.4.0（模板用到 `terraform_data` 的 precondition） |
| 华为云账号 | 具备创建 ELB / FunctionGraph / OBS / LTS / IAM 委托的权限 |
| 已有 VPC 与子网 | 负载均衡器需要占用一个子网 |
| 已有 云数据库 只读副本 | 或者打开 `create_rds_replicas` 由模板创建演示副本 |

创建 IAM 委托需要账号具有 `Security Administrator` 权限。若受限于组织策略，可让管理员先建好委托，再把 `huaweicloud_identity_agency.function` 换成数据源引用。

## 一、准备凭据

凭据只从环境变量注入，**不要写进 `.tf` 或 `.tfvars`**：

```bash
export HW_ACCESS_KEY=<AK>
export HW_SECRET_KEY=<SK>
export HW_REGION_NAME=<region> # 例如 cn-north-4
```

Windows PowerShell：

```powershell
$env:HW_ACCESS_KEY = "<AK>"
$env:HW_SECRET_KEY = "<SK>"
$env:HW_REGION_NAME = "<region>"
```

## 二、填写变量

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
```

最少需要填这几项：

```hcl
region = "cn-north-4"
vpc_id = "<VPC_ID>"
subnet_id = "<SUBNET_ID>" # 子网（网络）ID
elb_availability_zones = ["cn-north-4a"]

discovery_mode = "rds_api"
rds_replica_ids = ["<REPLICA_ID_1>", "<REPLICA_ID_2>"]
```

副本实例 ID 的获取方式：

```bash
hcloud RDS ListInstances --cli-region=cn-north-4 \
--jmes-filter="instances[?type=='Replica'].{id:id,name:name,ip:private_ips[0]}"
```

或者在控制台的 云数据库 实例列表里，只读实例名称右侧展开即可看到实例 ID。

### 切到 DNS 模式

```hcl
discovery_mode = "dns"
rds_replica_dns_names = ["<replica-1>.rds.cn-north-4.myhuaweicloud.com"]
```

此模式下模板会自动把函数接入 VPC，并给委托追加 `VPC FullAccess`。

### 副本跨 VPC

```hcl
cross_vpc_backend = true
```

负载均衡器会自动开启跨 VPC 后端能力，注册成员时不再携带子网 ID。

### 副本在同 VPC 的另一个子网

```hcl
backend_subnet_id = "<BACKEND_SUBNET_ID>"
```

## 三、确认资源清单

执行 `terraform plan` 预览本次将要创建的资源，确认无误后再继续。`plan` 只做预览，不会创建任何资源。

## 四、部署

```bash
terraform init
terraform validate
terraform plan
terraform apply
```

`plan` 阶段会自动检查这些容易踩的坑：

- 没有可发现的副本（三个来源都是空）；
- 打开了 `create_rds_replicas` 但没给主实例 ID、规格或可用区；
- 函数超时 ≥ 调度周期；
- ELB 规格名称在本区域命中的条数不等于 1。

## 五、验证

### 1. 看输出

```bash
terraform output
```

记下 `loadbalancer_private_ip`、`pool_id`、`function_urn`。

### 2. 手工触发一次函数

控制台 → 函数工作流 → 找到 `<name_prefix>-registrar-<suffix>` → 测试 → 事件模板选空事件 → 执行。

也可以直接等，定时器每分钟会触发一次。

### 3. 确认后端已注册

```bash
hcloud ELB ListMembers --pool_id=<pool_id> --cli-region=<region> \
--jmes-filter="members[].{ip:address,port:protocol_port,status:operating_status}"
```

期望看到每个只读副本的私有 IP，端口与 `rds_listener_port` 一致。

### 4. 确认状态文件已写入

```bash
obsutil ls obs://<state_bucket>/<state_prefix>/
obsutil cat obs://<state_bucket>/<state_prefix>/active_ip.json
```

`active_ip.json` 里的 `IPList` 应当与上一步的成员列表一致。

### 5. 看日志

云日志服务 → 日志组 `<name_prefix>-log-group-<suffix>` → 日志流 → 搜 `invocation_summary`。

一条正常的稳态摘要长这样（副本没变化时增删都是 0）：

```json
{"event":"invocation_summary","discovery_mode":"rds_api","discovered_ips":2,
"registered":0,"deregistered":0,"deregistration_skipped":false}
```

### 6. 端到端连一次

从同 VPC 的一台机器：

```bash
mysql -h <loadbalancer_private_ip> -P 3306 -u <user> -p -e "SELECT @@hostname;"
```

多连几次，`@@hostname` 应当在不同副本之间轮换。

### 7. 验证自动跟随（可选）

新增一个只读副本，把它的实例 ID 加进 `rds_replica_ids` 后 `terraform apply`，等一到两分钟再查一次成员列表——新副本的 IP 应该已经在里面了。

反向验证：删掉一个副本，等约 `schedule × invocations_before_deregistration`（默认约 3 分钟），它的 IP 应当从成员列表中消失。

## 六、接入应用

把应用的**只读**数据源指向 `loadbalancer_private_ip:<rds_listener_port>`。写操作仍然直连主实例。

连接池建议：

- 开启连接存活检测，让连接池能感知后端变化；
- 连接最大存活时间设短一些（例如 30 分钟），避免长连接一直钉在某个副本上；
- 应用侧不需要再维护任何副本清单。

## 七、清理

```bash
terraform destroy
```

destroy 之后**务必实查一遍**，`0 destroyed` 不等于云端干净：

```bash
hcloud ELB ListLoadBalancers --cli-region=<region> \
--jmes-filter="loadbalancers[?contains(name,'<name_prefix>')].{id:id,name:name}"
hcloud RDS ListInstances --cli-region=<region> \
--jmes-filter="instances[?contains(name,'<name_prefix>')].{id:id,name:name}"
```

若开过 `obs_versioning`，桶里会留下历史版本，`force_destroy` 之后仍可能残留：

```bash
obsutil rm obs://<state_bucket> -r -f -v
```

## 常见问题

**函数报 `context.getToken() returned nothing`**
委托没有正确挂到函数上。检查 `terraform output agency_name`，再到控制台确认函数的委托配置。

**注册接口返回 400，提示子网相关错误**
同 VPC / 跨 VPC 配错了。副本与负载均衡器不在同一个 VPC 时必须 `cross_vpc_backend = true`；在同一个 VPC 时必须让 `backend_subnet_id` 指向副本真正所在的子网。

**`dns` 模式解析不到域名**
函数没接入 VPC，或者接入的 VPC 不是副本所在的 VPC。云数据库 内网域名只在其所属 VPC 内可解析。

**成员注册上了但健康检查一直异常**
数据库安全组没有放行来自负载均衡器后端子网网段的 `rds_listener_port`。

**每次 apply 都想改动 ELB 规格**
`elb_flavor_name` 在当前区域不存在或命中多条。用 `terraform console` 查一下 `data.huaweicloud_elb_flavors.l4`，按实际目录填。

**副本 IP 变了但后端没跟上**
先看 LTS 里 `discovered_ips` 是否正常。若为 0，是发现链路的问题（权限、实例 ID 过期、域名不可解析）；若正常但 `registered` 一直非 0，是注册没生效，检查委托的 ELB 权限。
