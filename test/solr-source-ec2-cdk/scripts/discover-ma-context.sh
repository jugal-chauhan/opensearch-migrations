#!/usr/bin/env bash
# Discovers MA deployment values needed by this CDK app and writes cdk.context.json.
#
# Usage:
#   scripts/discover-ma-context.sh <eks-cluster-name> [stage] [region]
#
# Example:
#   scripts/discover-ma-context.sh migration-eks-cluster-mystage-us-west-2 mystage us-west-2
#
# Requirements:
#   - AWS credentials with eks:Describe* + ec2:Describe* + s3:ListAllMyBuckets in the target account.
#   - jq.
#
# What it discovers:
#   - VPC ID         from `eks describe-cluster`
#   - private subnet IDs + their AZs   filtered from `ec2 describe-subnets` (Name tag contains "private")
#   - EKS node SG    from any worker node in the cluster (the SG actually attached to the ENI)
#   - default MA S3 bucket (migrations-default-<account>-<stage>-<region>)
#
# Writes cdk.context.json (gitignored). Does NOT touch cdk.json (committed, has placeholders).
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <eks-cluster-name> [stage] [region]" >&2
  exit 1
fi

EKS_CLUSTER="$1"
STAGE="${2:-${EKS_CLUSTER##*cluster-}}"   # crude default: trim "migration-eks-cluster-" prefix
STAGE="${STAGE%-*-*}"                     # trim "-<region>" suffix if present
REGION="${3:-${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}}"

echo "[discover] eks=$EKS_CLUSTER  stage=$STAGE  region=$REGION"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
echo "[discover] account=$ACCOUNT"

VPC_ID=$(aws eks describe-cluster --name "$EKS_CLUSTER" --region "$REGION" \
  --query 'cluster.resourcesVpcConfig.vpcId' --output text)
echo "[discover] vpcId=$VPC_ID"

# Private subnets — must be MapPublicIpOnLaunch=false AND Name tag contains "private".
# This matches the convention from MA's CFN (subnet names: migration-assistant-private-subnet-N-<stage>).
PRIVATE_SUBNETS_JSON=$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=map-public-ip-on-launch,Values=false" \
  --query "Subnets[?Tags[?Key=='Name' && contains(Value, 'private')]].{id:SubnetId, az:AvailabilityZone}" \
  --output json)
SUBNET_COUNT=$(echo "$PRIVATE_SUBNETS_JSON" | jq 'length')
if [[ "$SUBNET_COUNT" -lt 2 ]]; then
  echo "[discover] WARNING: found $SUBNET_COUNT private subnets — expected at least 2 across different AZs" >&2
fi
PRIVATE_SUBNET_IDS=$(echo "$PRIVATE_SUBNETS_JSON" | jq -r 'map(.id) | join(",")')
PRIVATE_SUBNET_AZS=$(echo "$PRIVATE_SUBNETS_JSON" | jq -r 'map(.az) | join(",")')
echo "[discover] privateSubnetIds=$PRIVATE_SUBNET_IDS"
echo "[discover] availabilityZones=$PRIVATE_SUBNET_AZS"

# EKS node SG — discovered from a running worker (NOT the cluster SG, which is the control-plane SG).
# Look at any running EC2 in the VPC and grab its first SG.
NODE_SG=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text)
if [[ -z "$NODE_SG" || "$NODE_SG" == "None" ]]; then
  echo "[discover] ERROR: could not find a running worker node SG in VPC $VPC_ID. Is MA actually deployed?" >&2
  exit 1
fi
echo "[discover] eksNodeSecurityGroupId=$NODE_SG"

# Default MA bucket — convention is migrations-default-<account>-<stage>-<region>.
BUCKET="migrations-default-${ACCOUNT}-${STAGE}-${REGION}"
if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
  echo "[discover] s3BucketName=$BUCKET (exists)"
else
  echo "[discover] WARNING: bucket $BUCKET does not exist. MA install creates it via the chart's createS3Bucket hook — you may be running this before MA is up, or your stage name doesn't match the convention." >&2
fi

# Generate cdk.json from cdk.json.template, substituting REPLACE_ME-* placeholders
# with the discovered values. We write to cdk.json (gitignored, per-deployer) so the
# template stays untouched as the committed reference.
HERE="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$HERE/../cdk.json.template"
OUTFILE="$HERE/../cdk.json"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "[discover] ERROR: $TEMPLATE not found" >&2
  exit 1
fi

# jq makes the substitution explicit and JSON-safe. Comments in the template are
# preserved as-is (they're top-level _comment fields under .context).
jq \
  --arg stage "$STAGE" \
  --arg vpcId "$VPC_ID" \
  --arg subnets "$PRIVATE_SUBNET_IDS" \
  --arg azs "$PRIVATE_SUBNET_AZS" \
  --arg nodeSg "$NODE_SG" \
  --arg bucket "$BUCKET" \
  '.context.stage = $stage
   | .context.vpcId = $vpcId
   | .context.privateSubnetIds = $subnets
   | .context.availabilityZones = $azs
   | .context.eksNodeSecurityGroupId = $nodeSg
   | .context.s3BucketName = $bucket' \
  "$TEMPLATE" > "$OUTFILE"

echo
echo "[discover] Wrote $OUTFILE"
echo "[discover] Review and run:  npm run synth   then   npm run deploy"
