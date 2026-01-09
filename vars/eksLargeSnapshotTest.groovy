def call(Map config = [:]) {

    pipeline {
        agent { label config.workerAgent ?: 'Jenkins-Default-Agent-X64-C5xlarge-Single-Host' }

        parameters {
            string(name: 'GIT_REPO_URL', defaultValue: 'https://github.com/jugal-chauhan/opensearch-migrations.git', description: 'Git repository url')
            string(name: 'GIT_BRANCH', defaultValue: 'jenkins-pipeline-eks-large-migration', description: 'Git branch to use for repository')
            string(name: 'STAGE', defaultValue: 'bigtest', description: 'Stage name for deployment environment')
            string(name: 'REGION', defaultValue: 'us-west-2', description: 'AWS region for deployment')
            // Reuse options
            booleanParam(name: 'SKIP_TARGET_DEPLOY', defaultValue: false, description: 'Skip target cluster deployment (reuse existing cluster for this stage)')
            booleanParam(name: 'SKIP_TARGET_DESTROY', defaultValue: false, description: 'Do not destroy target cluster after pipeline completes')
            booleanParam(name: 'SKIP_MA_DEPLOY', defaultValue: false, description: 'Skip MA infrastructure deployment (reuse existing EKS/MA stack for this stage)')
            booleanParam(name: 'SKIP_MA_DESTROY', defaultValue: false, description: 'Do not destroy MA infrastructure after pipeline completes')
        }

        options {
            lock(label: params.STAGE, quantity: 1, variable: 'stage')
            timeout(time: 4, unit: 'HOURS')
            buildDiscarder(logRotator(daysToKeepStr: '30'))
            skipDefaultCheckout(true)
        }

        stages {
            stage('Print Configuration') {
                steps {
                    script {
                        echo """
╔══════════════════════════════════════════════════════════════════╗
║                    PIPELINE CONFIGURATION                        ║
╠══════════════════════════════════════════════════════════════════╣
║  USER-CONFIGURABLE PARAMETERS                                    ║
╠══════════════════════════════════════════════════════════════════╣
║  GIT_REPO_URL:        ${params.GIT_REPO_URL}
║  GIT_BRANCH:          ${params.GIT_BRANCH}
║  STAGE:               ${params.STAGE}
║  REGION:              ${params.REGION}
║  SKIP_TARGET_DEPLOY:  ${params.SKIP_TARGET_DEPLOY}
║  SKIP_TARGET_DESTROY: ${params.SKIP_TARGET_DESTROY}
║  SKIP_MA_DEPLOY:      ${params.SKIP_MA_DEPLOY}
║  SKIP_MA_DESTROY:     ${params.SKIP_MA_DESTROY}
╠══════════════════════════════════════════════════════════════════╣
║  HARDCODED TARGET CLUSTER CONFIGURATION                          ║
╠══════════════════════════════════════════════════════════════════╣
║  clusterVersion:              OS_2.19
║  dataNodeCount:               6
║  dataNodeType:                r6g.4xlarge.search
║  dedicatedManagerNodeCount:   4
║  dedicatedManagerNodeType:    m6g.xlarge.search
║  ebsEnabled:                  true
║  ebsVolumeSize:               1024 GB
║  nodeToNodeEncryptionEnabled: true
║  openAccessPolicyEnabled:     true
╚══════════════════════════════════════════════════════════════════╝
"""
                    }
                }
            }

            stage('Checkout') {
                steps {
                    checkoutStep(branch: params.GIT_BRANCH, repo: params.GIT_REPO_URL)
                }
            }

            stage('Deploy Target Cluster') {
                when {
                    expression { return !params.SKIP_TARGET_DEPLOY }
                }
                steps {
                    timeout(time: 60, unit: 'MINUTES') {
                        dir('test') {
                            script {
                                env.clusterContextFilePath = "tmp/cluster-context-${currentBuild.number}.json"
                                def clusterConfig = [
                                    clusterId: 'target',
                                    clusterVersion: 'OS_2.19',
                                    clusterType: 'OPENSEARCH_MANAGED_SERVICE',
                                    openAccessPolicyEnabled: true,
                                    domainRemovalPolicy: params.SKIP_TARGET_DESTROY ? 'RETAIN' : 'DESTROY',
                                    dataNodeCount: 6,
                                    dataNodeType: 'r6g.4xlarge.search',
                                    dedicatedManagerNodeCount: 4,
                                    dedicatedManagerNodeType: 'm6g.xlarge.search',
                                    ebsEnabled: true,
                                    ebsVolumeSize: 1024,
                                    nodeToNodeEncryptionEnabled: true
                                ]

                                def contextJson = groovy.json.JsonOutput.prettyPrint(
                                    groovy.json.JsonOutput.toJson([
                                        stage: stage,
                                        vpcAZCount: 2,
                                        clusters: [clusterConfig]
                                    ])
                                )
                                writeFile file: env.clusterContextFilePath, text: contextJson
                                sh "cat ${env.clusterContextFilePath}"

                                withCredentials([string(credentialsId: 'migrations-test-account-id', variable: 'MIGRATIONS_TEST_ACCOUNT_ID')]) {
                                    withAWS(role: 'JenkinsDeploymentRole', roleAccount: MIGRATIONS_TEST_ACCOUNT_ID, region: params.REGION, duration: 3600, roleSessionName: 'jenkins-session') {
                                        sh "./awsDeployCluster.sh --stage ${stage} --context-file ${env.clusterContextFilePath}"
                                    }
                                }
                                env.clusterDetailsJson = readFile "tmp/cluster-details-${stage}.json"
                                echo "Cluster details: ${env.clusterDetailsJson}"
                            }
                        }
                    }
                }
            }

            stage('Load Existing Cluster Details') {
                when {
                    expression { return params.SKIP_TARGET_DEPLOY }
                }
                steps {
                    dir('test') {
                        script {
                            withCredentials([string(credentialsId: 'migrations-test-account-id', variable: 'MIGRATIONS_TEST_ACCOUNT_ID')]) {
                                withAWS(role: 'JenkinsDeploymentRole', roleAccount: MIGRATIONS_TEST_ACCOUNT_ID, region: params.REGION, duration: 3600, roleSessionName: 'jenkins-session') {
                                    sh """
                                        mkdir -p tmp
                                        ./awsDeployCluster.sh --stage ${stage} --context-file /dev/null 2>/dev/null || true
                                    """
                                    def detailsFile = "tmp/cluster-details-${stage}.json"
                                    if (fileExists(detailsFile)) {
                                        env.clusterDetailsJson = readFile detailsFile
                                    } else {
                                        error "Could not find existing cluster details for stage ${stage}. Run without SKIP_TARGET_DEPLOY first."
                                    }
                                }
                            }
                        }
                    }
                }
            }

            stage('Synth EKS CFN Template') {
                when {
                    expression { return !params.SKIP_MA_DEPLOY }
                }
                steps {
                    timeout(time: 20, unit: 'MINUTES') {
                        dir('deployment/migration-assistant-solution') {
                            script {
                                sh 'npm install --dev'
                                sh "npx cdk synth '*'"
                            }
                        }
                    }
                }
            }

            stage('Deploy EKS CFN Stack') {
                when {
                    expression { return !params.SKIP_MA_DEPLOY }
                }
                steps {
                    timeout(time: 30, unit: 'MINUTES') {
                        dir('deployment/migration-assistant-solution') {
                            script {
                                def clusterDetails = readJSON text: env.clusterDetailsJson
                                def targetCluster = clusterDetails.target
                                env.STACK_NAME = "Migration-Assistant-Infra-Import-VPC-eks-${stage}-${params.REGION}"

                                withCredentials([string(credentialsId: 'migrations-test-account-id', variable: 'MIGRATIONS_TEST_ACCOUNT_ID')]) {
                                    withAWS(role: 'JenkinsDeploymentRole', roleAccount: MIGRATIONS_TEST_ACCOUNT_ID, region: params.REGION, duration: 3600, roleSessionName: 'jenkins-session') {
                                        sh """
                                            cdk deploy ${env.STACK_NAME} \
                                              --parameters Stage=${stage} \
                                              --parameters VPCId=${targetCluster.vpcId} \
                                              --parameters VPCSubnetIds=${targetCluster.subnetIds} \
                                              --require-approval never \
                                              --concurrency 3
                                        """
                                    }
                                }
                            }
                        }
                    }
                }
            }

            stage('Install Migration Assistant') {
                when {
                    expression { return !params.SKIP_MA_DEPLOY }
                }
                steps {
                    timeout(time: 30, unit: 'MINUTES') {
                        script {
                            env.STACK_NAME = "Migration-Assistant-Infra-Import-VPC-eks-${stage}-${params.REGION}"
                            withCredentials([string(credentialsId: 'migrations-test-account-id', variable: 'MIGRATIONS_TEST_ACCOUNT_ID')]) {
                                withAWS(role: 'JenkinsDeploymentRole', roleAccount: MIGRATIONS_TEST_ACCOUNT_ID, region: params.REGION, duration: 3600, roleSessionName: 'jenkins-session') {
                                    def rawOutput = sh(
                                        script: """
                                            aws cloudformation describe-stacks \
                                              --stack-name ${env.STACK_NAME} \
                                              --query "Stacks[0].Outputs[?OutputKey=='MigrationsExportString'].OutputValue" \
                                              --output text
                                        """,
                                        returnStdout: true
                                    ).trim()

                                    def exportsMap = rawOutput.split(';')
                                        .collect { it.trim().replaceFirst(/^export\s+/, '') }
                                        .findAll { it.contains('=') }
                                        .collectEntries {
                                            def (key, value) = it.split('=', 2)
                                            [(key): value]
                                        }

                                    env.eksClusterName = exportsMap['MIGRATIONS_EKS_CLUSTER_NAME']
                                    env.clusterSecurityGroup = exportsMap['EKS_CLUSTER_SECURITY_GROUP']

                                    def principalArn = "arn:aws:iam::\$MIGRATIONS_TEST_ACCOUNT_ID:role/JenkinsDeploymentRole"
                                    sh """
                                        if ! aws eks describe-access-entry --cluster-name ${env.eksClusterName} --principal-arn ${principalArn} >/dev/null 2>&1; then
                                            aws eks create-access-entry --cluster-name ${env.eksClusterName} --principal-arn ${principalArn} --type STANDARD
                                        fi
                                        aws eks associate-access-policy \
                                          --cluster-name ${env.eksClusterName} \
                                          --principal-arn ${principalArn} \
                                          --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
                                          --access-scope type=cluster
                                        aws eks update-kubeconfig --region ${params.REGION} --name ${env.eksClusterName}
                                        for i in {1..10}; do
                                            kubectl get namespace default >/dev/null 2>&1 && break
                                            sleep 5
                                        done
                                    """

                                    def clusterDetails = readJSON text: env.clusterDetailsJson
                                    env.targetSecurityGroupId = clusterDetails.target.securityGroupId
                                    sh """
                                        exists=\$(aws ec2 describe-security-groups \
                                          --group-ids ${env.targetSecurityGroupId} \
                                          --query "SecurityGroups[0].IpPermissions[?UserIdGroupPairs[?GroupId=='${env.clusterSecurityGroup}']]" \
                                          --output text)
                                        if [ -z "\$exists" ]; then
                                            aws ec2 authorize-security-group-ingress \
                                              --group-id ${env.targetSecurityGroupId} \
                                              --protocol -1 --port -1 \
                                              --source-group ${env.clusterSecurityGroup}
                                        fi
                                    """

                                    dir('deployment/k8s/aws') {
                                        sh "./aws-bootstrap.sh --skip-git-pull --base-dir ${WORKSPACE} --use-public-images true --skip-console-exec --stage ${stage}"
                                    }

                                    sh 'kubectl wait --for=condition=Ready pod/migration-console-0 -n ma --timeout=300s'
                                    sh 'kubectl exec migration-console-0 -n ma -- bash -c "source /.venv/bin/activate && console --version"'
                                }
                            }
                        }
                    }
                }
            }

            stage('Connect to Existing MA') {
                when {
                    expression { return params.SKIP_MA_DEPLOY }
                }
                steps {
                    timeout(time: 10, unit: 'MINUTES') {
                        script {
                            env.STACK_NAME = "Migration-Assistant-Infra-Import-VPC-eks-${stage}-${params.REGION}"
                            withCredentials([string(credentialsId: 'migrations-test-account-id', variable: 'MIGRATIONS_TEST_ACCOUNT_ID')]) {
                                withAWS(role: 'JenkinsDeploymentRole', roleAccount: MIGRATIONS_TEST_ACCOUNT_ID, region: params.REGION, duration: 3600, roleSessionName: 'jenkins-session') {
                                    def rawOutput = sh(
                                        script: """
                                            aws cloudformation describe-stacks \
                                              --stack-name ${env.STACK_NAME} \
                                              --query "Stacks[0].Outputs[?OutputKey=='MigrationsExportString'].OutputValue" \
                                              --output text
                                        """,
                                        returnStdout: true
                                    ).trim()

                                    if (!rawOutput) {
                                        error "Could not find existing MA stack ${env.STACK_NAME}. Run without SKIP_MA_DEPLOY first."
                                    }

                                    def exportsMap = rawOutput.split(';')
                                        .collect { it.trim().replaceFirst(/^export\s+/, '') }
                                        .findAll { it.contains('=') }
                                        .collectEntries {
                                            def (key, value) = it.split('=', 2)
                                            [(key): value]
                                        }

                                    env.eksClusterName = exportsMap['MIGRATIONS_EKS_CLUSTER_NAME']
                                    env.clusterSecurityGroup = exportsMap['EKS_CLUSTER_SECURITY_GROUP']

                                    sh "aws eks update-kubeconfig --region ${params.REGION} --name ${env.eksClusterName}"
                                    sh 'kubectl wait --for=condition=Ready pod/migration-console-0 -n ma --timeout=60s'
                                    sh 'kubectl exec migration-console-0 -n ma -- bash -c "source /.venv/bin/activate && console --version"'

                                    def clusterDetails = readJSON text: env.clusterDetailsJson
                                    env.targetSecurityGroupId = clusterDetails.target.securityGroupId
                                }
                            }
                        }
                    }
                }
            }

            stage('Test') {
                steps {
                    echo 'TODO: Add test steps'
                }
            }

            stage('Validations') {
                steps {
                    echo 'TODO: Add validation steps'
                }
            }
        }

        post {
            always {
                timeout(time: 60, unit: 'MINUTES') {
                    script {
                        withCredentials([string(credentialsId: 'migrations-test-account-id', variable: 'MIGRATIONS_TEST_ACCOUNT_ID')]) {
                            withAWS(role: 'JenkinsDeploymentRole', roleAccount: MIGRATIONS_TEST_ACCOUNT_ID, region: params.REGION, duration: 3600, roleSessionName: 'jenkins-session') {
                                if (!params.SKIP_MA_DESTROY) {
                                    if (env.eksClusterName) {
                                        sh 'helm uninstall -n ma ma --wait --timeout 60s || true'
                                        sh 'kubectl delete namespace ma --wait=true || true'

                                        if (env.targetSecurityGroupId && env.clusterSecurityGroup) {
                                            sh """
                                                if aws ec2 describe-security-groups --group-ids ${env.targetSecurityGroupId} >/dev/null 2>&1; then
                                                    aws ec2 revoke-security-group-ingress \
                                                      --group-id ${env.targetSecurityGroupId} \
                                                      --protocol -1 --port -1 \
                                                      --source-group ${env.clusterSecurityGroup} || true
                                                fi
                                            """
                                        }
                                    }

                                    if (env.STACK_NAME) {
                                        dir('deployment/migration-assistant-solution') {
                                            sh "cdk destroy ${env.STACK_NAME} --force --concurrency 3 || true"
                                        }
                                    }
                                } else {
                                    echo "SKIP_MA_DESTROY=true: Retaining MA infrastructure for stage ${stage}"
                                }

                                if (!params.SKIP_TARGET_DESTROY) {
                                    dir('test/amazon-opensearch-service-sample-cdk') {
                                        sh "cdk destroy '*' --force --concurrency 3 || true"
                                        sh 'rm -f cdk.context.json'
                                    }
                                } else {
                                    echo "SKIP_TARGET_DESTROY=true: Retaining target cluster for stage ${stage}"
                                }
                            }
                        }

                        sh """
                            if command -v kubectl >/dev/null 2>&1; then
                                kubectl config get-contexts 2>/dev/null | grep migration-eks-cluster-${stage}-${params.REGION} | awk '{print \$2}' | xargs -r kubectl config delete-context || true
                            fi
                        """
                    }
                }
            }
        }
    }
}
