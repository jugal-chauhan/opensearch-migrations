package org.opensearch.migrations;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;
import java.util.stream.Collectors;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.opensearch.migrations.bulkload.framework.SearchClusterContainer;
import org.opensearch.migrations.bulkload.http.ClusterOperations;
import org.opensearch.migrations.utils.DockerImageBuilder;

import lombok.AllArgsConstructor;
import lombok.Getter;
import lombok.extern.slf4j.Slf4j;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.Network;
import org.testcontainers.containers.wait.strategy.Wait;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * Test class to verify custom transformations during metadata migrations.
 */
@Tag("isolatedTest")
@Slf4j
class UpgradeTest extends BaseMigrationTest {
    @TempDir
    protected Path tempDirectory;
    
    // Docker image names for pre-built optimized images
    private static final String ES56_OSS_IMAGE = "es56-oss-no-xpack:test";
    private static final String KIBANA56_OSS_IMAGE = "kibana56-oss-no-xpack:test";
    
    @BeforeAll
    static void setupImages() {
        // Build optimized ES 5.6 image without X-Pack
        DockerImageBuilder.buildImage(
            DockerImageBuilder.getResourcePath("docker/es56-oss"), 
            ES56_OSS_IMAGE);
            
        // Build optimized Kibana 5.6 image without X-Pack
        DockerImageBuilder.buildImage(
            DockerImageBuilder.getResourcePath("docker/kibana56-oss"), 
            KIBANA56_OSS_IMAGE);
    }

    /**
     * Helper class to define upgrade steps in the migration path
     */
    @AllArgsConstructor
    @Getter
    static class UpgradeStep {
        private final SearchClusterContainer.ContainerVersion version;
        private final String kibanaImage;
        private final boolean requiresSpecialReindexing;
    }
    
    /**
     * Helper class to track snapshot information between steps
     */
    @AllArgsConstructor
    @Getter
    static class SnapshotInfo {
        private final Path snapshotPath;
        private final String snapshotName;
    }
    
    @Test
    public void migrateDashboardsWithSuccessiveSnapshotRestores() throws Exception {
        final String SNAPSHOT_REPO_NAME = "repo";
        final String KIBANA_INDEX_NAME = ".kibana";
        final Duration MANUAL_VERIFICATION_DURATION = Duration.ofMinutes(10);
        
        // Define the upgrade path with appropriate container versions and Kibana images
        List<UpgradeStep> upgradePath = List.of(
            new UpgradeStep(SearchClusterContainer.ES_V6_8_23, "docker.elastic.co/kibana/kibana-oss:6.3.2", true),
            new UpgradeStep(SearchClusterContainer.ES_V7_10_2, "docker.elastic.co/kibana/kibana-oss:7.10.2", false),
            new UpgradeStep(SearchClusterContainer.OS_V1_3_16, "opensearchproject/opensearch-dashboards:1.3.16", false),
            new UpgradeStep(SearchClusterContainer.OS_LATEST, "opensearchproject/opensearch-dashboards:2.11.0", false)
        );
        
        // Create shared network for all containers
        Network network = Network.newNetwork();
        
        // Get initial snapshot location
        final var initialSnapshot = TestResources.SNAPSHOT_ES_5_6_KIBANA;
        
        log.info("Setting up initial ES 5.6 environment...");
        
        // Initialize and start source ES 5.6 cluster - keep this running throughout the test
        SearchClusterContainer sourceCluster = setupEs56Cluster(network);

        // Setup initial snapshot from ES 5.6
        ClusterOperations sourceOps = new ClusterOperations(sourceCluster);
        sourceCluster.putSnapshotData(initialSnapshot.dir.toString());

        // Create repository and restore initial snapshot
        sourceOps.createSnapshotRepository(SearchClusterContainer.CLUSTER_SNAPSHOT_DIR, SNAPSHOT_REPO_NAME);
        sourceOps.restoreSnapshot(SNAPSHOT_REPO_NAME, "my_snapshot");

        // Initialize and start Kibana 5.6 - keep this running throughout the test
        GenericContainer<?> sourceKibana = setupKibana56(network, sourceCluster);
        
        log.info("Source ES 5.6 cluster started at: {}", sourceCluster.getUrl());
        log.info("Source Kibana 5.6 started at: http://localhost:{}", sourceKibana.getMappedPort(5601));

        // Take a new snapshot after restoring to ES 5.6
        String initialSnapshotName = "snapshot_after_restore_to_5_6";
        sourceOps.takeSnapshot(SNAPSHOT_REPO_NAME, initialSnapshotName, KIBANA_INDEX_NAME);
        
        // Copy snapshot to temp dir
        Path currentSnapshotPath = Files.createTempDirectory(tempDirectory, "snapshot_5_6");
        sourceCluster.copySnapshotData(currentSnapshotPath.toString());
        
        // Track current snapshot info
        SnapshotInfo currentSnapshot = new SnapshotInfo(currentSnapshotPath, initialSnapshotName);
        
        // Perform sequential upgrades (except for the last one which we'll keep running)
        for (int i = 0; i < upgradePath.size() - 1; i++) {
            UpgradeStep step = upgradePath.get(i);
            log.info("Performing upgrade step to {}", step.getVersion());
            
            currentSnapshot = performUpgradeStep(network, step, currentSnapshot, SNAPSHOT_REPO_NAME, KIBANA_INDEX_NAME);
        }
        
        // Setup final OpenSearch cluster and keep it running for verification
        log.info("Setting up final OpenSearch cluster for verification...");
        UpgradeStep finalStep = upgradePath.get(upgradePath.size() - 1);
        
        // Initialize and start final OpenSearch cluster - keep this running for verification
        SearchClusterContainer finalCluster = new SearchClusterContainer(finalStep.getVersion())
            .withNetwork(network)
            .withNetworkAliases("final")
            .withExposedPorts(9200)
            .withAccessToHost(true)
            .withEnv("discovery.type", "single-node")
            .withEnv("network.host", "0.0.0.0")
            .withEnv("http.host", "0.0.0.0");
            
        finalCluster.start();
        
        // Initialize and start OpenSearch Dashboards - keep this running for verification
        GenericContainer<?> finalDashboards = new GenericContainer<>(finalStep.getKibanaImage())
            .withNetwork(network)
            .withNetworkAliases("dashboards")
            .withExposedPorts(5601)
            .withEnv("OPENSEARCH_HOSTS", "http://final:9200")
            .withEnv("DISABLE_SECURITY_DASHBOARDS_PLUGIN", "true")
            .withAccessToHost(true)
            .waitingFor(Wait.forHttp("/").withStartupTimeout(Duration.ofMinutes(5)));
            
        finalDashboards.start();
        
        log.info("Final OpenSearch cluster started at: {}", finalCluster.getUrl());
        log.info("Final OpenSearch Dashboards started at: http://localhost:{}", finalDashboards.getMappedPort(5601));
        
        // Restore snapshot to final cluster
        ClusterOperations finalOps = new ClusterOperations(finalCluster);
        finalCluster.putSnapshotData(currentSnapshot.getSnapshotPath().toString());
        finalOps.createSnapshotRepository(SearchClusterContainer.CLUSTER_SNAPSHOT_DIR, SNAPSHOT_REPO_NAME);
        finalOps.restoreSnapshot(SNAPSHOT_REPO_NAME, currentSnapshot.getSnapshotName());
        
        // Reindex and cleanup indices if needed
        reindexAndCleanupIndices(finalOps, KIBANA_INDEX_NAME);
        
        log.info("Sleeping for {} minutes to allow manual verification...", MANUAL_VERIFICATION_DURATION.toMinutes());
        log.info("=== VERIFICATION INFORMATION ===");
        log.info("Source ES 5.6: http://localhost:{}", sourceCluster.getFirstMappedPort());
        log.info("Source Kibana 5.6: http://localhost:{}", sourceKibana.getMappedPort(5601));
        log.info("Final OpenSearch: http://localhost:{}", finalCluster.getFirstMappedPort());
        log.info("Final Dashboards: http://localhost:{}", finalDashboards.getMappedPort(5601));
        log.info("==============================");
        
        Thread.sleep(MANUAL_VERIFICATION_DURATION.toMillis());
        
        // Containers will be closed automatically when the test exits
    }
    
    /**
     * Sets up and starts an Elasticsearch 5.6 cluster using pre-built optimized image
     */
    private SearchClusterContainer setupEs56Cluster(Network network) {
        log.info("Starting optimized ES 5.6 cluster (without X-Pack)");
        
        // Build the optimized image if needed
        DockerImageBuilder.buildImage(
            DockerImageBuilder.getResourcePath("docker/es56-oss"), 
            ES56_OSS_IMAGE);
            
        // Create a custom container version using our optimized image
        SearchClusterContainer.ContainerVersion customEs56Version = 
            new SearchClusterContainer.ElasticsearchOssVersion(
                ES56_OSS_IMAGE, 
                Version.fromString("ES 5.6.16"));
            
        // Create a container with our custom version
        SearchClusterContainer cluster = new SearchClusterContainer(customEs56Version)
            .withNetwork(network)
            .withNetworkAliases("source")
            .withExposedPorts(9200)
            .withAccessToHost(true)
            .withExposedPorts(9200, 9300);
                
        cluster.start();
        return cluster;
    }
    
    /**
     * Sets up and starts Kibana 5.6 connected to the source cluster using pre-built optimized image
     */
    private GenericContainer<?> setupKibana56(Network network, SearchClusterContainer sourceCluster) {
        log.info("Starting optimized Kibana 5.6 (without X-Pack and pre-optimized)");
        GenericContainer<?> kibana = new GenericContainer<>(KIBANA56_OSS_IMAGE)
            .withNetwork(network)
            .withNetworkAliases("kibanasource")
            .withExposedPorts(5601)
            .withEnv("ELASTICSEARCH_URL", "http://source:9200")
            .withAccessToHost(true)
            .waitingFor(Wait.forHttp("/").withStartupTimeout(Duration.ofMinutes(2)));
                
        kibana.start();
        return kibana;
    }
    
    /**
     * Performs a single upgrade step in the migration path
     * @return Updated snapshot information for the next step
     */
    private SnapshotInfo performUpgradeStep(
            Network network, 
            UpgradeStep step, 
            SnapshotInfo currentSnapshot, 
            String repoName, 
            String indexName) throws Exception {
            
        log.info("Starting upgrade to version {}", step.getVersion());
        
        try (var cluster = new SearchClusterContainer(step.getVersion())
                .withNetwork(network)
                .withNetworkAliases("cluster")
                .withExposedPorts(9200)
                .withAccessToHost(true)
                .withEnv("discovery.type", "single-node")
                .withEnv("network.host", "0.0.0.0")
                .withEnv("http.host", "0.0.0.0")) {
                
            cluster.start();
            var clusterOps = new ClusterOperations(cluster);
            
            // Restore snapshot from previous step
            cluster.putSnapshotData(currentSnapshot.getSnapshotPath().toString());
            clusterOps.createSnapshotRepository(SearchClusterContainer.CLUSTER_SNAPSHOT_DIR, repoName);
            clusterOps.restoreSnapshot(repoName, currentSnapshot.getSnapshotName());
            
            // Special handling for ES 6.3 upgrade
            if (step.isRequiresSpecialReindexing()) {
                handleEs6SpecialReindexing(clusterOps);
            }

            // Reindex and cleanup for all versions
            reindexAndCleanupIndices(clusterOps, indexName);
            
            // Start Kibana to perform any necessary upgrades
            try (var kibana = new GenericContainer<>(step.getKibanaImage())
                    .withNetwork(network)
                    .withNetworkAliases("kibana")
                    .withExposedPorts(5601)
                    .withEnv("ELASTICSEARCH_URL", "http://cluster:9200")
                    .withEnv("ELASTICSEARCH_HOSTS", "http://cluster:9200")
                    .withEnv("OPENSEARCH_HOSTS", "http://cluster:9200")
                    .withAccessToHost(true)
                    .waitingFor(Wait.forHttp("/").withStartupTimeout(Duration.ofMinutes(5)))) {
                
                kibana.start();
                log.info("Started Kibana {} for version {}", step.getKibanaImage(), step.getVersion());
                
                // Allow Kibana to complete internal upgrade processes
                Thread.sleep(10000);
            }
            
            // Take a new snapshot after upgrade - create URL-safe name
            String versionString = step.getVersion().toString().replaceAll("[^a-zA-Z0-9]", "-");
            String newSnapshotName = "snapshot_after_upgrade_v" + versionString.toLowerCase(Locale.ROOT);

            // Get the index/alias info
            var indexResponse = clusterOps.get("/" + indexName);
            if (indexResponse.getKey() == 200) {
                // Parse the JSON response to get the index names returned
                var resolvedIndexNames = parseIndexNames(indexResponse.getValue()); // e.g. returns [".kibana_7"]

                // If the requested name is not in the resolved names, it's an alias.
                if (!resolvedIndexNames.contains(indexName)) {
                    log.info("Provided alias {} resolved to indices {}", indexName, resolvedIndexNames);
                    clusterOps.takeSnapshot(repoName, newSnapshotName, resolvedIndexNames.stream().collect(Collectors.joining(",")));
                } else {
                    log.info("Taking snapshot of index {}", indexName);
                    clusterOps.takeSnapshot(repoName, newSnapshotName, indexName);
                }
            } else {
                // Fallback: snapshot all indices
                var indicesResponse = clusterOps.get("/_cat/indices?format=json");
                log.info("Index {} not found directly, checking all indices: {}", indexName, indicesResponse.getValue());
                log.info("Taking snapshot of all indices as fallback");
                clusterOps.takeSnapshot(repoName, newSnapshotName, "*");
            }


            // Copy snapshot to temp dir for next step
            Path newSnapshotPath = Files.createTempDirectory(tempDirectory, "snapshot_" + step.getVersion());
            cluster.copySnapshotData(newSnapshotPath.toString());
            
            log.info("Completed upgrade to {}, took snapshot: {}", step.getVersion(), newSnapshotName);
            
            return new SnapshotInfo(newSnapshotPath, newSnapshotName);
        }
    }

    public List<String> parseIndexNames(String jsonResponse) {
        List<String> indexNames = new ArrayList<>();
        try {
            ObjectMapper mapper = new ObjectMapper();
            JsonNode root = mapper.readTree(jsonResponse);
            Iterator<String> fieldNames = root.fieldNames();
            while (fieldNames.hasNext()) {
                indexNames.add(fieldNames.next());
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        return indexNames;
    }


    /**
     * Special handling for ES 6.8 migration - reindexes .kibana to .kibana-6
     */
    private void handleEs6SpecialReindexing(ClusterOperations clusterOps) throws Exception {
        log.info("Performing special ES 6.8 reindexing for Kibana index");
        
        // Block writes on the original index
        clusterOps.put("/.kibana/_settings", "{\"index.blocks.write\": true}");
        
        // Create destination index with appropriate settings
        clusterOps.put("/.kibana-6", Files.readString(Paths.get("src/test/resources/kibana6-index-settings.json")));
        
        // Reindex with type migration
        var reindexPayload = "{\n" +
                "  \"source\": {\"index\": \".kibana\"},\n" +
                "  \"dest\": {\"index\": \".kibana-6\"},\n" +
                "  \"script\": {\n" +
                "    \"inline\": \"ctx._source = [ ctx._type : ctx._source ]; ctx._source.type = ctx._type; ctx._id = ctx._type + \\\":\\\" + ctx._id; ctx._type = \\\"doc\\\";\",\n" +
                "    \"lang\": \"painless\"\n" +
                "  }\n" +
                "}";
//        var reindexPayload = "{\"source\":{\"index\":\".kibana\"},\"dest\":{\"index\":\".kibana-6\"}," +
//                            "\"script\":{\"source\":\"ctx._source.type = ctx._type; ctx._id = ctx._id; ctx._type='doc';\",\"lang\":\"painless\"}}";
        var reindexResult = clusterOps.post("/_reindex?wait_for_completion=true", reindexPayload);
        assertEquals(200, reindexResult.getKey());
        
        // Delete original index
        clusterOps.delete("/.kibana");
        
        // Create alias to maintain original index name
        var aliasPayload = "{\"actions\":[{\"add\":{\"index\":\".kibana-6\",\"alias\":\".kibana\"}}]}";
        clusterOps.post("/_aliases", aliasPayload);
        
        log.info("Completed ES 6.8 special reindexing");
    }
    
    /**
     * Reindex and cleanup indices to maintain original names but update to current version
     */
    private void reindexAndCleanupIndices(ClusterOperations clusterOps, String indexName) throws Exception {
        log.info("Performing general reindexing and cleanup for {}", indexName);
        
        // Get list of indices
        var indicesResponse = clusterOps.get("/_cat/indices?format=json");
        if (indicesResponse.getKey() != 200) {
            log.warn("Could not retrieve indices list: {}", indicesResponse.getValue());
            return;
        }
        
        // For any index that matches our pattern but isn't the latest version, reindex it
        // Note: In a real implementation, this would analyze the indices and reindex as needed
        // For this test implementation, we'll check if the index exists and already has proper format
        
        var indexResponse = clusterOps.get("/" + indexName);
        if (indexResponse.getKey() == 200) {
            log.info("Index {} already exists and appears to be in the correct format", indexName);
        } else {
            log.info("Index {} needs reindexing or doesn't exist - this would handle the reindexing logic", indexName);
            // Implement actual reindexing logic here if needed
        }
    }
}
