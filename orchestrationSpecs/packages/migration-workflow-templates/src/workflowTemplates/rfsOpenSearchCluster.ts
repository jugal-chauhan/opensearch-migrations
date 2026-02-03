import {
    BaseExpression,
    configMapKey,
    expr,
    INTERNAL,
    makeStringTypeProxy,
    selectInputsForRegister,
    typeToken,
    WorkflowBuilder
} from "@opensearch-migrations/argo-workflow-builders";
import {CommonWorkflowParameters} from "./commonUtils/workflowParameters";
import {makeRequiredImageParametersForKeys} from "./commonUtils/imageDefinitions";

export function getRfsOpenSearchClusterName(sessionName: BaseExpression<string>): BaseExpression<string> {
    return expr.concat(sessionName, expr.literal("-rfs-opensearch"));
}

function createRFSOpenSearchSecretManifest(clusterName: BaseExpression<string>) {
    return {
        apiVersion: "v1",
        kind: "Secret",
        metadata: {
            name: makeStringTypeProxy(expr.concat(clusterName, expr.literal("-creds"))),
            labels: {
                app: makeStringTypeProxy(clusterName)
            }
        },
        type: "Opaque",
        stringData: {
            username: "admin",
            password: "myStrongPassword123!"
        }
    };
}

function createRFSOpenSearchServiceManifest(clusterName: BaseExpression<string>) {
    return {
        apiVersion: "v1",
        kind: "Service",
        metadata: {
            name: makeStringTypeProxy(clusterName),
            labels: {
                app: makeStringTypeProxy(clusterName)
            }
        },
        spec: {
            selector: {
                app: makeStringTypeProxy(clusterName)
            },
            ports: [
                {
                    name: "https",
                    port: 9200,
                    targetPort: "https"
                }
            ]
        }
    };
}

function createRFSOpenSearchStatefulSetManifest(clusterName: BaseExpression<string>) {
    return {
        apiVersion: "apps/v1",
        kind: "StatefulSet",
        metadata: {
            name: makeStringTypeProxy(clusterName),
            labels: {
                app: makeStringTypeProxy(clusterName),
                "app.kubernetes.io/instance": makeStringTypeProxy(clusterName)
            }
        },
        spec: {
            serviceName: makeStringTypeProxy(clusterName),
            replicas: 1,
            persistentVolumeClaimRetentionPolicy: {
                whenDeleted: "Delete",
                whenScaled: "Retain"
            },
            selector: {
                matchLabels: {
                    app: makeStringTypeProxy(clusterName)
                }
            },
            template: {
                metadata: {
                    labels: {
                        app: makeStringTypeProxy(clusterName),
                        "app.kubernetes.io/instance": makeStringTypeProxy(clusterName)
                    }
                },
                spec: {
                    serviceAccountName: "argo-workflow-executor",
                    initContainers: [
                        {
                            name: "install-plugins",
                            image: "opensearchproject/opensearch:3.1.0",
                            command: ["sh", "-c"],
                            args: [
                                `set -euo pipefail
# Copy bundled plugins to shared volume, install repository-s3, then copy it over
cp -r /usr/share/opensearch/plugins/* /plugins/ 2>/dev/null || true
bin/opensearch-plugin install --batch repository-s3
cp -r /usr/share/opensearch/plugins/repository-s3 /plugins/`
                            ],
                            volumeMounts: [
                                {
                                    name: "plugins",
                                    mountPath: "/plugins"
                                }
                            ]
                        }
                    ],
                    containers: [
                        {
                            name: "opensearch",
                            image: "opensearchproject/opensearch:3.1.0",
                            ports: [
                                {
                                    name: "https",
                                    containerPort: 9200
                                }
                            ],
                            env: [
                                {
                                    name: "cluster.name",
                                    value: makeStringTypeProxy(clusterName)
                                },
                                {
                                    name: "discovery.type",
                                    value: "single-node"
                                },
                                {
                                    name: "OPENSEARCH_INITIAL_ADMIN_USERNAME",
                                    valueFrom: {
                                        secretKeyRef: {
                                            name: makeStringTypeProxy(expr.concat(clusterName, expr.literal("-creds"))),
                                            key: "username",
                                            optional: false
                                        }
                                    }
                                },
                                {
                                    name: "OPENSEARCH_INITIAL_ADMIN_PASSWORD",
                                    valueFrom: {
                                        secretKeyRef: {
                                            name: makeStringTypeProxy(expr.concat(clusterName, expr.literal("-creds"))),
                                            key: "password",
                                            optional: false
                                        }
                                    }
                                },
                                {
                                    name: "OPENSEARCH_JAVA_OPTS",
                                    value: "-Xms2g -Xmx2g"
                                }
                            ],
                            resources: {
                                requests: {
                                    cpu: "2",
                                    memory: "4Gi"
                                },
                                limits: {
                                    cpu: "2",
                                    memory: "4Gi"
                                }
                            },
                            readinessProbe: {
                                exec: {
                                    command: [
                                        "sh",
                                        "-c",
                                        'curl -sk -u "${OPENSEARCH_INITIAL_ADMIN_USERNAME}:${OPENSEARCH_INITIAL_ADMIN_PASSWORD}" "https://localhost:9200/_cluster/health?wait_for_status=yellow&timeout=1s"'
                                    ]
                                },
                                initialDelaySeconds: 5,
                                periodSeconds: 5,
                                timeoutSeconds: 3,
                                failureThreshold: 24
                            },
                            volumeMounts: [
                                {
                                    name: "data",
                                    mountPath: "/usr/share/opensearch/data"
                                },
                                {
                                    name: "plugins",
                                    mountPath: "/usr/share/opensearch/plugins"
                                }
                            ]
                        }
                    ],
                    volumes: [
                        {
                            name: "plugins",
                            emptyDir: {}
                        }
                    ]
                }
            },
            volumeClaimTemplates: [
                {
                    metadata: {
                        name: "data"
                    },
                    spec: {
                        accessModes: ["ReadWriteOnce"],
                        resources: {
                            requests: {
                                storage: "1Gi"
                            }
                        }
                    }
                }
            ]
        }
    };
}

export const RFSOpenSearchCluster = WorkflowBuilder.create({
    k8sResourceName: "rfs-opensearch-cluster",
    serviceAccountName: "argo-workflow-executor"
})
    .addParams(CommonWorkflowParameters)

    .addTemplate("createRFSOpenSearchSecret", t => t
        .addRequiredInput("clusterName", typeToken<string>())
        .addResourceTask(b => b
            .setDefinition({
                action: "apply",
                setOwnerReference: true,
                manifest: createRFSOpenSearchSecretManifest(b.inputs.clusterName)
            }))
    )

    .addTemplate("createRFSOpenSearchService", t => t
        .addRequiredInput("clusterName", typeToken<string>())
        .addResourceTask(b => b
            .setDefinition({
                action: "apply",
                setOwnerReference: true,
                manifest: createRFSOpenSearchServiceManifest(b.inputs.clusterName)
            }))
    )

    .addTemplate("createRFSOpenSearchStatefulSet", t => t
        .addRequiredInput("clusterName", typeToken<string>())
        .addResourceTask(b => b
            .setDefinition({
                action: "apply",
                setOwnerReference: true,
                successCondition: "status.readyReplicas > 0",
                manifest: createRFSOpenSearchStatefulSetManifest(b.inputs.clusterName)
            }))
    )

    .addTemplate("createRFSOpenSearchMigrationConfigMap", t => t
        .addRequiredInput("clusterName", typeToken<string>())
        .addResourceTask(b => b
            .setDefinition({
                action: "apply",
                setOwnerReference: true,
                manifest: {
                    apiVersion: "v1",
                    kind: "ConfigMap",
                    metadata: {
                        name: makeStringTypeProxy(expr.concat(b.inputs.clusterName, expr.literal("-migration-config"))),
                        labels: {
                            app: makeStringTypeProxy(b.inputs.clusterName)
                        }
                    },
                    data: {
                        "cluster-config": makeStringTypeProxy(
                            expr.asString(expr.serialize(expr.makeDict({
                                endpoint: expr.concat(expr.literal("https://"), b.inputs.clusterName, expr.literal(":9200")),
                                allowInsecure: expr.literal(true),
                                authConfig: expr.makeDict({
                                    basic: expr.makeDict({
                                        secretName: expr.concat(b.inputs.clusterName, expr.literal("-creds"))
                                    })
                                })
                            })))
                        )
                    }
                }
            }))
        .addJsonPathOutput("clusterConfig", "{.data.cluster-config}", typeToken<string>())
    )

    .addTemplate("waitForRFSOpenSearchReady", t => t
        .addRequiredInput("clusterName", typeToken<string>())
        .addInputsFromRecord(makeRequiredImageParametersForKeys(["MigrationConsole"]))
        .addContainer(c => c
            .addImageInfo(c.inputs.imageMigrationConsoleLocation, c.inputs.imageMigrationConsolePullPolicy)
            .addCommand(["/bin/sh", "-c"])
            .addEnvVarsFromRecord({
                OPENSEARCH_INITIAL_ADMIN_USERNAME: {
                    secretKeyRef: configMapKey(
                        expr.concat(c.inputs.clusterName, expr.literal("-creds")),
                        "username",
                        false
                    ),
                    type: typeToken<string>()
                },
                OPENSEARCH_INITIAL_ADMIN_PASSWORD: {
                    secretKeyRef: configMapKey(
                        expr.concat(c.inputs.clusterName, expr.literal("-creds")),
                        "password",
                        false
                    ),
                    type: typeToken<string>()
                }
            })
            .addResources({
                requests: { cpu: "100m", memory: "128Mi" },
                limits: { cpu: "200m", memory: "256Mi" }
            })
            .addArgs([
                expr.fillTemplate(
                    `set -e
curl -sk -u "$OPENSEARCH_INITIAL_ADMIN_USERNAME:$OPENSEARCH_INITIAL_ADMIN_PASSWORD" "https://{{CLUSTER_NAME}}:9200/_cluster/health?wait_for_status=yellow&timeout=5s"
echo "RFS OpenSearch Cluster is ready!"`,
                    { "CLUSTER_NAME": c.inputs.clusterName }
                )
            ])
        )
        .addRetryParameters({
            limit: "60",
            retryPolicy: "Always",
            backoff: { duration: "5", factor: "2", cap: "30" }
        })
    )

    .addTemplate("createAllRFSOpenSearch", t => t
        .addRequiredInput("clusterName", typeToken<string>())
        .addInputsFromRecord(makeRequiredImageParametersForKeys(["MigrationConsole"]))
        .addSteps(b => b
            .addStep("secret", INTERNAL, "createRFSOpenSearchSecret", c =>
                c.register({ clusterName: b.inputs.clusterName }))
            .addStep("service", INTERNAL, "createRFSOpenSearchService", c =>
                c.register({ clusterName: b.inputs.clusterName }))
            .addStep("statefulset", INTERNAL, "createRFSOpenSearchStatefulSet", c =>
                c.register({ clusterName: b.inputs.clusterName }))
            .addStep("configmap", INTERNAL, "createRFSOpenSearchMigrationConfigMap", c =>
                c.register({ clusterName: b.inputs.clusterName }))
            .addStep("healthCheck", INTERNAL, "waitForRFSOpenSearchReady", c =>
                c.register(selectInputsForRegister(b, c)))
        )
        .addExpressionOutput("rfsOpenSearchConfig", b =>
            expr.serialize(expr.makeDict({
                name: expr.literal("rfsopensearch"),
                endpoint: expr.concat(expr.literal("https://"), b.inputs.clusterName, expr.literal(":9200")),
                allowInsecure: expr.literal(true),
                authConfig: expr.makeDict({
                    basic: expr.makeDict({
                        secretName: expr.concat(b.inputs.clusterName, expr.literal("-creds"))
                    })
                })
            }))
        )
    )

    .addTemplate("deleteRFSOpenSearchStatefulSet", t => t
        .addRequiredInput("clusterName", typeToken<string>())
        .addResourceTask(b => b
            .setDefinition({
                action: "delete",
                flags: ["--ignore-not-found"],
                manifest: {
                    apiVersion: "apps/v1",
                    kind: "StatefulSet",
                    metadata: {
                        name: makeStringTypeProxy(b.inputs.clusterName)
                    }
                }
            }))
    )

    .addTemplate("deleteRFSOpenSearchService", t => t
        .addRequiredInput("clusterName", typeToken<string>())
        .addResourceTask(b => b
            .setDefinition({
                action: "delete",
                flags: ["--ignore-not-found"],
                manifest: {
                    apiVersion: "v1",
                    kind: "Service",
                    metadata: {
                        name: makeStringTypeProxy(b.inputs.clusterName)
                    }
                }
            }))
    )

    .addTemplate("deleteRFSOpenSearchConfigMap", t => t
        .addRequiredInput("clusterName", typeToken<string>())
        .addResourceTask(b => b
            .setDefinition({
                action: "delete",
                flags: ["--ignore-not-found"],
                manifest: {
                    apiVersion: "v1",
                    kind: "ConfigMap",
                    metadata: {
                        name: makeStringTypeProxy(expr.concat(b.inputs.clusterName, expr.literal("-migration-config")))
                    }
                }
            }))
    )

    .addTemplate("deleteRFSOpenSearchSecret", t => t
        .addRequiredInput("clusterName", typeToken<string>())
        .addResourceTask(b => b
            .setDefinition({
                action: "delete",
                flags: ["--ignore-not-found"],
                manifest: {
                    apiVersion: "v1",
                    kind: "Secret",
                    metadata: {
                        name: makeStringTypeProxy(expr.concat(b.inputs.clusterName, expr.literal("-creds")))
                    }
                }
            }))
    )

    .addTemplate("deleteAllRFSOpenSearch", t => t
        .addRequiredInput("clusterName", typeToken<string>())
        .addSteps(b => b
            .addStep("deleteRFSOpenSearchStatefulSet", INTERNAL, "deleteRFSOpenSearchStatefulSet", c =>
                c.register({ clusterName: b.inputs.clusterName }))
            .addStep("deleteRFSOpenSearchService", INTERNAL, "deleteRFSOpenSearchService", c =>
                c.register({ clusterName: b.inputs.clusterName }))
            .addStep("deleteRFSOpenSearchConfigMap", INTERNAL, "deleteRFSOpenSearchConfigMap", c =>
                c.register({ clusterName: b.inputs.clusterName }))
            .addStep("deleteRFSOpenSearchSecret", INTERNAL, "deleteRFSOpenSearchSecret", c =>
                c.register({ clusterName: b.inputs.clusterName }))
        )
    )

    .getFullScope();
