#!/bin/bash
# Solr 8.11.4 SolrCloud (single-node, embedded ZooKeeper) on Amazon Linux 2023.
# Runs as a Docker container under systemd. The instance profile (EC2 trust)
# vends AWS credentials directly via IMDSv2 — no IRSA or env-var token shuffle
# like we needed inside K8s.
#
# Variables substituted by CDK at synth time:
#   __S3_BUCKET__   the MA-managed default bucket
#   __S3_REGION__   AWS region (us-west-2)
#   __SOLR_IMAGE__  Docker image tag (solr:8.11.4)
set -euo pipefail
exec > >(tee /var/log/solr-bootstrap.log) 2>&1
echo "[$(date -u +%FT%TZ)] Starting Solr bootstrap"

# 1. Install Docker
dnf install -y docker
systemctl enable --now docker
usermod -aG docker ec2-user

# 2. Write solr.xml mirroring what we used in EKS, minus s3.endpoint (real AWS S3)
mkdir -p /opt/solr-config
cat > /opt/solr-config/solr.xml <<'SOLRXML'
<?xml version="1.0" encoding="UTF-8" ?>
<solr>
  <int name="maxBooleanClauses">${solr.max.booleanClauses:1024}</int>
  <str name="allowPaths">${solr.allowPaths:}</str>
  <str name="sharedLib">/opt/solr/dist,/opt/solr/contrib/s3-repository/lib</str>

  <solrcloud>
    <str name="host">${host:}</str>
    <int name="hostPort">${solr.port.advertise:0}</int>
    <str name="hostContext">${hostContext:solr}</str>
    <bool name="genericCoreNodeNames">${genericCoreNodeNames:true}</bool>
    <int name="zkClientTimeout">${zkClientTimeout:30000}</int>
    <int name="distribUpdateSoTimeout">${distribUpdateSoTimeout:600000}</int>
    <int name="distribUpdateConnTimeout">${distribUpdateConnTimeout:60000}</int>
    <str name="zkCredentialsProvider">${zkCredentialsProvider:org.apache.solr.common.cloud.DefaultZkCredentialsProvider}</str>
    <str name="zkACLProvider">${zkACLProvider:org.apache.solr.common.cloud.DefaultZkACLProvider}</str>
  </solrcloud>

  <backup>
    <repository name="default" class="org.apache.solr.s3.S3BackupRepository" default="true">
      <str name="s3.bucket.name">${S3_BUCKET_NAME:}</str>
      <str name="s3.region">${S3_REGION:us-west-2}</str>
    </repository>
  </backup>

  <shardHandlerFactory name="shardHandlerFactory" class="HttpShardHandlerFactory">
    <int name="socketTimeout">${socketTimeout:600000}</int>
    <int name="connTimeout">${connTimeout:60000}</int>
  </shardHandlerFactory>

  <metrics enabled="${metricsEnabled:true}"/>
</solr>
SOLRXML

chown -R 8983:8983 /opt/solr-config

# 3. Pull image up front so the systemd unit doesn't have to
docker pull __SOLR_IMAGE__

# 4. Systemd unit — runs Solr container, restarts on failure.
# Lets the image's docker-entrypoint.sh do its own SOLR_HOME / zoo.cfg / solr.xml
# bootstrap (no /var/solr bind mount). State is ephemeral — fine for this POC,
# since the only durable artifact is the snapshot, and that goes to S3.
# Our customised solr.xml is mounted on top of the default one after the entrypoint
# has set things up internally.
cat > /etc/systemd/system/solr.service <<'UNIT'
[Unit]
Description=Solr 8.11 SolrCloud (single-node, embedded ZK)
After=docker.service
Requires=docker.service

[Service]
Type=simple
Restart=always
RestartSec=10
TimeoutStartSec=0
ExecStartPre=-/usr/bin/docker rm -f solr
ExecStart=/usr/bin/docker run --rm --name solr \
  --network host \
  -e SOLR_HEAP=512m \
  -e SOLR_SECURITY_MANAGER_ENABLED=false \
  -e ZK_CREATE_CHROOT=true \
  -e S3_BUCKET_NAME=__S3_BUCKET__ \
  -e S3_REGION=__S3_REGION__ \
  -v /opt/solr-config/solr.xml:/opt/solr/server/solr/solr.xml:ro \
  __SOLR_IMAGE__ \
  solr-fg -force -cloud -DzkRun
ExecStop=/usr/bin/docker stop -t 30 solr

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now solr.service

echo "[$(date -u +%FT%TZ)] Solr bootstrap complete; service is starting"
