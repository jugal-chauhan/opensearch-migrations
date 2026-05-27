import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';

export interface SolrSourceEc2StackProps extends cdk.StackProps {
    stage: string;
    vpcId: string;
    /**
     * Private subnet IDs. Each must be paired with its AZ in `availabilityZones`
     * at the SAME index. We can't auto-discover AZ at synth time when subnets are
     * imported by ID, so the caller has to supply both. The discovery script does this.
     */
    privateSubnetIds: string[];
    availabilityZones: string[];
    eksNodeSecurityGroupId: string;
    instanceType: string;
    s3BucketName: string;
    s3Prefix: string;
    solrImage: string;
}

/**
 * Provisions a single-node SolrCloud 8.11 on EC2 in the same VPC as MA.
 *
 * Auth model: instance profile (EC2 trust) with least-privilege S3 access scoped
 * to MA's default bucket. No IRSA, no Pod Identity, no static creds. Solr's bundled
 * AWS SDK v1 picks up creds from IMDSv2.
 *
 * Network model: instance launches in MA's private subnet with no public IP. Ingress
 * on 8983 is restricted SG-to-SG to the EKS cluster security group, so MA pods can
 * reach Solr but nothing else can. Egress is unrestricted (Solr needs S3 + STS via NAT).
 *
 * Access for humans: SSM Session Manager only (instance profile has SSMManagedInstanceCore).
 * No SSH key, no port 22.
 */
export class SolrSourceEc2Stack extends cdk.Stack {
    constructor(scope: Construct, id: string, props: SolrSourceEc2StackProps) {
        super(scope, id, props);

        // 1. Import MA's existing VPC + private subnets so we don't create new networking.
        // Subnet AZs MUST be supplied — CDK can't resolve them at synth time when subnets
        // are imported by ID, and ec2.Instance subnet selection requires them.
        if (props.privateSubnetIds.length !== props.availabilityZones.length) {
            throw new Error(
                `privateSubnetIds (${props.privateSubnetIds.length}) and availabilityZones ` +
                `(${props.availabilityZones.length}) must have the same length and be paired by index. ` +
                `Run scripts/discover-ma-context.sh to generate matching values.`,
            );
        }
        const vpc = ec2.Vpc.fromVpcAttributes(this, 'MaVpc', {
            vpcId: props.vpcId,
            availabilityZones: props.availabilityZones,
            privateSubnetIds: props.privateSubnetIds,
        });
        const eksNodeSg = ec2.SecurityGroup.fromSecurityGroupId(
            this, 'EksNodeSg', props.eksNodeSecurityGroupId,
            { mutable: false },
        );

        // 2. Solr instance security group: ingress 8983 from EKS nodes only.
        const solrSg = new ec2.SecurityGroup(this, 'SolrSg', {
            vpc,
            description: 'Solr-on-EC2 SG: ingress 8983 from EKS node SG only',
            allowAllOutbound: true,
            securityGroupName: `solr-ec2-sg-${props.stage}`,
        });
        solrSg.addIngressRule(
            ec2.Peer.securityGroupId(eksNodeSg.securityGroupId),
            ec2.Port.tcp(8983),
            'Solr HTTP from MA EKS pods',
        );

        // 3. IAM role for the EC2 — instance profile auto-created by Role.
        const role = new iam.Role(this, 'SolrEc2Role', {
            assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
            roleName: `solr-ec2-role-${props.stage}`,
            description: 'EC2 instance role for Solr-on-EC2 source: SSM + S3 backup write',
            managedPolicies: [
                iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
            ],
        });
        // Mirrors solr-s3-write inline policy from the IRSA POC, minus delete (Solr
        // backup repo can rewrite/cleanup; explicit delete only needed for housekeeping).
        role.addToPolicy(new iam.PolicyStatement({
            effect: iam.Effect.ALLOW,
            actions: ['s3:ListBucket', 's3:ListBucketMultipartUploads'],
            resources: [`arn:aws:s3:::${props.s3BucketName}`],
        }));
        role.addToPolicy(new iam.PolicyStatement({
            effect: iam.Effect.ALLOW,
            actions: [
                's3:GetObject',
                's3:PutObject',
                's3:DeleteObject',
                's3:AbortMultipartUpload',
                's3:ListMultipartUploadParts',
            ],
            resources: [`arn:aws:s3:::${props.s3BucketName}/*`],
        }));

        // 4. User-data — substitute placeholders in the asset script.
        const userDataTemplate = fs.readFileSync(
            path.join(__dirname, '..', 'assets', 'solr-user-data.sh'),
            'utf8',
        );
        const userDataScript = userDataTemplate
            .replace(/__S3_BUCKET__/g, props.s3BucketName)
            .replace(/__S3_REGION__/g, this.region)
            .replace(/__SOLR_IMAGE__/g, props.solrImage);
        const userData = ec2.UserData.forLinux({ shebang: '#!/bin/bash' });
        userData.addCommands(userDataScript.replace(/^#!\/bin\/bash\n?/, ''));

        // 5. EC2 instance — IMDSv2 required, hop-limit 1, encrypted gp3 root, no public IP.
        // Pair each imported subnet ID with the matching AZ from props.
        const importedSubnets = props.privateSubnetIds.map((sid, i) =>
            ec2.Subnet.fromSubnetAttributes(this, `PrivateSubnet${i}`, {
                subnetId: sid,
                availabilityZone: props.availabilityZones[i],
            }));
        const instance = new ec2.Instance(this, 'SolrInstance', {
            vpc,
            vpcSubnets: { subnets: importedSubnets },
            instanceType: new ec2.InstanceType(props.instanceType),
            // Use the latest AL2023 x86_64 AMI for the deployment region (resolved via SSM
            // parameter at synth time). No region->ami map needed; works in any region.
            machineImage: ec2.MachineImage.latestAmazonLinux2023({
                cpuType: ec2.AmazonLinuxCpuType.X86_64,
            }),
            securityGroup: solrSg,
            role,
            userData,
            blockDevices: [{
                deviceName: '/dev/xvda',
                volume: ec2.BlockDeviceVolume.ebs(30, {
                    volumeType: ec2.EbsDeviceVolumeType.GP3,
                    encrypted: true,
                    deleteOnTermination: true,
                }),
            }],
            requireImdsv2: true,
            instanceName: `solr-ec2-${props.stage}`,
            propagateTagsToVolumeOnCreation: true,
        });
        // Defense-in-depth: tighten IMDSv2 hop limit so containers can't proxy creds.
        const cfnInstance = instance.node.defaultChild as ec2.CfnInstance;
        cfnInstance.metadataOptions = {
            httpTokens: 'required',
            httpEndpoint: 'enabled',
            httpPutResponseHopLimit: 1,
        };

        cdk.Tags.of(instance).add('Stage', props.stage);
        cdk.Tags.of(instance).add('Purpose', 'solr-source-for-aoss-migration-poc');
        cdk.Tags.of(instance).add('auto-stop', 'true');

        // 6. Outputs — drive the workflow config + smoke tests.
        new cdk.CfnOutput(this, 'SolrInstanceId', {
            value: instance.instanceId,
            description: 'EC2 instance ID (use for SSM start-session)',
        });
        new cdk.CfnOutput(this, 'SolrPrivateIp', {
            value: instance.instancePrivateIp,
            description: 'Solr instance private IP (use in workflow config endpoint)',
        });
        new cdk.CfnOutput(this, 'SolrEndpoint', {
            value: `http://${instance.instancePrivateIp}:8983`,
            description: 'Solr HTTP endpoint reachable from MA pods',
        });
        new cdk.CfnOutput(this, 'SolrSecurityGroupId', {
            value: solrSg.securityGroupId,
            description: 'Solr instance SG ID',
        });
        new cdk.CfnOutput(this, 'SolrRoleArn', {
            value: role.roleArn,
            description: 'IAM role assumed by the Solr EC2 (S3 + SSM)',
        });
        new cdk.CfnOutput(this, 'SnapshotS3RepoUri', {
            value: `s3://${props.s3BucketName}/${props.s3Prefix}`,
            description: 'Snapshot prefix for the workflow config',
        });
    }
}
