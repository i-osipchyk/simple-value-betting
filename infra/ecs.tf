# ── ECS Cluster ───────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = local.common_tags
}

# ── EC2 Launch Template + ASG ─────────────────────────────────────────────────

data "aws_ssm_parameter" "ecs_ami" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2/recommended/image_id"
}

resource "aws_launch_template" "ecs" {
  name_prefix   = "${local.name_prefix}-ecs-"
  image_id      = data.aws_ssm_parameter.ecs_ami.value
  instance_type = var.ec2_instance_type

  iam_instance_profile {
    arn = aws_iam_instance_profile.ecs.arn
  }

  vpc_security_group_ids = [aws_security_group.ecs.id]

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = var.environment == "prod" ? 30 : 20
      volume_type           = "gp3"
      delete_on_termination = false
      encrypted             = true
    }
  }

  user_data = base64encode(<<-EOF
    #!/bin/bash
    echo ECS_CLUSTER=${aws_ecs_cluster.main.name} >> /etc/ecs/ecs.config
    mkdir -p /opt/app/data/raw /opt/app/data/resolutions /opt/app/data/models /opt/app/data/trades
  EOF
  )

  metadata_options {
    http_tokens = "required"
  }

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name   = "${local.name_prefix}-ecs-instance"
      Backup = "true"
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(local.common_tags, { Backup = "true" })
  }

  tags = local.common_tags
}

resource "aws_autoscaling_group" "ecs" {
  name                = "${local.name_prefix}-ecs-asg"
  min_size            = 1
  max_size            = 1
  desired_capacity    = 1
  vpc_zone_identifier = data.aws_subnets.default.ids

  launch_template {
    id      = aws_launch_template.ecs.id
    version = "$Latest"
  }

  protect_from_scale_in = true

  tag {
    key                 = "AmazonECSManaged"
    value               = ""
    propagate_at_launch = true
  }

  dynamic "tag" {
    for_each = merge(local.common_tags, { Name = "${local.name_prefix}-ecs-instance" })
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [desired_capacity]
  }
}

# ── ECS Capacity Provider ─────────────────────────────────────────────────────

resource "aws_ecs_capacity_provider" "ec2" {
  name = "${local.name_prefix}-cp"

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.ecs.arn
    managed_termination_protection = "ENABLED"

    managed_scaling {
      status          = "ENABLED"
      target_capacity = 100
    }
  }

  tags = local.common_tags
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = [aws_ecs_capacity_provider.ec2.name]

  default_capacity_provider_strategy {
    base              = 1
    weight            = 100
    capacity_provider = aws_ecs_capacity_provider.ec2.name
  }
}

# ── Bootstrap Task Definition ─────────────────────────────────────────────────
# Uses :latest images so the service can be created before the first CI deploy.
# GitHub Actions will register a new revision with the SHA-tagged images and
# update the service; the ignore_changes below prevents Terraform from reverting.

resource "aws_ecs_task_definition" "main" {
  family             = "${local.name_prefix}-task"
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.ecs_task.arn
  network_mode       = "bridge"

  requires_compatibilities = ["EC2"]

  container_definitions = jsonencode([
    {
      name      = "collector"
      image     = "${aws_ecr_repository.collector.repository_url}:latest"
      essential = true
      mountPoints = [{
        sourceVolume  = "data"
        containerPath = "/data"
        readOnly      = false
      }]
      environment = [
        { name = "ENV", value = "aws" },
        { name = "LOCAL_DATA_DIR", value = "/data" },
        { name = "AWS_BUCKET", value = var.s3_bucket_name },
        { name = "AWS_REGION", value = var.aws_region },
        { name = "PM_WS_URL", value = "wss://ws-subscriptions-clob.polymarket.com" },
        { name = "PM_SLUG_PREFIX", value = "btc-updown-5m" },
        { name = "CANDLE_INTERVAL_MINUTES", value = "5" },
        { name = "TICK_INTERVAL_SECONDS", value = "1.0" },
        { name = "EXPORT_INTERVAL_MINUTES", value = "5" },
      ]
      healthCheck = {
        command     = ["CMD-SHELL", "test -f /data/raw_data.db || exit 1"]
        interval    = 10
        timeout     = 5
        retries     = 5
        startPeriod = 10
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = "/ecs/${var.environment}/collector"
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    },
    {
      name      = "trade_engine"
      image     = "${aws_ecr_repository.trade_engine.repository_url}:latest"
      essential = true
      mountPoints = [{
        sourceVolume  = "data"
        containerPath = "/data"
        readOnly      = false
      }]
      dependsOn = [{
        containerName = "collector"
        condition     = "HEALTHY"
      }]
      environment = [
        { name = "ENV", value = "aws" },
        { name = "LOCAL_DATA_DIR", value = "/data" },
        { name = "AWS_REGION", value = var.aws_region },
        { name = "CANDLE_INTERVAL_MINUTES", value = "5" },
        { name = "PM_FEE", value = "0.02" },
      ]
      healthCheck = {
        command     = ["CMD-SHELL", "python3 -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\" || exit 1"]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = "/ecs/${var.environment}/trade_engine"
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])

  volume {
    name      = "data"
    host_path = "/opt/app/data"
  }

  tags = local.common_tags

  lifecycle {
    ignore_changes = [container_definitions]
  }
}

# ── ECS Service ───────────────────────────────────────────────────────────────

resource "aws_ecs_service" "main" {
  name            = "${local.name_prefix}-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.main.arn
  desired_count   = 1

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.ec2.name
    weight            = 100
    base              = 1
  }

  # 0/100 allows the old task to stop before the new one starts (single-instance setup)
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  enable_execute_command = true

  tags = local.common_tags

  lifecycle {
    ignore_changes = [task_definition]
  }
}
