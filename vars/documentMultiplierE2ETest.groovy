def call(Map args) {
    def sourceContextId = args.sourceContextId ?: 'source'
    def migrationContextId = args.migrationContextId ?: 'migration'
    def testUniqueId = args.testUniqueId ?: UUID.randomUUID().toString()

    def source_cdk_context = """
        {
          "source": {
            "esVersion": "ES_5.6",
            "nodeToNodeEncryptionEnabled": true,
            "encryptionAtRestEnabled": true,
            "vpcEnabled": true,
            "vpcAZCount": 2,
            "domainAZCount": 2
          }
        }
    """

    def migration_cdk_context = """
        {
          "migration": {
            "esVersion": "ES_5.6",
            "nodeToNodeEncryptionEnabled": true,
            "encryptionAtRestEnabled": true,
            "vpcEnabled": true,
            "vpcAZCount": 2,
            "domainAZCount": 2,
            "mskAZCount": 2,
            "migrationAssistanceEnabled": true,
            "replayerOutputEFSRemovalPolicy": "DESTROY",
            "migrationConsoleServiceEnabled": true,
            "otelCollectorEnabled": true
          }
        }
    """

    defaultIntegPipeline(
            sourceContext: source_cdk_context,
            migrationContext: migration_cdk_context,
            sourceContextId: sourceContextId,
            migrationContextId: migrationContextId,
            defaultStageId: 'doc-multiplier',
            skipCaptureProxyOnNodeSetup: true,
            jobName: 'document-multiplier-e2e-test',
            testUniqueId: testUniqueId,
            integTestCommand: '/root/lib/integ_test/integ_test/document_multiplier_tests.py'
    )
}
