#!/usr/bin/env bash
set -euo pipefail

ASG="polymarket-dev-ecs-asg"
REGION="eu-central-1"

echo "Looking up instance in $ASG..."
INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:aws:autoscaling:groupName,Values=$ASG" \
             "Name=instance-state-name,Values=running,stopped,pending" \
  --query 'Reservations[0].Instances[0].InstanceId' \
  --output text --region "$REGION")

if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
  echo "No instance found in $ASG — nothing to do."
  exit 0
fi

echo "Instance: $INSTANCE_ID"

STATE=$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].State.Name' \
  --output text --region "$REGION")

echo "Current state: $STATE"

if [[ "$STATE" == "stopped" ]]; then
  echo "Instance is already stopped."
  exit 0
fi

echo "Entering ASG standby..."
aws autoscaling enter-standby \
  --instance-ids "$INSTANCE_ID" \
  --auto-scaling-group-name "$ASG" \
  --should-decrement-desired-capacity \
  --region "$REGION"

echo "Stopping instance..."
aws ec2 stop-instances --instance-ids "$INSTANCE_ID" --region "$REGION" > /dev/null

echo "Waiting for instance to stop..."
aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID" --region "$REGION"

echo "Done. Instance $INSTANCE_ID is stopped."
