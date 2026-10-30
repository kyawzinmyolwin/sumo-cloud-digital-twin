# On-demand EC2 host: runs SUMO + the emitter, forwarding live snapshots to the
# WebSocket API Gateway. Gated by var.sim_host_enabled so it costs nothing unless
# you're demoing. Access via SSM Session Manager (no open SSH ports).

data "aws_ami" "ubuntu" {
  count       = var.sim_host_enabled ? 1 : 0
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
}

data "aws_vpc" "default" {
  count   = var.sim_host_enabled ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = var.sim_host_enabled ? 1 : 0
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
}

# Outbound only — the producer dials OUT to API Gateway; SSM is outbound too.
resource "aws_security_group" "sim_host" {
  count       = var.sim_host_enabled ? 1 : 0
  name        = "${local.name_prefix}-sim-host"
  description = "SUMO producer: outbound only"
  vpc_id      = data.aws_vpc.default[0].id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# IAM role so SSM Session Manager can reach the box (no SSH keys or open ports).
resource "aws_iam_role" "sim_host" {
  count = var.sim_host_enabled ? 1 : 0
  name  = "${local.name_prefix}-sim-host"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "sim_host_ssm" {
  count      = var.sim_host_enabled ? 1 : 0
  role       = aws_iam_role.sim_host[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "sim_host" {
  count = var.sim_host_enabled ? 1 : 0
  name  = "${local.name_prefix}-sim-host"
  role  = aws_iam_role.sim_host[0].name
}

resource "aws_instance" "sim_host" {
  count                       = var.sim_host_enabled ? 1 : 0
  ami                         = data.aws_ami.ubuntu[0].id
  instance_type               = var.sim_host_instance_type
  subnet_id                   = data.aws_subnets.default[0].ids[0]
  vpc_security_group_ids      = [aws_security_group.sim_host[0].id]
  iam_instance_profile        = aws_iam_instance_profile.sim_host[0].name
  associate_public_ip_address = true
  user_data_replace_on_change = true

  user_data = templatefile("${path.module}/sim_host_userdata.sh.tftpl", {
    ws_url  = aws_apigatewayv2_stage.prod.invoke_url
    git_ref = var.sim_host_git_ref
  })

  tags = { Name = "${local.name_prefix}-sim-host" }
}