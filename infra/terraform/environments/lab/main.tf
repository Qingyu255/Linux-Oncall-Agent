data "aws_caller_identity" "current" {}

data "aws_ami" "ubuntu" {
  owners = ["099720109477"]
  filter {
    name   = "image-id"
    values = [var.ami_id]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
}

locals {
  name = "linux-oncall-day2"
  tags = {
    Project     = "linux-oncall-agent"
    Owner       = var.owner
    Environment = "disposable-lab"
    ExpiresAt   = var.expiry
    ManagedBy   = "terraform"
  }
  parameter_prefix = "/linux-oncall"
}

resource "aws_vpc" "lab" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${local.name}-vpc" }
}

resource "aws_internet_gateway" "lab" {
  vpc_id = aws_vpc.lab.id
  tags   = { Name = "${local.name}-igw" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.lab.id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = true
  tags                    = { Name = "${local.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.lab.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.lab.id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "target" {
  name_prefix = "${local.name}-"
  description = "Zero ingress; broad egress for disposable bootstrap and SSM"
  vpc_id      = aws_vpc.lab.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-target" }
}

resource "aws_iam_role" "target" {
  name_prefix = "${local.name}-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.target.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "bootstrap" {
  name = "oncall-bootstrap"
  role = aws_iam_role.target.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadPinnedRelease"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.release_bucket}/${var.release_key}"
      },
      {
        Sid      = "PublishEphemeralEnrollment"
        Effect   = "Allow"
        Action   = ["ssm:PutParameter"]
        Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.parameter_prefix}/*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "target" {
  name_prefix = "${local.name}-"
  role        = aws_iam_role.target.name
}

resource "aws_ebs_volume" "lab" {
  availability_zone = var.availability_zone
  size              = 1
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "${local.name}-data", Purpose = "bounded-fault-data" }
}

resource "aws_instance" "target" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  availability_zone           = var.availability_zone
  subnet_id                   = aws_subnet.public.id
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.target.id]
  iam_instance_profile        = aws_iam_instance_profile.target.name
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/../../templates/cloud-init.sh.tftpl", {
    region            = var.region
    release_bucket    = var.release_bucket
    release_key       = var.release_key
    release_sha256    = var.release_sha256
    lab_volume_serial = replace(aws_ebs_volume.lab.id, "-", "")
    parameter_prefix  = local.parameter_prefix
  })

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 8
    encrypted             = true
    delete_on_termination = true
  }

  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.allowed_account_id
      error_message = "AWS caller account does not match allowed_account_id."
    }
  }

  tags = { Name = "${local.name}-target", Role = "probe-target" }
}

resource "aws_volume_attachment" "lab" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.lab.id
  instance_id = aws_instance.target.id
}

resource "aws_ssm_document" "fixed_port" {
  name            = "LinuxOncallFixedPort-${aws_instance.target.id}"
  document_type   = "Session"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "1.0"
    description   = "Forward only the Linux OnCall loopback probe port"
    sessionType   = "Port"
    parameters = {
      portNumber = {
        type           = "String"
        default        = "8765"
        allowedPattern = "^8765$"
      }
      localPortNumber = {
        type           = "String"
        default        = "18765"
        allowedPattern = "^18765$"
      }
    }
    properties = {
      portNumber      = "{{ portNumber }}"
      type            = "LocalPortForwarding"
      localPortNumber = "{{ localPortNumber }}"
    }
  })

  tags = { Name = "${local.name}-fixed-port" }
}
