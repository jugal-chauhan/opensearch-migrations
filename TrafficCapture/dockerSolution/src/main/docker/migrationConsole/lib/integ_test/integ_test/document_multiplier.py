import logging
import pytest
import unittest
import json
from console_link.middleware.clusters import connection_check, clear_cluster, clear_snapshots, delete_repo, ConnectionResult
from console_link.models.cluster import Cluster, HttpMethod
from console_link.models.backfill_base import Backfill
from console_link.models.command_result import CommandResult
from console_link.models.snapshot import Snapshot
from console_link.cli import Context
from .default_operations import DefaultOperationsLibrary
from .common_utils import execute_api_call
from datetime import datetime
import time
import shutil

# Global configuration
NUM_SHARDS = 50
MULTIPLICATION_FACTOR = 9999  # N in transformation
BATCH_COUNT = 10  # j range
DOCS_PER_BATCH = 200000  # i range
TOTAL_SOURCE_DOCS = BATCH_COUNT * DOCS_PER_BATCH  # 2M source documents
TOTAL_TARGET_DOCS = TOTAL_SOURCE_DOCS * (MULTIPLICATION_FACTOR + 1)  # +1 because transformation keeps original doc (2M * 10000 = 20B docs)

logger = logging.getLogger(__name__)

class CliContext:
    """Context object for CLI commands"""
    def __init__(self, env):
        self.env = env
        self.json = False

ops = DefaultOperationsLibrary()

def preload_data(source_cluster: Cluster, target_cluster: Cluster):
    """Setup test data"""
    # Confirm source and target connection
    source_con_result: ConnectionResult = connection_check(source_cluster)
    assert source_con_result.connection_established is True
    target_con_result: ConnectionResult = connection_check(target_cluster)
    assert target_con_result.connection_established is True

    # Clear indices and snapshots at the start
    logger.info("Clearing indices and snapshots before starting test...")
    clear_cluster(source_cluster)
    clear_cluster(target_cluster)

    # Cleanup generated transformation files
    try:
        shutil.rmtree("/shared-logs-output/test-transformations")
        logger.info("Removed existing /shared-logs-output/test-transformations directory")
    except FileNotFoundError:
        logger.info("No transformation files detected to cleanup")

    # Create transformation.json file
    transform_config_data = [
        {
            "JsonJSTransformerProvider": {
                "initializationScript": f"function transform(document, context) {{\n  if (!document) {{\n    throw new Error(\"No source_document was defined - nothing to transform!\");\n  }}\n\n  const indexCommandMap = document.get(\"index\");\n  const sourceDocumentMap = document.get(\"source\");\n  const originalId = indexCommandMap.get(\"_id\");\n  const N = {MULTIPLICATION_FACTOR};\n\n  const results = [document];\n\n  for (let i = 1; i <= N; i++) {{\n    const newIndexMap = new Map(indexCommandMap);\n    newIndexMap.set(\"_id\", `${{originalId}}_${{i}}`);\n    newIndexMap.set(\"_index\", indexCommandMap.get(\"_index\").replace(\"largetest\", \"new_largetest\"));\n\n    const newSourceMap = new Map(sourceDocumentMap);\n    newSourceMap.set(\"doc_number\", i);\n\n    results.push(new Map([\n      [\"index\", newIndexMap],\n      [\"source\", newSourceMap]\n    ]));\n  }}\n\n  return results;\n}}\n\nfunction main(context) {{\n  console.log(\"Context: \", JSON.stringify(context, null, 2));\n  return (document) => {{\n    if (Array.isArray(document)) {{\n      return document.flatMap(item => transform(item, context));\n    }}\n    return transform(document, context);\n  }};\n}}\n\n(() => main)();",
                "bindingsObject": {}
            }
        }
    ]
    ops.create_transformation_json_file(transform_config_data, "/shared-logs-output/test-transformations/transformation.json")

    # Create source index with settings for ES 5.6
    index_settings = {
        "settings": {
            "number_of_shards": str(NUM_SHARDS),
            "number_of_replicas": "1"
        },
        "mappings": {
            "doc": {  # ES 5.6 type mapping
                "properties": {
                    "timestamp": {"type": "date"},
                    "value": {"type": "keyword"},
                    "doc_number": {"type": "integer"},
                    "description": {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}},
                    "metadata": {
                        "properties": {  # ES 5.6 nested object mapping
                            "tags": {"type": "keyword"},
                            "category": {"type": "keyword"},
                            "subcategories": {"type": "keyword"},
                            "attributes": {"type": "keyword"},
                            "status": {"type": "keyword"},
                            "version": {"type": "keyword"},
                            "region": {"type": "keyword"},
                            "details": {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}}
                        }
                    },
                    "content": {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}},
                    "additional_info": {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}}
                }
            }
        }
    }

    index_name_source = f"largetest_{pytest.unique_id}"
    index_name_target = f"new_largetest_{pytest.unique_id}"
    logger.info("Creating index %s with settings: %s", index_name_source, index_settings)
    
    # Create index on both source and target with same settings
    ops.create_index_es56(cluster=source_cluster, index_name=index_name_source, data=json.dumps(index_settings))
    ops.create_index_es56(cluster=source_cluster, index_name=index_name_target, data=json.dumps(index_settings))
    
    # Create documents with timestamp in bulk
    for j in range(BATCH_COUNT):
        bulk_data = []
        for i in range(DOCS_PER_BATCH):
            doc_id = f"doc_{j}_{i}"
            bulk_data.extend([
                {"index": {"_index": index_name_source, "_type": "doc", "_id": doc_id}},
                {
                    "timestamp": datetime.now().isoformat(),
                    "value": f"test_value_{i}",
                    "doc_number": i,
                    "description": f"This is a detailed description for document {doc_id} containing information about the test data and its purpose in the migration process.",
                    "metadata": {
                        "tags": [f"tag1_{i}", f"tag2_{i}", f"tag3_{i}"],
                        "category": f"category_{i % 10}",
                        "subcategories": [f"subcat1_{i % 5}", f"subcat2_{i % 5}"],
                        "attributes": [f"attr1_{i % 8}", f"attr2_{i % 8}"],
                        "status": f"status_{i % 6}",
                        "version": f"1.{i % 10}.{i % 5}",
                        "region": f"region_{i % 12}",
                        "details": f"Detailed metadata information for document {doc_id} including test parameters."
                    },
                    "content": f"Main content for document {doc_id}. This section contains the primary information and data relevant to the testing process. The content is designed to test the system's ability to handle substantial amounts of text data.",
                    "additional_info": f"Supplementary information for document {doc_id} providing extra context and details about the test data."
                }
            ])
        execute_api_call(
            cluster=source_cluster,
            method=HttpMethod.POST,
            path="/_bulk",
            data="\n".join(json.dumps(d) for d in bulk_data) + "\n",
            headers={"Content-Type": "application/x-ndjson"}
        )
    
    # Bulk index documents
    

    execute_api_call(
        cluster=source_cluster,
        method=HttpMethod.POST,
        path="/_refresh"
    )

    logger.info("Created 100 documents in bulk in index %s", index_name_source)



@pytest.fixture(scope="class")
def setup_backfill(request):
    """Test setup with backfill lifecycle management"""
    pytest.console_env = Context(request.config.getoption("--config_file_path")).env
    pytest.unique_id = request.config.getoption("--unique_id")

    # Preload data and create target indices
    preload_data(source_cluster=pytest.console_env.source_cluster,
                 target_cluster=pytest.console_env.target_cluster)

    # Get components
    backfill: Backfill = pytest.console_env.backfill
    assert backfill is not None
    snapshot: Snapshot = pytest.console_env.snapshot
    assert snapshot is not None

    # Initialize backfill first (creates .migrations_working_state)
    backfill_create_result: CommandResult = backfill.create()
    assert backfill_create_result.success
    logger.info("Backfill initialized successfully")

    # Create snapshot and wait for completion
    snapshot_result: CommandResult = snapshot.create(wait=True)
    assert snapshot_result.success
    logger.info("Snapshot creation completed successfully")

    # Start backfill process
    backfill_start_result: CommandResult = backfill.start()
    assert backfill_start_result.success
    logger.info("Backfill started successfully")

    # Scale up backfill workers
    backfill_scale_result: CommandResult = backfill.scale(5)
    assert backfill_scale_result.success
    logger.info("Backfill scaled successfully")

    yield

    # Cleanup - stop backfill and clean snapshots
    logger.info("Cleaning up test environment...")
    try:
        backfill.stop()
        clear_snapshots(pytest.console_env.source_cluster, "migration_assistant_repo")
        delete_repo(pytest.console_env.source_cluster, "migration_assistant_repo")
        logger.info("Backfill stopped and snapshots cleaned up.")
    except Exception as e:
        logger.error(f"Error during cleanup: {str(e)}")


@pytest.fixture(scope="session", autouse=True)
def setup_environment(request):
    """Initialize test environment"""
    config_path = request.config.getoption("--config_file_path")
    unique_id = request.config.getoption("--unique_id")
    pytest.console_env = Context(config_path).env
    pytest.unique_id = unique_id
    
    logger.info("Starting backfill tests...")
    yield
    # Note: Individual tests handle their own cleanup
    logger.info("Test environment teardown complete")


@pytest.mark.usefixtures("setup_backfill")
class BackfillTest(unittest.TestCase):
    """Test backfill functionality"""

    def get_cluster_stats(self, cluster: Cluster, index_name_source: str = None):
        """Get document count and size stats for a cluster (primary shards only)"""
        try:
            if index_name_source:
                path = f"/{index_name_source}/_stats"
            else:
                path = "/_stats"

            stats = execute_api_call(cluster=cluster, method=HttpMethod.GET, path=path).json()
            total_docs = stats['_all']['primaries']['docs']['count']
            total_size_bytes = stats['_all']['primaries']['store']['size_in_bytes']
            total_size_mb = total_size_bytes / (1024 * 1024)
            
            return total_docs, total_size_mb
        except Exception as e:
            logger.error(f"Error getting cluster stats: {str(e)}")
            return 0, 0

    def wait_for_backfill_completion(self, target_cluster: Cluster, index_name_target: str):
        """Wait until document count stabilizes or bulk-loader pods terminate"""
        previous_count = 0
        stable_count = 0
        max_stable_checks = 2  # Reduced from 3 to 2 consecutive stable counts needed
        
        for attempt in range(80):  # Max 30 attempts
            target_response = execute_api_call(cluster=target_cluster, method=HttpMethod.GET, path=f"/{index_name_target}/_count?format=json")
            current_count = target_response.json()['count']
            
            # Get bulk loader pod status
            try:
                bulk_loader_pods = execute_api_call(
                    cluster=target_cluster,
                    method=HttpMethod.GET,
                    path="/_cat/tasks?detailed",
                    headers={"Accept": "application/json"}
                ).json()
                bulk_loader_active = any(task.get('action', '').startswith('indices:data/write/bulk') for task in bulk_loader_pods)
            except Exception as e:
                logger.warning(f"Failed to check bulk loader status: {e}")
                bulk_loader_active = True  # Assume active if we can't check
            
            logger.info(f"Backfill Progress - Attempt {attempt + 1}/30:")
            logger.info(f"- Current doc count: {current_count:,}")
            logger.info(f"- Bulk loader active: {bulk_loader_active}")
            
            # Don't consider it stable if count is 0 and bulk loader is still active
            if current_count == 0 and bulk_loader_active:
                logger.info("Waiting for documents to start appearing...")
                stable_count = 0
            # Only consider it stable if count matches previous and is non-zero
            elif current_count == previous_count and current_count > 0:
                stable_count += 1
                logger.info(f"Count stable at {current_count:,} for {stable_count}/{max_stable_checks} checks")
                if stable_count >= max_stable_checks:
                    logger.info(f"Document count stabilized at {current_count:,} for {max_stable_checks} consecutive checks")
                    return
            else:
                stable_count = 0
                
            previous_count = current_count
            time.sleep(30)
        
        logger.warning("Backfill monitoring timed out after 30 attempts")

    def test_data_multiplication(self):
        """Monitor backfill progress and report final stats"""
        source = pytest.console_env.source_cluster
        target = pytest.console_env.target_cluster
        index_name_source = f"largetest_{pytest.unique_id}"
        index_name_target = f"new_largetest_{pytest.unique_id}"
        backfill = pytest.console_env.backfill

        logger.info("\n" + "="*50)
        logger.info("Starting Document Multiplication Test")
        logger.info("="*50)

        # Initial source stats
        source_docs, source_size = self.get_cluster_stats(source, index_name_source)
        logger.info("\n=== Initial Source Cluster Stats ===")
        logger.info(f"Source Index: {index_name_source}")
        logger.info(f"Documents: {source_docs:,}")
        logger.info(f"Index Size: {source_size:.2f} MB")

        logger.info("\n=== Starting Backfill Process ===")
        logger.info(f"Target Index: {index_name_target}")
        logger.info(f"Expected Document Multiplication Factor: {MULTIPLICATION_FACTOR}")
        logger.info(f"Expected Final Document Count: {TOTAL_SOURCE_DOCS * MULTIPLICATION_FACTOR:,}")

        # Start and scale backfill
        logger.info("Starting backfill...")
        backfill_start_result: CommandResult = backfill.start()
        assert backfill_start_result.success, f"Failed to start backfill: {backfill_start_result.error}"

        logger.info("Scaling backfill...")
        backfill_scale_result: CommandResult = backfill.scale(units=1)
        assert backfill_scale_result.success, f"Failed to scale backfill: {backfill_scale_result.error}"

        # Wait for backfill to complete
        logger.info("\n=== Monitoring Backfill Progress ===")
        self.wait_for_backfill_completion(target, index_name_target)

        # Get final stats
        logger.info("\n=== Final Cluster Stats ===")
        source_total_docs, source_total_size = self.get_cluster_stats(source, index_name_source)
        target_total_docs, target_total_size = self.get_cluster_stats(target, index_name_target)
        
        logger.info("\nSource Cluster:")
        logger.info(f"- Index: {index_name_source}")
        logger.info(f"- Total Documents: {source_total_docs:,}")
        logger.info(f"- Total Size: {source_total_size:.2f} MB")
        
        logger.info("\nTarget Cluster:")
        logger.info(f"- Index: {index_name_target}")
        logger.info(f"- Total Documents: {target_total_docs:,}")
        logger.info(f"- Total Size: {target_total_size:.2f} MB")
        logger.info(f"- Multiplication Factor Achieved: {target_total_docs/source_total_docs:.2f}x")

        # Assert that documents were actually migrated
        assert target_total_docs > 0, "No documents were migrated to target index"
        assert target_total_docs == TOTAL_SOURCE_DOCS * MULTIPLICATION_FACTOR, f"Document count mismatch: source={source_total_docs}, target={target_total_docs}"

        # Stop backfill using the API directly
        logger.info("\n=== Stopping Backfill ===")
        stop_result = backfill.stop()
        assert stop_result.success, f"Failed to stop backfill: {stop_result.error}"
        logger.info("Backfill stopped successfully")

        logger.info("\n=== Test Completed Successfully ===")
        logger.info("Document multiplication verified with correct count")
