# ===========================================================================
# 从零搭建演示环境的实跑参数（华南-广州 cn-south-1，账号原本为空）
#
# 凭据一律走环境变量，不写进本文件：
#   export HW_ACCESS_KEY=<AK>
#   export HW_SECRET_KEY=<SK>
#   export HW_USER_NAME=<IAM 用户名>
#   export HW_DOMAIN_ID=<账号 ID>        # 注意：用账号 ID，不是账号名
# ===========================================================================

region = "cn-south-1"

# 从零搭环境：VPC / 子网 / 安全组 / RDS 主实例全部由模板创建
create_vpc         = true
create_rds_primary = true

# 网络
vpc_cidr  = "192.168.0.0/16"
subnet_cidr = "192.168.1.0/24"

# RDS 主实例（MySQL 8.0，单节点）
rds_primary_flavor            = "rds.mysql.n1.large.2"
rds_primary_engine            = "MySQL"
rds_primary_version           = "8.0"
rds_primary_volume_type       = "CLOUDSSD"
rds_primary_volume_size       = 40
rds_primary_availability_zones = ["cn-south-1c"]

# 自动派生 1 个只读副本（.rr 后缀表示只读副本规格）
rds_replica_count              = 1
rds_replica_flavor             = "rds.mysql.n1.large.2.rr"
rds_replica_volume_type        = "CLOUDSSD"
rds_replica_availability_zones = ["cn-south-1e"]

# 负载均衡（单可用区即可，弹性规格空闲极低）
elb_availability_zones = ["cn-south-1c"]

# 其余使用 variables.tf 中的默认值：
#   discovery_mode = "rds_api"（默认，不挂 VPC、不配 NAT、成本最低）
#   schedule = "1m"
#   function_timeout = 45
#   obs_force_destroy = true
#   log_ttl_in_days = 7
