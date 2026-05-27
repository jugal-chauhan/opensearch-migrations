# solr-source-ec2-cdk

Mini CDK app that provisions a single-node Solr 8.11 SolrCloud on EC2 inside an
**existing Migration Assistant VPC**, configured to write its backup repository
to MA's existing S3 bucket. Used as the source cluster for a Solr → OpenSearch
(or AOSS) backfill migration POC.

This is a portable, greenfield mini-app — it doesn't extend `aws-samples/amazon-opensearch-service-sample-cdk`
(which only supports OpenSearch managed/serverless cluster types as of v0.4.1).

## What it creates

| Resource | Notes |
|---|---|
| EC2 instance (`t3.medium` default, AL2023 x86_64, encrypted gp3 root) | Private subnet of MA's VPC, no public IP, IMDSv2 required, hop-limit 1 |
| Security group `solr-ec2-sg-<stage>` | Ingress 8983 from MA's EKS node SG only (SG-to-SG); egress unrestricted (S3 + STS via NAT) |
| IAM role `solr-ec2-role-<stage>` (instance profile auto-created) | `AmazonSSMManagedInstanceCore` + inline S3 read/write scoped to one bucket |
| User-data | Installs Docker, writes a `solr.xml` with `S3BackupRepository`, runs `solr:8.11.4` under systemd with `--network host` |

Access to the host is **SSM Session Manager only** — no SSH key, no port 22 open.

## Why these choices

| Decision | Why |
|---|---|
| **EC2, not K8s/Operator** | Most AWS-resident Solr customers run on EC2. EC2 instance profile is simpler and stronger than IRSA/Pod-Identity for this case. |
| **`--network host`** | IMDSv2 with hop-limit 1 (security hardening) is unreachable from inside a container's network namespace at hop 2. Host mode keeps the JVM at hop 1 so it can fetch instance-profile creds. |
| **Bind-mount `solr.xml` at `/var/solr/data/solr.xml`** | After the docker entrypoint runs `init-var-solr`, that's the path Solr actually reads from. Mounting at `/opt/solr/server/solr/solr.xml` gets ignored. |
| **No `/var/solr` bind mount** | Lets the image's entrypoint do its own SOLR_HOME + zoo.cfg setup. State is ephemeral by design — durable artifact is the snapshot, which goes to S3. |
| **Reuse MA's auto-created S3 bucket** | MA's Helm chart creates `migrations-default-<account>-<stage>-<region>` automatically. No reason to add a second bucket. |
| **SG-to-SG ingress** | Tightest practical rule. Survives subnet/CIDR changes. Anything outside MA's EKS workers can't reach Solr. |

## Prerequisites

- An existing Migration Assistant deployment on EKS in the same account+region.
- AWS credentials with the usual deploy permissions in that account.
- Node 18+ and `npm`. CDK v2 is pulled in via dev-deps.
- `jq` (used by the discovery script).

## Quick start

```bash
cd test/solr-source-ec2-cdk
npm install

# Auto-discover MA's network values from a deployed EKS cluster.
# Renders cdk.json (gitignored, per-deployer) from cdk.json.template (committed reference).
./scripts/discover-ma-context.sh <your-eks-cluster-name>

# Sanity check
npm run synth                        # renders the CFN template, no AWS calls

# Deploy (~3 min for resources, ~1 min for Solr to come up inside the container)
npm run deploy

# Use the outputs in your MA workflow config:
#   sourceClusters.<name>.endpoint        ← SolrEndpoint
#   sourceClusters.<name>.snapshotInfo.repos.<repo>.s3RepoPathUri  ← SnapshotS3RepoUri
cat cdk-outputs.json

# Tear down
npm run destroy
```

## Verifying after deploy

```bash
INSTANCE=$(jq -r '.["SolrSourceEc2-<stage>"].SolrInstanceId' cdk-outputs.json)

# Confirm Solr is healthy
aws ssm send-command --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["curl -s http://localhost:8983/solr/admin/info/system?wt=json | head"]'

# Optional: smoke-test S3 backup
aws ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunShellScript \
  --parameters 'commands=[
    "curl -s \"http://localhost:8983/solr/admin/collections?action=CREATE&name=dummy&numShards=1&replicationFactor=1&maxShardsPerNode=1&wt=json\"",
    "curl -s \"http://localhost:8983/solr/admin/collections?action=BACKUP&name=smoke&collection=dummy&location=s3:///<s3Prefix>&repository=default&wt=json\""
  ]'
```

## Configuration

All inputs are CDK context values. The committed `cdk.json.template` has placeholders (`REPLACE_ME-*`); `scripts/discover-ma-context.sh` renders them into a real `cdk.json` (gitignored, per-deployer). The `bin/` entrypoint rejects any `REPLACE_ME-*` value at synth so a forgotten input fails fast.

| Context key | Required | Description |
|---|---|---|
| `stage` | ✓ | Stage suffix used in resource names (e.g. `solraoss`, `dev`, `team1`) |
| `vpcId` | ✓ | MA's VPC |
| `privateSubnetIds` | ✓ | Comma-separated list of MA's private subnet IDs |
| `availabilityZones` | ✓ | Comma-separated AZs paired with `privateSubnetIds` by index |
| `eksNodeSecurityGroupId` | ✓ | SG attached to MA's EKS workers (source for ingress 8983) |
| `s3BucketName` | ✓ | MA's default snapshot bucket |
| `s3Prefix` | ✗ | Subpath under the bucket (default: `solr-aoss-ec2`) |
| `instanceType` | ✗ | EC2 type (default: `t3.medium`) |
| `solrImage` | ✗ | Docker image tag (default: `solr:8.11.4`) |

`scripts/discover-ma-context.sh` populates the 6 required values from a live EKS cluster. Override anything on the CLI: `cdk deploy --context instanceType=m5.large`.

To bootstrap from scratch without running the script:
```bash
cp cdk.json.template cdk.json
# then edit the REPLACE_ME-* values by hand
```

## Outputs

| Output | What to do with it |
|---|---|
| `SolrInstanceId` | `aws ssm start-session --target ...` |
| `SolrPrivateIp` | Source endpoint in MA workflow config |
| `SolrEndpoint` | `http://<ip>:8983`, ready to paste into workflow |
| `SnapshotS3RepoUri` | `s3RepoPathUri` for the workflow's snapshot config |
| `SolrSecurityGroupId` | If you need to extend ingress later |
| `SolrRoleArn` | Instance role (e.g. for adding to AOSS data-access policy) |

## Wiring into a Migration Assistant workflow

Once Solr is up and the smoke test passes:

```yaml
# inside `workflow configure edit` on the migration-console pod
sourceClusters:
  solr-source:
    endpoint: "http://<SolrPrivateIp>:8983"
    allowInsecure: true
    version: "SOLR 8.11.4"
    snapshotInfo:
      repos:
        default:                         # MUST match <repository name=...> in solr.xml
          awsRegion: "<region>"
          s3RepoPathUri: "<SnapshotS3RepoUri>"
      snapshots:
        solr-snap:
          config:
            createSnapshotConfig: {}     # MA drives Solr's BACKUP API itself
          repoName: "default"

targetClusters:
  target:
    endpoint: "https://<aoss-collection>.<region>.aoss.amazonaws.com"
    authConfig:
      sigv4:
        region: "<region>"
        service: "aoss"

snapshotMigrationConfigs:
  - fromSource: "solr-source"
    toTarget: "target"
    perSnapshotConfig:
      solr-snap:
        - metadataMigrationConfig: {}
          documentBackfillConfig:
            podReplicas: 1
```

If the target is AOSS, you must also add `SolrRoleArn` to the AOSS data-access policy
along with the MA migration role (so AOSS allows writes from MA's RFS workers).

## Caveats and known issues

1. **ts-node** is required to run `bin/solr-source-ec2.ts`; it's listed in `devDependencies`. If `cdk synth` complains "Cannot find module 'ts-node'", run `npm install`.
2. CDK throws **"You cannot reference a Subnet's availability zone if it was not supplied"** if `availabilityZones` count doesn't match `privateSubnetIds` count. The discovery script keeps them paired.
3. **AL2023 only** — the AMI lookup uses `ec2.MachineImage.latestAmazonLinux2023()`. If you need a different OS, edit `lib/solr-source-ec2-stack.ts`.
4. Solr's container is on **`--network host`** intentionally. Don't change that without re-thinking IMDS reachability.
5. No HTTPS / no auth on Solr. The SG-to-SG rule is the only access control. For prod-shaped testing, layer on Solr Basic Auth or run behind a TLS terminating proxy.
