import groovy.json.JsonOutput

def call(Map config = [:]) {
    def migrationContextId = 'full-migration'
    def time = new Date().getTime()
    def testUniqueId = "integ_full_${time}_${currentBuild.number}"

    def docTransformerPath = "/shared-logs-output/test-transformations/transformation.json"
    
    def migration_cdk_context = """
        {
          "full-migration": {
            "stage": "dev",
            "artifactBucketRemovalPolicy": "DESTROY",
            "captureProxyServiceEnabled": false,
            "targetClusterProxyServiceEnabled": false,
            "trafficReplayerServiceEnabled": false,
            "reindexFromSnapshotServiceEnabled": true,
            "reindexFromSnapshotExtraArgs": "--doc-transformer-config-file ${docTransformerPath}",
            "sourceCluster": {
                "endpoint": "https://search-es56-test2-large-snapshot-gu73liixr675uddje2rtbi3qla.aos.us-east-1.on.aws",
                "auth": {
                    "type": "sigv4",
                    "region": "us-east-1",
                    "serviceSigningName": "es"
                },
                "version": "ES_5.6"
            },
            "targetCluster": {
                "endpoint": "https://search-es56-test2-large-snapshot-gu73liixr675uddje2rtbi3qla.aos.us-east-1.on.aws",
                "auth": {
                    "type": "sigv4",
                    "region": "us-east-1",
                    "serviceSigningName": "es"
                },
                "version": "ES_5.6"
            },
            "vpcEnabled": true,
            "migrationAssistanceEnabled": true,
            "replayerOutputEFSRemovalPolicy": "DESTROY",
            "migrationConsoleServiceEnabled": true,
            "reindexFromSnapshotWorkerSize": "maximum",
            "otelCollectorEnabled": true
          }
        }
    """

    def source_cdk_context = """
        {
          "source-single-node-ec2": {
            "suffix": "ec2-source-<STAGE>",
            "networkStackSuffix": "ec2-source-<STAGE>"
          }
        }
    """

    defaultIntegPipeline(
            sourceContext: source_cdk_context,
            migrationContext: migration_cdk_context,
            migrationContextId: migrationContextId,
            defaultStageId: 'dev',
            skipCaptureProxyOnNodeSetup: true,
            skipSourceDeploy: true,
            jobName: 'k8s-large-snapshot-test',
            testUniqueId: testUniqueId,
            integTestCommand: '/root/lib/integ_test/integ_test/document_multiplier.py --config-file=/config/migration-services.yaml --log-cli-level=info'
    )
}
