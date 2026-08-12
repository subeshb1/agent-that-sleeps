#!/usr/bin/env bash
# Creates everything the MicroVM image build needs: the artifact bucket,
# the build role, the execution role, and the image itself. Idempotent
# enough to re-run; times the image build because nobody publishes that.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${BUCKET:-agent-that-sleeps-$ACCOUNT_ID}"
IMAGE_NAME="agent-that-sleeps"

echo "==> artifact bucket: $BUCKET"
aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || \
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"

echo "==> build role (assumed by Lambda to fetch the zip and write build logs)"
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":["sts:AssumeRole","sts:TagSession"]}]}'
aws iam get-role --role-name agent-that-sleeps-build 2>/dev/null >/dev/null || \
  aws iam create-role --role-name agent-that-sleeps-build \
    --assume-role-policy-document "$TRUST" >/dev/null
aws iam put-role-policy --role-name agent-that-sleeps-build \
  --policy-name build --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": [\"s3:GetObject\"], \"Resource\": \"arn:aws:s3:::$BUCKET/*\"},
    {\"Effect\": \"Allow\", \"Action\": [\"logs:CreateLogGroup\", \"logs:CreateLogStream\", \"logs:PutLogEvents\"], \"Resource\": \"arn:aws:logs:*:*:*\"}
  ]}"

echo "==> execution role (the agent's runtime identity; Bedrock only)"
aws iam get-role --role-name agent-that-sleeps-exec 2>/dev/null >/dev/null || \
  aws iam create-role --role-name agent-that-sleeps-exec \
    --assume-role-policy-document "$TRUST" >/dev/null
aws iam put-role-policy --role-name agent-that-sleeps-exec \
  --policy-name bedrock --policy-document '{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
     "Resource": "*"}
  ]}'

echo "==> package and upload"
(cd "$(dirname "$0")/../app" && zip -q -r /tmp/agent-that-sleeps.zip agent_server.py Dockerfile)
aws s3 cp --only-show-errors /tmp/agent-that-sleeps.zip "s3://$BUCKET/app.zip"

echo "==> create the MicroVM image (build + snapshot), timing it"
BUILD_START=$(date +%s)
aws lambda-microvms create-microvm-image \
  --name "$IMAGE_NAME" \
  --code-artifact "uri=s3://$BUCKET/app.zip" \
  --base-image-arn "arn:aws:lambda:$REGION:aws:microvm-image:al2023-1" \
  --build-role-arn "arn:aws:iam::$ACCOUNT_ID:role/agent-that-sleeps-build" >/dev/null

# get-microvm-image wants the full ARN even though create takes a name
IMAGE_ARN="arn:aws:lambda:$REGION:$ACCOUNT_ID:microvm-image:$IMAGE_NAME"
while true; do
  STATE=$(aws lambda-microvms get-microvm-image --image-identifier "$IMAGE_ARN" \
    --query 'state' --output text)
  [ "$STATE" = "CREATED" ] && break
  if [ "$STATE" = "CREATE_FAILED" ]; then
    echo "build failed; see /aws/lambda/microvms/$IMAGE_NAME in CloudWatch" >&2
    exit 1
  fi
  sleep 10
done
echo "image built in $(( $(date +%s) - BUILD_START ))s"
