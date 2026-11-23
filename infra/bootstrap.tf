# ===========================================================================
# 可选：从零搭建演示环境
#
# 账号原本没有 VPC / 子网 / RDS 时，由本文件一次性建出可运行环境：
#   VPC → 子网 → 安全组（放行 RDS 端口）→ RDS 主实例（MySQL）
#
# 仅当 create_vpc / create_rds_primary 为真时创建；其余时间保持关闭，
# 直接把既有资源 ID 传入主配置即可。
# ===========================================================================

resource "huaweicloud_vpc" "this" {
  count = var.create_vpc ? 1 : 0

  name = "${local.name_prefix}-vpc-${local.suffix}"
  cidr = var.vpc_cidr

  tags = {
    workload = "rds-read-replica-elb-registration"
  }
}

resource "huaweicloud_vpc_subnet" "this" {
  count = var.create_vpc ? 1 : 0

  vpc_id         = huaweicloud_vpc.this[0].id
  name           = "${local.name_prefix}-subnet-${local.suffix}"
  cidr           = var.subnet_cidr
  gateway_ip     = cidrhost(var.subnet_cidr, 1)
  # 子网必须落到某个可用区，取 ELB 的第一个可用区即可。
  availability_zone = element(var.elb_availability_zones, 0)

  tags = {
    workload = "rds-read-replica-elb-registration"
  }
}

resource "huaweicloud_networking_secgroup" "this" {
  count = var.create_vpc ? 1 : 0

  name = "${local.name_prefix}-sg-${local.suffix}"

  tags = {
    workload = "rds-read-replica-elb-registration"
  }
}

# 仅放行本子网内对 RDS 端口的访问，演示用最小暴露面。
resource "huaweicloud_networking_secgroup_rule" "rds_ingress" {
  count = var.create_vpc ? 1 : 0

  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = var.rds_listener_port
  port_range_max    = var.rds_listener_port
  remote_ip_prefix  = var.subnet_cidr
  security_group_id = huaweicloud_networking_secgroup.this[0].id
}

# 主实例管理员密码由 Terraform 生成，不落明文；apply 后从 rds_primary_password 输出来取。
resource "random_password" "rds_admin" {
  count = var.create_rds_primary ? 1 : 0

  length           = 16
  upper            = true
  lower            = true
  numeric          = true
  special          = true
  override_special = "!@#%^&*()-_=+"
}

# RDS 主实例（单节点）。仅 create_rds_primary 为真时创建。
resource "huaweicloud_rds_instance" "primary" {
  count = var.create_rds_primary ? 1 : 0

  name              = "${local.name_prefix}-primary-${local.suffix}"
  flavor            = var.rds_primary_flavor
  vpc_id            = local.vpc_id
  subnet_id         = local.subnet_id
  security_group_id = local.security_group_id

  availability_zone = [element(var.rds_primary_availability_zones, 0)]

  volume {
    type = var.rds_primary_volume_type
    size = var.rds_primary_volume_size
  }

  db {
    type     = var.rds_primary_engine
    version  = var.rds_primary_version
    password = random_password.rds_admin[0].result
    port     = var.rds_listener_port
  }

  tags = {
    workload = "rds-read-replica-elb-registration"
  }
}
