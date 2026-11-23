terraform {
  # terraform_data 用于把配置自检提前到 plan 阶段，该资源自 1.4 起提供。
  required_version = ">= 1.4.0"

  required_providers {
    huaweicloud = {
      source  = "huaweicloud/huaweicloud"
      version = ">= 1.60.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5.0"
    }
  }
}

# ---------------------------------------------------------------------------
# 认证配置
#
# 认证信息一律通过环境变量提供，不写入 .tf / .tfvars / 版本库：
#   export HW_ACCESS_KEY=<AK>
#   export HW_SECRET_KEY=<SK>
#   export HW_REGION_NAME=<region>
# ---------------------------------------------------------------------------
provider "huaweicloud" {
  region = var.region
}

resource "random_id" "suffix" {
  byte_length = 4
}

# 子网同时存在两种 ID，用途不同，混用会导致 ELB 创建失败：
#   - 子网（网络）ID     供 FunctionGraph network_id / RDS 使用
#   - ipv4_subnet_id     原生 OpenStack IPv4 子网 ID，供 ELB ipv4_subnet_id
#                        与后端服务器 subnet_cidr_id 使用
# create_vpc = true 时由 bootstrap.tf 自建子网，不再查既有子网。
data "huaweicloud_vpc_subnet" "this" {
  count = var.create_vpc ? 0 : 1

  id = var.subnet_id
}
