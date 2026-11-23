# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
variable "region" {
  description = "资源所在区域，例如 cn-north-4。"
  type        = string
}

variable "name_prefix" {
  description = "所有资源名称的统一前缀。"
  type        = string
  default     = "rds-rr-elb"
}

variable "cloud_domain" {
  description = <<-EOT
    云服务域名后缀，函数据此拼接 ELB / RDS / IAM / OBS 的 API 端点。
    中国站为 myhuaweicloud.com；国际站按局点调整（如 myhuaweicloud.eu）。
  EOT
  type        = string
  default     = "myhuaweicloud.com"
}

variable "enterprise_project_id" {
  description = "企业项目 ID，留空表示 default 企业项目。"
  type        = string
  default     = ""
}

variable "vpc_id" {
  description = <<-EOT
    负载均衡器所在 VPC 的 ID。
    create_vpc = true 时留空，模板会自建 VPC。
  EOT
  type    = string
  default = ""
}

variable "subnet_id" {
  description = <<-EOT
    子网 ID（网络 ID，即 huaweicloud_vpc_subnet 的 id）。
    ELB 所需的 ipv4_subnet_id 会自动从该子网查出，无需另行填写。
    create_vpc = true 时留空，模板会自建子网。
  EOT
  type    = string
  default = ""
}

variable "security_group_id" {
  description = <<-EOT
    安全组 ID，RDS 主实例使用。
    create_vpc = true 时留空，模板会自建安全组。
    仅在 create_rds_primary = true 且 create_vpc = false 时需要提供。
  EOT
  type    = string
  default = ""
}

# ---------------------------------------------------------------------------
# 只读副本发现
# ---------------------------------------------------------------------------
variable "discovery_mode" {
  description = <<-EOT
    只读副本 IP 的发现方式：

      rds_api（默认）：调用 RDS API 读取只读实例的 private_ips。
                       函数无需接入 VPC，也不需要 NAT 网关。
      dns：            解析只读副本的内网域名。
                       华为云 RDS 内网域名仅在 VPC 内可解析，因此该模式下
                       函数必须接入 VPC（本模板会自动接入）。
  EOT
  type        = string
  default     = "rds_api"

  validation {
    condition     = contains(["rds_api", "dns"], var.discovery_mode)
    error_message = "discovery_mode 只能是 rds_api 或 dns。"
  }
}

variable "rds_replica_ids" {
  description = <<-EOT
    已有 RDS 只读实例的 ID 列表，rds_api 模式使用。
    当 create_rds_replicas = true 时留空，模板会用新建副本的 ID 自动填充。
  EOT
  type        = list(string)
  default     = []
}

variable "rds_replica_dns_names" {
  description = <<-EOT
    只读副本的域名列表，dns 模式使用。
    当 create_rds_replicas = true 时留空，模板会用新建副本的内网域名自动填充。
  EOT
  type        = list(string)
  default     = []
}

variable "rds_require_active_status" {
  description = <<-EOT
    为 true 时，仅把状态为 ACTIVE 的只读实例视为在册。
    默认 false——实例重启期间 DNS 记录依然解析，因此不应立即摘除，
    交由审慎摘除机制处理。
  EOT
  type        = bool
  default     = false
}

variable "rds_listener_port" {
  description = "RDS 实例的监听端口。"
  type        = number
  default     = 3306
}

# ---------------------------------------------------------------------------
# 可选：创建演示用只读副本
# ---------------------------------------------------------------------------
variable "create_rds_replicas" {
  description = <<-EOT
    是否创建演示用的只读副本。默认 false。
    注意：只读实例按规格与存储计费，开启前请确认所选规格与存储容量。
  EOT
  type        = bool
  default     = false
}

variable "rds_primary_instance_id" {
  description = "主实例 ID，create_rds_replicas = true 时必填。"
  type        = string
  default     = ""
}

variable "rds_replica_count" {
  description = "创建的只读副本数量。"
  type        = number
  default     = 2
}

variable "rds_replica_flavor" {
  description = <<-EOT
    只读副本规格码，例如 rds.mysql.n1.large.2.rr。
    不提供默认值：规格直接决定单价，必须由使用者显式选定。
  EOT
  type        = string
  default     = ""
}

variable "rds_replica_volume_type" {
  description = "只读副本存储类型，须与主实例一致。"
  type        = string
  default     = "CLOUDSSD"
}

variable "rds_replica_volume_size" {
  description = <<-EOT
    只读副本存储容量（GB）。0 表示继承主实例容量。
    显式指定前请确认所需容量。
  EOT
  type        = number
  default     = 0
}

variable "rds_replica_availability_zones" {
  description = "只读副本所在可用区列表，按副本序号依次取用。"
  type        = list(string)
  default     = []
}

# ---------------------------------------------------------------------------
# 可选：从零搭建演示环境（账号原本没有 VPC / 子网 / RDS 时）
#
# 对应「账号为空、由模板一次性建出可运行环境」的场景。生产环境通常已有这些
# 资源，保持关闭即可，直接把既有资源 ID 传入上面的相应变量。
# 注意：RDS 主实例 + 只读副本按规格与存储计费，开启前请确认所选规格与容量。
# ---------------------------------------------------------------------------
variable "create_vpc" {
  description = "是否自建演示用 VPC / 子网 / 安全组。默认 false，使用账号已有网络。"
  type        = bool
  default     = false
}

variable "vpc_cidr" {
  description = "自建 VPC 的 CIDR。"
  type        = string
  default     = "192.168.0.0/16"
}

variable "subnet_cidr" {
  description = "自建子网的 CIDR（必须落在 vpc_cidr 内）。"
  type        = string
  default     = "192.168.1.0/24"
}

variable "create_rds_primary" {
  description = <<-EOT
    是否自建演示用 RDS 主实例（MySQL）。默认 false。
    开启后会自动派生只读副本（create_rds_replicas 视为 true），
    从而无需在别处准备 RDS 即可跑通端到端演示。
    主实例管理员密码由 random_password 生成，不落明文，apply 后从输出来取。
  EOT
  type        = bool
  default     = false
}

variable "rds_primary_flavor" {
  description = <<-EOT
    主实例规格码，例如 rds.mysql.n1.large.2。
    不提供默认值：规格直接决定单价，必须由使用者显式选定。
    create_rds_primary = true 时必填。
  EOT
  type        = string
  default     = ""
}

variable "rds_primary_engine" {
  description = "主实例数据库引擎。"
  type        = string
  default     = "MySQL"
}

variable "rds_primary_version" {
  description = "主实例数据库版本。"
  type        = string
  default     = "8.0"
}

variable "rds_primary_volume_type" {
  description = "主实例存储类型。"
  type        = string
  default     = "CLOUDSSD"
}

variable "rds_primary_volume_size" {
  description = "主实例存储容量（GB）。"
  type        = number
  default     = 40
}

variable "rds_primary_availability_zones" {
  description = "主实例所在可用区列表，按实例序号取用。"
  type        = list(string)
  default     = []
}

# ---------------------------------------------------------------------------
# 负载均衡
# ---------------------------------------------------------------------------
variable "elb_availability_zones" {
  description = <<-EOT
    负载均衡器的可用区列表。
    可用区数量是 ELB 费用的直接乘数；独享型 ELB 本身支持跨可用区转发到后端，
    因此单可用区部署不会削弱后端的多可用区容灾能力。
  EOT
  type        = list(string)
}

variable "elb_flavor_type" {
  description = "四层规格类型。L4_elastic_max 为弹性规格，按实际 LCU 用量计费。"
  type        = string
  default     = "L4_elastic_max"
}

variable "elb_flavor_name" {
  description = <<-EOT
    四层规格名称，必须精确指定。
    绝不使用 data.huaweicloud_elb_flavors.*.ids[N] 让系统随机挑选：
    该数据源按 flavor UUID 字典序返回，顺序与规格大小和价格无关。
  EOT
  type        = string
  default     = "L4_flavor.elb.pro.max"
}

variable "cross_vpc_backend" {
  description = <<-EOT
    只读副本是否位于负载均衡器所在 VPC 之外。

      false（默认）：后端与负载均衡器同 VPC。注册后端时必须带上后端所在子网的
                     原生 IPv4 子网 ID，模板会自动查出并注入函数。
      true：         后端跨 VPC。注册后端时不能带子网 ID，同时负载均衡器必须
                     开启跨 VPC 后端能力，模板会自动开启。

    false 表示后端与负载均衡器位于同一 VPC，true 表示跨 VPC；请按副本实际所在网络选择。
  EOT
  type        = bool
  default     = false
}

variable "backend_subnet_id" {
  description = <<-EOT
    只读副本所在子网的 ID（网络 ID）。留空表示与 subnet_id 相同。
    仅在 cross_vpc_backend = false 时生效。
  EOT
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# 委托授权
# ---------------------------------------------------------------------------
variable "agency_project_roles" {
  description = <<-EOT
    授予函数委托的区域级策略名称，默认已覆盖运行所需的最小集合：
      ELB FullAccess      —— 增删后端服务器组成员
      RDS ReadOnlyAccess  —— 查询只读实例的 private_ips（rds_api 模式）
      VPC ReadOnlyAccess  —— ELB 细粒度策略的依赖项

    dns 模式下函数需接入 VPC，模板会自动追加 VPC FullAccess。
    若账号内策略名称与此处不同，请按实际名称覆盖。
  EOT
  type        = list(string)
  default     = ["ELB FullAccess", "RDS ReadOnlyAccess", "VPC ReadOnlyAccess"]
}

variable "agency_domain_roles" {
  description = <<-EOT
    授予函数委托的全局服务策略名称。
    OBS 是全局服务，其权限必须在这里授予，写进区域级策略不生效。

    注意：OBS 的细粒度角色（OBS OperateAccess / OBS ReadOnlyAccess）是
    项目级角色，作为 agency 的「全局角色」授予时不会生效，会报 403
    AccessDenied。agency 访问 OBS 必须使用域级角色 OBS Administrator。
  EOT
  type        = list(string)
  default     = ["OBS Administrator"]
}

variable "health_check_interval" {
  description = "健康检查间隔（秒）。"
  type        = number
  default     = 5
}

variable "health_check_timeout" {
  description = "健康检查超时（秒）。"
  type        = number
  default     = 3
}

variable "health_check_max_retries" {
  description = "判定后端健康所需的连续成功次数。"
  type        = number
  default     = 3
}

# ---------------------------------------------------------------------------
# 状态存储
# ---------------------------------------------------------------------------
variable "obs_bucket_name" {
  description = "状态桶名称，留空则自动生成。桶名全局唯一。"
  type        = string
  default     = ""
}

variable "obs_force_destroy" {
  description = "destroy 时是否连同桶内对象一并删除。演示环境建议 true。"
  type        = bool
  default     = true
}

variable "obs_versioning" {
  description = <<-EOT
    是否为状态桶开启版本控制。
    开启后能回溯误摘除当时的状态文件，代价是历史版本会留在桶里，
    destroy 时需要额外清理。演示环境建议保持关闭。
  EOT
  type        = bool
  default     = false
}

variable "state_prefix" {
  description = "状态文件在桶内的键前缀，最终写入 <prefix>/active_ip.json 与 <prefix>/pending_ip.json。"
  type        = string
  default     = "rds-read-replica"
}

# ---------------------------------------------------------------------------
# 函数与调度
# ---------------------------------------------------------------------------
variable "schedule" {
  description = <<-EOT
    定时触发周期，Rate 类型。取值形如 1m / 5m / 1h / 1d。
    默认 1m。
  EOT
  type        = string
  default     = "1m"
}

variable "function_memory_size" {
  description = "函数内存（MB）。FunctionGraph 的 CPU 配额与内存成正比。"
  type        = number
  default     = 256
}

variable "function_timeout" {
  description = "函数超时（秒）。须小于调度周期，避免相邻两次执行重叠。"
  type        = number
  default     = 45
}

variable "function_runtime" {
  description = "函数运行时。"
  type        = string
  default     = "Python3.9"
}

variable "max_lookup_per_invocation" {
  description = "单次执行中每个副本的最大发现重试次数。"
  type        = number
  default     = 3
}

variable "invocations_before_deregistration" {
  description = <<-EOT
    某个 IP 需要连续多少次执行都处于"已不在册"状态，才会被真正摘除。
    这是抵御瞬时抖动的核心参数，调低会更快摘除、也更容易误摘。
  EOT
  type        = number
  default     = 3
}

variable "max_deregistration_percent" {
  description = <<-EOT
    单次执行允许摘除的后端占比上限（百分比）。
    超过该比例会触发熔断，跳过本次摘除并告警，防止一次性摘空整个后端组。
  EOT
  type        = number
  default     = 50
}

variable "log_ttl_in_days" {
  description = "LTS 日志保留天数。"
  type        = number
  default     = 7
}

variable "account_id" {
  description = <<-EOT
    华为云账号 ID（domain id）。用于给 OBS 桶写一条桶策略，
    显式允许函数委托（agency）的临时凭证读写对象。
    注意：OBS 数据面（getObject/putObject）不认 IAM 角色授权，
    必须靠桶策略放行，否则函数会一直 403。此变量不出现在任何
    凭据语义里，仅为资源 ARN 的命名前缀。
  EOT
  type        = string
  default     = "72f076e16a7a4eb49c97cbf59a533c3b"
}
