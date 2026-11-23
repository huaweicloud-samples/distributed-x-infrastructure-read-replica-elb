output "loadbalancer_id" {
  description = "独享型负载均衡器 ID。"
  value       = huaweicloud_elb_loadbalancer.this.id
}

output "loadbalancer_private_ip" {
  description = <<-EOT
    负载均衡器的内网 IPv4 地址。
    这就是应用侧的只读流量入口——把只读连接串指向它，副本增减对应用透明。
  EOT
  value       = huaweicloud_elb_loadbalancer.this.ipv4_address
}

output "listener_port" {
  description = "监听端口，与 RDS 实例端口一致。"
  value       = huaweicloud_elb_listener.this.protocol_port
}

output "pool_id" {
  description = <<-EOT
    IP 类型后端服务器组 ID（由 REST 脚本创建，经 huaweicloud_elb_pools 查回）。
    排查时可用它直接查当前注册了哪些副本 IP：
      hcloud ELB ListMembers --pool_id=<pool_id>
  EOT
  value       = data.huaweicloud_elb_pools.this.pools[0].id
}

output "function_urn" {
  description = "函数 URN。手工触发一次可用于验证：控制台 → 函数 → 测试。"
  value       = huaweicloud_fgs_function.this.urn
}

output "function_name" {
  description = "函数名称。"
  value       = huaweicloud_fgs_function.this.name
}

output "state_bucket" {
  description = <<-EOT
    状态桶名称。桶内两个对象记录了整个状态机的现场：
      <state_prefix>/active_ip.json   已注册的副本 IP
      <state_prefix>/pending_ip.json  连续缺失中、尚未达到摘除阈值的 IP
  EOT
  value       = huaweicloud_obs_bucket.state.bucket
}

output "state_object_keys" {
  description = "状态文件在桶内的完整键名。"
  value = {
    active  = "${var.state_prefix}/active_ip.json"
    pending = "${var.state_prefix}/pending_ip.json"
  }
}

output "log_group_name" {
  description = "日志组名称，函数运行日志在这里。"
  value       = huaweicloud_lts_group.this.group_name
}

output "log_stream_name" {
  description = "日志流名称。"
  value       = huaweicloud_lts_stream.this.stream_name
}

output "agency_name" {
  description = "函数使用的委托名称。"
  value       = huaweicloud_identity_agency.function.name
}

output "discovery_mode" {
  description = "当前生效的副本发现方式。"
  value       = var.discovery_mode
}

output "monitored_replica_ids" {
  description = "函数正在监视的只读副本 ID 列表（rds_api 模式使用）。"
  value       = local.replica_ids
}

output "monitored_replica_dns_names" {
  description = "函数正在监视的只读副本域名列表（dns 模式使用）。"
  value       = local.replica_dns_names
}

# ---------------------------------------------------------------------------
# 仅自建环境时才有值（否则为 null / 空），便于从输出里拿到连接信息。
# ---------------------------------------------------------------------------
output "created_vpc_id" {
  description = "自建 VPC 的 ID（create_vpc = true 时）。"
  value       = var.create_vpc ? huaweicloud_vpc.this[0].id : null
}

output "created_subnet_id" {
  description = "自建子网的 ID（create_vpc = true 时）。"
  value       = var.create_vpc ? huaweicloud_vpc_subnet.this[0].id : null
}

output "rds_primary_id" {
  description = "自建 RDS 主实例 ID（create_rds_primary = true 时）。"
  value       = var.create_rds_primary ? huaweicloud_rds_instance.primary[0].id : null
}

output "rds_primary_password" {
  description = <<-EOT
    自建 RDS 主实例的管理员密码（create_rds_primary = true 时）。
    由 Terraform 随机生成，不落明文；仅在此输出中可见一次，请妥善保存。
  EOT
  value       = var.create_rds_primary ? random_password.rds_admin[0].result : null
  sensitive   = true
}

output "rds_primary_endpoint" {
  description = "自建 RDS 主实例的内网连接地址（create_rds_primary = true 时）。"
  value       = var.create_rds_primary ? huaweicloud_rds_instance.primary[0].private_ips : null
}
