output "inventory" {
  value = {
    disposable         = true
    account_id         = data.aws_caller_identity.current.account_id
    region             = var.region
    availability_zone  = var.availability_zone
    instance_id        = aws_instance.target.id
    instance_type      = aws_instance.target.instance_type
    ami_id             = aws_instance.target.ami
    vpc_id             = aws_vpc.lab.id
    subnet_id          = aws_subnet.public.id
    security_group_id  = aws_security_group.target.id
    lab_volume_id      = aws_ebs_volume.lab.id
    session_document   = aws_ssm_document.fixed_port.name
    parameter_name     = "${local.parameter_prefix}/${aws_instance.target.id}/target-token"
    probe_port         = 8765
    local_forward_port = 18765
  }
  description = "Non-secret target inventory used by the trusted operator."
}
