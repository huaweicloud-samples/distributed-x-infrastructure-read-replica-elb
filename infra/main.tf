# ===========================================================================
# RDS 只读副本自动注册到独享型负载均衡器
#
# 数据通路：
#   定时器 → 函数 → 发现副本 IP → 与后端服务器组现有成员比对
#          → 激进注册 / 审慎摘除 → 状态回写对象存储
#
# 两处结构性差异（详见 docs/how-it-works.md）：
#   1. 发现方式默认走 RDS API，函数因此不需要接入 VPC，也就不需要 NAT 网关。
#      dns 模式通过解析副本内网域名发现 IP，此时函数会自动接入 VPC。
#   2. 后端服务器组用「IP 类型」；成员的增删走 batch-add / batch-delete 批量接口。
# ===========================================================================

locals {
  name_prefix = var.name_prefix
  suffix      = random_id.suffix.hex

  # dns 模式依赖 RDS 内网域名，域名只在 VPC 内可解析，函数必须接入 VPC。
  needs_vpc_access = var.discovery_mode == "dns"

  # VPC / 子网 / 安全组：自建时取 bootstrap.tf 资源，否则用调用方传入。
  vpc_id = var.create_vpc ? huaweicloud_vpc.this[0].id : var.vpc_id
  # 子网（网络）ID：RDS 主实例与 FunctionGraph 使用。
  subnet_id = var.create_vpc ? huaweicloud_vpc_subnet.this[0].id : var.subnet_id
  # 原生 IPv4 子网 ID：ELB ipv4_subnet_id 与后端服务器 subnet_cidr_id 使用。
  ipv4_subnet_id = var.create_vpc ? huaweicloud_vpc_subnet.this[0].ipv4_subnet_id : data.huaweicloud_vpc_subnet.this[0].ipv4_subnet_id
  security_group_id = var.create_vpc ? huaweicloud_networking_secgroup.this[0].id : var.security_group_id

  # 后端子网：留空即与负载均衡器同子网。跨 VPC 场景下不存在这个概念。
  backend_subnet_id = var.backend_subnet_id != "" ? var.backend_subnet_id : local.subnet_id

  # 注册后端时携带的原生 IPv4 子网 ID。跨 VPC 后端必须留空，带上会被 ELB 拒绝。
  subnet_cidr_id = var.cross_vpc_backend ? "" : data.huaweicloud_vpc_subnet.backend[0].ipv4_subnet_id

  obs_bucket_name = (
    var.obs_bucket_name != ""
    ? var.obs_bucket_name
    : "${local.name_prefix}-state-${local.suffix}"
  )

  # 是否创建只读副本：自建主实例会自动派生副本。
  create_replicas = var.create_rds_replicas || var.create_rds_primary

  # 主实例 ID：自建时取 bootstrap.tf 创建的主实例，否则用调用方传入。
  rds_primary_instance_id = var.create_rds_primary ? huaweicloud_rds_instance.primary[0].id : var.rds_primary_instance_id

  # 副本清单：优先用新建出来的副本，否则用调用方传入的既有副本。
  replica_ids = (
    local.create_replicas
    ? huaweicloud_rds_read_replica_instance.this[*].id
    : var.rds_replica_ids
  )

  replica_dns_names = (
    local.create_replicas
    ? [for r in huaweicloud_rds_read_replica_instance.this : "${r.id}.rds.${var.region}.${var.cloud_domain}"]
    : var.rds_replica_dns_names
  )

  agency_project_roles = (
    local.needs_vpc_access
    ? distinct(concat(var.agency_project_roles, ["VPC FullAccess"]))
    : var.agency_project_roles
  )
}

# 参数自检：把会在 apply 中途才暴露的配置错误提前到 plan 阶段。
resource "terraform_data" "input_validation" {
  lifecycle {
    precondition {
      condition = (
        var.create_rds_primary
        || var.create_rds_replicas
        || (var.discovery_mode == "rds_api" && length(var.rds_replica_ids) > 0)
        || (var.discovery_mode == "dns" && length(var.rds_replica_dns_names) > 0)
      )
      error_message = <<-EOT
        没有可发现的只读副本。请三选一：
          - create_rds_primary = true，由本模板自建主实例并自动派生只读副本；
          - create_rds_replicas = true，由本模板创建演示副本（需提供 rds_primary_instance_id）；
          - 直接提供 rds_replica_ids / rds_replica_dns_names。
      EOT
    }

    precondition {
      condition     = var.create_vpc || var.vpc_id != ""
      error_message = "create_vpc = false 时必须提供 vpc_id。"
    }

    precondition {
      condition     = var.create_vpc || var.subnet_id != ""
      error_message = "create_vpc = false 时必须提供 subnet_id。"
    }

    precondition {
      condition     = !var.create_rds_primary || var.create_vpc || var.security_group_id != ""
      error_message = "create_rds_primary = true 且 create_vpc = false 时必须提供 security_group_id。"
    }

    precondition {
      condition     = !var.create_rds_primary || var.rds_primary_flavor != ""
      error_message = "create_rds_primary = true 时必须显式指定 rds_primary_flavor，规格直接决定单价。"
    }

    precondition {
      condition     = !var.create_rds_primary || length(var.rds_primary_availability_zones) > 0
      error_message = "create_rds_primary = true 时必须提供 rds_primary_availability_zones。"
    }

    precondition {
      condition     = !var.create_rds_primary || length(var.rds_replica_availability_zones) > 0
      error_message = "create_rds_primary = true 时会自动创建只读副本，必须提供 rds_replica_availability_zones。"
    }

    precondition {
      condition     = !var.create_rds_replicas || var.create_rds_primary || var.rds_primary_instance_id != ""
      error_message = "仅 create_rds_replicas = true（未自建主实例）时必须提供 rds_primary_instance_id。"
    }

    precondition {
      condition     = !var.create_rds_replicas || var.rds_replica_flavor != ""
      error_message = "create_rds_replicas = true 时必须显式指定 rds_replica_flavor，规格直接决定单价。"
    }

    precondition {
      condition = (
        !var.create_rds_replicas
        || length(var.rds_replica_availability_zones) > 0
      )
      error_message = "create_rds_replicas = true 时必须提供 rds_replica_availability_zones。"
    }

    precondition {
      condition     = var.function_timeout < 60
      error_message = "函数超时须小于调度周期（默认 1 分钟），否则相邻两次执行会重叠。"
    }
  }
}

# 后端所在子网。跨 VPC 后端时不查询——后端不在本账号可见的子网里。
data "huaweicloud_vpc_subnet" "backend" {
  count = var.cross_vpc_backend ? 0 : 1

  id = local.backend_subnet_id
}

# 取当前区域的 project_id，供下面的 ELB 池 REST 创建脚本使用
# （该脚本绕过 provider 直连 API，URL 路径里需要 project_id）。
data "huaweicloud_identity_projects" "this" {
  name = var.region
}

# ---------------------------------------------------------------------------
# 可选：演示用只读副本
#
# 对应「可选：创建演示用只读副本」配置块。默认不创建——
# 只读实例按规格 + 存储实时计费，不该由一个"顺带"的开关产生费用。
# ---------------------------------------------------------------------------
resource "huaweicloud_rds_read_replica_instance" "this" {
  count = local.create_replicas ? var.rds_replica_count : 0

  name                = "${local.name_prefix}-rr-${count.index + 1}-${local.suffix}"
  primary_instance_id = local.rds_primary_instance_id
  flavor              = var.rds_replica_flavor

  # 注意：这里是字符串而非列表。资源文档的示例写成了列表，实际 schema 是
  # String，写成列表会在 plan 阶段报类型错误。
  availability_zone = element(
    var.rds_replica_availability_zones,
    count.index % max(length(var.rds_replica_availability_zones), 1)
  )

  enterprise_project_id = var.enterprise_project_id != "" ? var.enterprise_project_id : null

  volume {
    type = var.rds_replica_volume_type
    size = var.rds_replica_volume_size > 0 ? var.rds_replica_volume_size : null
  }

  tags = {
    workload = "rds-read-replica-elb-registration"
  }
}

# ---------------------------------------------------------------------------
# 负载均衡（独享型网络负载均衡器 + IP 目标组）
# ---------------------------------------------------------------------------

# 规格必须按名称精确取。绝不写 data.huaweicloud_elb_flavors.l4.ids[N]：
# 该数据源按 flavor UUID 字典序返回，下标与规格大小没有关系，
# 必须按名称精确匹配目标规格。
data "huaweicloud_elb_flavors" "l4" {
  type = var.elb_flavor_type
  name = var.elb_flavor_name
}

resource "huaweicloud_elb_loadbalancer" "this" {
  name        = "${local.name_prefix}-elb-${local.suffix}"
  description = "Fronts the RDS read replicas discovered by the FunctionGraph function"

  vpc_id         = local.vpc_id
  ipv4_subnet_id = local.ipv4_subnet_id

  # 可用区数量是 ELB 计费的直接乘数。独享型 ELB 本身可跨可用区转发到后端，
  # 单可用区部署不会削弱后端的多可用区容灾能力。
  availability_zone = var.elb_availability_zones

  # 后端跨 VPC 时必须打开，否则注册非本 VPC 的 IP 会被拒绝。
  cross_vpc_backend = var.cross_vpc_backend

  # cost-preflight: allow-index reason=按 flavor 名称精确过滤，候选集唯一，规格与价格确定
  l4_flavor_id = data.huaweicloud_elb_flavors.l4.ids[0]

  enterprise_project_id = var.enterprise_project_id != "" ? var.enterprise_project_id : null

  tags = {
    workload = "rds-read-replica-elb-registration"
  }

  lifecycle {
    # cross_vpc_backend 在 state 中为 true（LB 实际运行状态），而配置默认 false。
    # 该差异会让每次 apply 尝试 PUT LB 改回 false，而 ELB 在有 IP 成员时拒绝
    # 把 ip_target_enable 重置为 false（ELB.8902）。忽略此字段即可避免 PUT，
    # 再由 terraform_data.elb_lb_ip_target_enable 的 patch-lb 兜底保持
    # ip_target_enable=true，二者配合即可安全运行期。
    ignore_changes = [cross_vpc_backend]

    # 兜住上面那个下标：名称过滤必须刚好命中一个规格，否则 ids[0] 又变成
    # "碰运气"，而规格直接决定单价。
    precondition {
      condition     = length(data.huaweicloud_elb_flavors.l4.ids) == 1
      error_message = <<-EOT
        elb_flavor_type = "${var.elb_flavor_type}" 与 elb_flavor_name = "${var.elb_flavor_name}"
        在本区域命中的规格数不等于 1。规格目录随区域略有差异，请先实查再指定：
          terraform console
          > data.huaweicloud_elb_flavors.l4
      EOT
    }
  }
}

# IP 类型后端服务器组 —— 成员以 IP + 端口形式注册，可承接任意来源的副本 IP。
#
# 注意：本应直接用 huaweicloud_elb_pool 资源，但该 provider 版本（<= 1.96.0）
# 不支持 ip_target_enable 属性，而华为云 ELB 要求 IP 类型池必须显式开启
# ip_target_enable（否则报 ELB.8902）。因此这里绕开 provider，用官方 REST API
# （AK/SK 直接签名，无需换取 token）创建该池，遵循运行期所有权原则：
# 池子本身仍由 Terraform 生命周期（terraform_data）完整管理，destroy 时一并清理。
resource "terraform_data" "elb_ip_pool" {
  # 注意：销毁期 provisioner 只能引用本资源自身属性，不能引用 path.module 或其他
  # 资源。因此创建/销毁脚本与 meta 文件一律用相对路径（local-exec 的工作目录即模块
  # 目录 infra/），避免任何跨资源引用。
  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      PY="$(command -v python3 || command -v python)"
      # IP 类型池要求挂载它的 ELB 先开启 ip_target_enable（该字段属于 LB，
      # 不属于池）。provider 1.96.0 不支持，故这里先直连 REST PATCH LB，
      # 再创建池子。两步任一失败都会让 set -e 中止，apply 报错而非污染状态。
      "$${PY}" "./scripts/elb_ip_pool.py" patch-lb \
        --region "${var.region}" \
        --project-id "${data.huaweicloud_identity_projects.this.projects[0].id}" \
        --lb-id "${huaweicloud_elb_loadbalancer.this.id}"
      "$${PY}" "./scripts/elb_ip_pool.py" create \
        --region "${var.region}" \
        --project-id "${data.huaweicloud_identity_projects.this.projects[0].id}" \
        --lb-id "${huaweicloud_elb_loadbalancer.this.id}" \
        --name "${local.name_prefix}-pool-${local.suffix}" \
        --meta-file "./.build/elb_pool_meta.json"
    EOT
    # 凭据由父进程环境变量继承（HW_ACCESS_KEY / HW_SECRET_KEY），不写入任何文件。
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      PY="$(command -v python3 || command -v python)"
      "$${PY}" "./scripts/elb_ip_pool.py" delete --meta-file "./.build/elb_pool_meta.json"
    EOT
  }
}

# 保活 LB 的 ip_target_enable。
# 该属性属于负载均衡器、provider 1.96.0 不支持，只能 REST PATCH。
# 但任何对 LB 的 apply（含 in-place update，例如同步 cross_vpc_backend /
# tags）都会把它重置回 false，导致 IP 成员注册失败（ELB.8939）。
# 因此用本资源在「每次 apply」结束后重新 PATCH 一次保持 true。
# input 含 timestamp() 是为了确保每次 apply 都执行该 provisioner
# （而非仅在 LB 被 replace 时）——这样才能兜住 in-place update 的重置。
resource "terraform_data" "elb_lb_ip_target_enable" {
  input = {
    lb_id = huaweicloud_elb_loadbalancer.this.id
    nonce = timestamp()
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      PY="$(command -v python3 || command -v python)"
      "$${PY}" "./scripts/elb_ip_pool.py" patch-lb \
        --region "${var.region}" \
        --project-id "${data.huaweicloud_identity_projects.this.projects[0].id}" \
        --lb-id "${huaweicloud_elb_loadbalancer.this.id}"
    EOT
  }
}

# 把 REST 创建的池子查回来，供 monitor / listener / 函数引用。
# depends_on 让该数据源在池子创建之后（apply 阶段）才读取，避免在 plan 阶段
# 因为池子还不存在而报错。
data "huaweicloud_elb_pools" "this" {
  depends_on      = [terraform_data.elb_ip_pool]
  loadbalancer_id = huaweicloud_elb_loadbalancer.this.id
  name            = "${local.name_prefix}-pool-${local.suffix}"
}

# 这里刻意没有 huaweicloud_elb_member 资源。
# 后端成员由函数在运行期增删，一旦纳入 Terraform 管理，每次 apply 都会把函数
# 注册进来的副本当成"配置漂移"清掉，与整个方案的目的直接冲突。

resource "huaweicloud_elb_monitor" "this" {
  pool_id     = data.huaweicloud_elb_pools.this.pools[0].id
  name        = "${local.name_prefix}-hc-${local.suffix}"
  protocol    = "TCP"
  interval    = var.health_check_interval
  timeout     = var.health_check_timeout
  max_retries = var.health_check_max_retries
  port        = var.rds_listener_port
}

resource "huaweicloud_elb_listener" "this" {
  name            = "${local.name_prefix}-listener-${local.suffix}"
  protocol        = "TCP"
  protocol_port   = var.rds_listener_port
  loadbalancer_id = huaweicloud_elb_loadbalancer.this.id
  default_pool_id = data.huaweicloud_elb_pools.this.pools[0].id
}

# ---------------------------------------------------------------------------
# 状态存储（OBS 对象存储桶）
# ---------------------------------------------------------------------------
resource "huaweicloud_obs_bucket" "state" {
  bucket        = local.obs_bucket_name
  acl           = "private"
  storage_class = "STANDARD"
  force_destroy = var.obs_force_destroy

  # 版本控制便于回溯误摘除时的状态文件现场，但开启后桶内会留下历史版本，
  # destroy 时容易残留。默认关闭，生产环境按需打开。
  versioning = var.obs_versioning

  # SSE-OBS（AES256）由对象存储自带密钥，不产生 KMS 密钥费用。
  # 换成 kms 会额外按密钥个数计费。
  encryption    = true
  sse_algorithm = "AES256"

  enterprise_project_id = var.enterprise_project_id != "" ? var.enterprise_project_id : null

  tags = {
    workload = "rds-read-replica-elb-registration"
  }
}

# OBS 数据面（对象读写）不认 IAM 角色授权，必须靠桶策略放行。
# 这里显式允许函数委托（agency）的临时凭证读写本桶对象，
# 否则函数运行时会一直 403 AccessDenied。
resource "huaweicloud_obs_bucket_policy" "state" {
  bucket = huaweicloud_obs_bucket.state.bucket
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          ID = "domain/${var.account_id}:agency/${huaweicloud_identity_agency.function.name}"
        }
        Action = [
          "GetObject",
          "PutObject",
          "DeleteObject",
          "ListBucket"
        ]
        Resource = [
          huaweicloud_obs_bucket.state.bucket,
          "${huaweicloud_obs_bucket.state.bucket}/*"
        ]
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# 运行时权限（函数执行委托）
#
# 函数不持有任何静态凭据：运行期通过委托换取临时 token / 临时 AK-SK，
# 代码、环境变量、Terraform 状态里都不会出现密钥。
# ---------------------------------------------------------------------------
resource "huaweicloud_identity_agency" "function" {
  name        = "${local.name_prefix}-agency-${local.suffix}"
  description = "Lets the FunctionGraph function manage ELB members and read RDS/OBS"

  # 固定值，表示把权限委托给函数工作流服务。
  delegated_domain_name = "op_svc_cff"

  project_role {
    project = var.region
    roles   = local.agency_project_roles
  }

  # 对象存储是全局服务，权限只能在这里授予，放进 project_role 不生效。
  domain_roles = var.agency_domain_roles
}

# ---------------------------------------------------------------------------
# 日志（LTS 日志服务）
# ---------------------------------------------------------------------------
resource "huaweicloud_lts_group" "this" {
  group_name  = "${local.name_prefix}-log-group-${local.suffix}"
  ttl_in_days = var.log_ttl_in_days

  tags = {
    workload = "rds-read-replica-elb-registration"
  }
}

resource "huaweicloud_lts_stream" "this" {
  group_id    = huaweicloud_lts_group.this.id
  stream_name = "${local.name_prefix}-log-stream-${local.suffix}"
}

# ---------------------------------------------------------------------------
# 函数（FunctionGraph 函数工作流）
# ---------------------------------------------------------------------------
data "archive_file" "function" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/.build/function.zip"
  excludes    = ["__pycache__", "*.pyc"]
}

resource "huaweicloud_fgs_function" "this" {
  name        = "${local.name_prefix}-registrar-${local.suffix}"
  description = "Keeps the ELB backend server group in sync with the RDS read replicas"
  app         = "default"

  runtime     = var.function_runtime
  handler     = "index.handler"
  memory_size = var.function_memory_size
  timeout     = var.function_timeout

  code_type     = "zip"
  code_filename = "function.zip"
  func_code     = filebase64(data.archive_file.function.output_path)

  # 委托同时用于两件事：ELB / RDS 调用取 token，对象存储调用取临时 AK-SK。
  agency = huaweicloud_identity_agency.function.name

  # 只有 dns 模式需要进 VPC——RDS 内网域名只在 VPC 内可解析。
  # rds_api 模式下函数走公网 API 端点，不占用子网 IP，也不需要 NAT 网关。
  vpc_id     = local.needs_vpc_access ? local.vpc_id : null
  network_id = local.needs_vpc_access ? local.subnet_id : null

  enable_lts_log  = true
  log_group_id    = huaweicloud_lts_group.this.id
  log_group_name  = huaweicloud_lts_group.this.group_name
  log_stream_id   = huaweicloud_lts_stream.this.id
  log_stream_name = huaweicloud_lts_stream.this.stream_name

  enterprise_project_id = var.enterprise_project_id != "" ? var.enterprise_project_id : null

  # 全部配置都以函数环境变量下发，代码里没有任何硬编码的账号或资源 ID。
  # 这里不放任何密钥——凭据一律运行期从委托换取。
  user_data = jsonencode({
    DISCOVERY_MODE                    = var.discovery_mode
    RDS_REPLICA_IDS                   = join(",", local.replica_ids)
    RDS_REPLICA_DNS_NAMES             = join(",", local.replica_dns_names)
    RDS_REQUIRE_ACTIVE_STATUS         = var.rds_require_active_status ? "true" : "false"
    RDS_LISTENER_PORT                 = tostring(var.rds_listener_port)
    STATE_PREFIX                      = var.state_prefix
    OBS_BUCKET                        = huaweicloud_obs_bucket.state.bucket
    ELB_POOL_ID                       = data.huaweicloud_elb_pools.this.pools[0].id
    REGION                            = var.region
    CLOUD_DOMAIN                      = var.cloud_domain
    SUBNET_CIDR_ID                    = local.subnet_cidr_id
    MAX_LOOKUP_PER_INVOCATION         = tostring(var.max_lookup_per_invocation)
    INVOCATIONS_BEFORE_DEREGISTRATION = tostring(var.invocations_before_deregistration)
    MAX_DEREGISTRATION_PERCENT        = tostring(var.max_deregistration_percent)
  })

  lifecycle {
    # 控制台上手工改过的并发配置不该被 apply 抹掉。
    ignore_changes = [
      max_instance_num,
    ]
  }
}

# 定时触发器 —— Rate 类型，默认 1 分钟。
resource "huaweicloud_fgs_function_trigger" "timer" {
  function_urn = huaweicloud_fgs_function.this.urn
  type         = "TIMER"

  event_data = jsonencode({
    name          = "${local.name_prefix}-timer-${local.suffix}"
    schedule_type = "Rate"
    schedule      = var.schedule
    user_event    = "Managed by Terraform"
  })

  # FunctionGraph 拒绝更新处于 ACTIVE 状态的 TIMER 触发器（FSS.1153 / 412），
  # 而 event_data 的 JSON 序列化在 config 与远端返回值之间存在固有格式差异，
  # 每次 plan 都会误判为需 update。忽略这些不可变字段，避免 apply 撞 412；
  # 若日后真要改 schedule，请先 taint 本资源再 apply。
  lifecycle {
    ignore_changes = [event_data, status]
  }
}
