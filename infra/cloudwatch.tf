# ── Log groups ────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "collector" {
  name              = "/ecs/${var.environment}/collector"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "trade_engine" {
  name              = "/ecs/${var.environment}/trade_engine"
  retention_in_days = 30
  tags              = local.common_tags
}

# ── Alarms ────────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "ecs_no_running_tasks" {
  alarm_name          = "${local.name_prefix}-no-running-tasks"
  alarm_description   = "ECS service has no running tasks"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "RunningTaskCount"
  namespace           = "ECS/ContainerInsights"
  period              = 60
  statistic           = "Average"
  threshold           = 1

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.main.name
  }

  treat_missing_data = "breaching"
  tags               = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "ec2_high_cpu" {
  alarm_name          = "${local.name_prefix}-high-cpu"
  alarm_description   = "EC2 CPU > 80% for 10 minutes"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 300
  statistic           = "Average"
  threshold           = 80

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.ecs.name
  }

  tags = local.common_tags
}

# ── EBS snapshot lifecycle (daily, retain 7 days) ─────────────────────────────

resource "aws_dlm_lifecycle_policy" "ebs_backup" {
  description        = "Daily EBS snapshots for ${var.environment} - retain 7"
  execution_role_arn = aws_iam_role.dlm_lifecycle.arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["INSTANCE"]

    target_tags = {
      Backup = "true"
    }

    schedule {
      name = "daily"

      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["03:00"]
      }

      retain_rule {
        count = 7
      }

      tags_to_add = {
        SnapshotCreator = "DLM"
        Environment     = var.environment
      }

      copy_tags = false
    }
  }

  tags = local.common_tags
}
