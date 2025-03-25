@Library(['jenkins-shared-libraries']) _

def sourceContextId = UUID.randomUUID().toString()
def migrationContextId = UUID.randomUUID().toString()
def testUniqueId = UUID.randomUUID().toString()

pipeline {
    agent {
        label 'al2'
    }
    
    options {
        timestamps()
        timeout(time: 3, unit: 'HOURS')
        buildDiscarder(logRotator(numToKeepStr: '10'))
    }
    
    stages {
        stage('Run Document Multiplier Test') {
            steps {
                script {
                    documentMultiplierE2ETest(
                        sourceContextId: sourceContextId,
                        migrationContextId: migrationContextId,
                        testUniqueId: testUniqueId
                    )
                }
            }
        }
    }
    
    post {
        always {
            cleanWs()
        }
    }
}
