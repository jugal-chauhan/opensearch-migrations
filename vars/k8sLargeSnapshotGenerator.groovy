def call(Map config = [:]) {
    k8sLocalDeployment(
            jobName: 'k8s-large-snapshot-generator'
    )
}
