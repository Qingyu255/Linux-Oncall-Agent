variable "region" {
  type        = string
  description = "AWS region for the disposable lab."
  default     = "ap-southeast-1"
}

variable "project" {
  type        = string
  description = "Project tag and resource-name prefix."
  default     = "linux-oncall-agent"
}

variable "owner" {
  type        = string
  description = "Human-readable owner tag."
  default     = "qingyu"
}

variable "expiry" {
  type        = string
  description = "Operator-visible expiry tag; cleanup remains explicit."
}
