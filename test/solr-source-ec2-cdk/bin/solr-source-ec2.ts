#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { SolrSourceEc2Stack } from '../lib/solr-source-ec2-stack';

const app = new cdk.App();

function required<T>(key: string, val: T | undefined): T {
    if (val === undefined || val === null || val === '') {
        throw new Error(`Required CDK context value missing: '${key}'.\n` +
            `Either set it in cdk.json or pass --context ${key}=<value>.\n` +
            `Run scripts/discover-ma-context.sh to auto-generate the network values.`);
    }
    return val;
}

function rejectPlaceholder(key: string, val: string): string {
    if (val.startsWith('REPLACE_ME') || val.includes('<your-')) {
        throw new Error(`CDK context value '${key}' is still a placeholder ('${val}').\n` +
            `Run scripts/discover-ma-context.sh or override with --context ${key}=<real-value>.`);
    }
    return val;
}

const stage = required('stage', app.node.tryGetContext('stage') as string | undefined);
const vpcId = rejectPlaceholder('vpcId',
    required('vpcId', app.node.tryGetContext('vpcId') as string | undefined));
const privateSubnetIdsRaw = rejectPlaceholder('privateSubnetIds',
    required('privateSubnetIds', app.node.tryGetContext('privateSubnetIds') as string | undefined));
const availabilityZonesRaw = rejectPlaceholder('availabilityZones',
    required('availabilityZones', app.node.tryGetContext('availabilityZones') as string | undefined));
const eksNodeSecurityGroupId = rejectPlaceholder('eksNodeSecurityGroupId',
    required('eksNodeSecurityGroupId', app.node.tryGetContext('eksNodeSecurityGroupId') as string | undefined));
const s3BucketName = rejectPlaceholder('s3BucketName',
    required('s3BucketName', app.node.tryGetContext('s3BucketName') as string | undefined));

const instanceType = (app.node.tryGetContext('instanceType') as string) ?? 't3.medium';
const s3Prefix = (app.node.tryGetContext('s3Prefix') as string) ?? `solr-${stage}`;
const solrImage = (app.node.tryGetContext('solrImage') as string) ?? 'solr:8.11.4';

new SolrSourceEc2Stack(app, `SolrSourceEc2-${stage}`, {
    env: {
        account: process.env.CDK_DEFAULT_ACCOUNT,
        region: process.env.CDK_DEFAULT_REGION,
    },
    description: `Self-managed Solr 8.11 source on EC2 in MA's VPC (stage=${stage})`,
    stage,
    vpcId,
    privateSubnetIds: privateSubnetIdsRaw.split(',').map(s => s.trim()).filter(Boolean),
    availabilityZones: availabilityZonesRaw.split(',').map(s => s.trim()).filter(Boolean),
    eksNodeSecurityGroupId,
    instanceType,
    s3BucketName,
    s3Prefix,
    solrImage,
});
