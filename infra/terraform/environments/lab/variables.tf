variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "allowed_account_id" {
  type        = string
  description = "Fail closed if credentials point at another AWS account."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.allowed_account_id))
    error_message = "allowed_account_id must be a 12-digit AWS account ID."
  }
}

variable "availability_zone" {
  type        = string
  description = "Explicit AZ used by both EC2 and the lab EBS volume."
}

variable "ami_id" {
  type        = string
  description = "Pinned Canonical Ubuntu 24.04 x86_64 AMI."
  validation {
    condition     = can(regex("^ami-[a-f0-9]+$", var.ami_id))
    error_message = "ami_id must be an EC2 AMI ID."
  }
}

variable "instance_type" {
  type    = string
  default = "t3.micro"
  validation {
    condition     = contains(["t3.micro", "t3.small"], var.instance_type)
    error_message = "The disposable lab allows only t3.micro or t3.small."
  }
}

variable "release_bucket" {
  type = string
}

variable "release_key" {
  type = string
  validation {
    condition     = can(regex("^releases/[a-f0-9]{64}/[A-Za-z0-9_.-]+\\.whl$", var.release_key))
    error_message = "release_key must be a content-addressed wheel path."
  }
}

variable "release_sha256" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{64}$", var.release_sha256))
    error_message = "release_sha256 must be a lowercase SHA-256 digest."
  }
}

variable "owner" {
  type    = string
  default = "qingyu"
}

variable "expiry" {
  type = string
}
