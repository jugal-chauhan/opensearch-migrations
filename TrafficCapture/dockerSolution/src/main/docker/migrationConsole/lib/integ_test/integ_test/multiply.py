import logging
import pytest
import unittest
import json
from http import HTTPStatus
from console_link.middleware.clusters import connection_check, clear_cluster, ConnectionResult
from console_link.models.cluster import Cluster, HttpMethod
from console_link.models.backfill_base import Backfill
from console_link.models.command_result import CommandResult
from console_link.cli import Context
from .default_operations import DefaultOperationsLibrary
from .common_utils import execute_api_call, DEFAULT_INDEX_IGNORE_LIST
from datetime import datetime
import time

logger = logging.getLogger(__name__)
ops = DefaultOperationsLibrary()

def preload_data(source_cluster: Cluster, target_cluster: Cluster):
    """Setup test data"""
    # Confirm source and target connection
    source_con_result: ConnectionResult = connection_check(source_cluster)
    assert source_con_result.connection_established is True
    target_con_result: ConnectionResult = connection_check(target_cluster)
    assert target_con_result.connection_established is True

    # Clear all data from clusters
    clear_cluster(source_cluster)
    clear_cluster(target_cluster)

    # Create source index with settings
    index_settings = {
        "settings": {
            "number_of_shards": "50",
            "number_of_replicas": "1"
        },
        "mappings": {
            "doc": {
                "properties": {
                    "timestamp": {"type": "date"},
                    "value": {"type": "keyword"},
                    "doc_number": {"type": "integer"}
                }
            }
        }
    }

    index_name = f"largetest_{pytest.unique_id}"
    logger.info("Creating index %s with settings: %s", index_name, index_settings)
    
    # Create index on both source and target with same settings
    ops.create_index(cluster=source_cluster, index_name=index_name, data=json.dumps(index_settings))
    ops.create_index(cluster=target_cluster, index_name=index_name, data=json.dumps(index_settings))

    # Create 100 documents with timestamp in bulk
    bulk_data = []
    for i in range(100):
        doc_id = f"doc_{i}"
        bulk_data.extend([
            {"index": {"_index": index_name, "_type": "doc", "_id": doc_id}},
            {
                "timestamp": datetime.now().isoformat(),
                "value": f"test_value_{i}",
                "doc_number": i
            }
        ])
    
    # Bulk index documents
    execute_api_call(
        cluster=source_cluster,
        method=HttpMethod.POST,
        path="/_bulk",
        data="\n".join(json.dumps(d) for d in bulk_data) + "\n",
        headers={"Content-Type": "application/x-ndjson"}
    )
    logger.info("Created 100 documents in bulk in index %s", index_name)


@pytest.fixture(scope="class")
def setup_backfill(request):
    """Test setup with backfill lifecycle management"""
    config_path = request.config.getoption("--config_file_path")
    unique_id = request.config.getoption("--unique_id")
    pytest.console_env = Context(config_path).env
    pytest.unique_id = unique_id

    # Preload data and create target indices
    preload_data(source_cluster=pytest.console_env.source_cluster,
                 target_cluster=pytest.console_env.target_cluster)

    # Start the backfill process
    backfill: Backfill = pytest.console_env.backfill
    assert backfill is not None

    # Create backfill and snapshot
    backfill.create()
    snapshot_result: CommandResult = pytest.console_env.snapshot.create(wait=True)
    assert snapshot_result.success

    # Start and scale backfill
    backfill_start_result: CommandResult = backfill.start()
    assert backfill_start_result.success
    backfill_scale_result: CommandResult = backfill.scale(units=1)
    assert backfill_scale_result.success


@pytest.fixture(scope="session", autouse=True)
def setup_environment(request):
    """Initialize test environment"""
    config_path = request.config.getoption("--config_file_path")
    unique_id = request.config.getoption("--unique_id")
    pytest.console_env = Context(config_path).env
    pytest.unique_id = unique_id
    
    # Setup code
    logger.info("Starting backfill tests...")
    yield
    
    # Teardown code
    logger.info("Stopping backfill...")
    backfill: Backfill = pytest.console_env.backfill
    backfill.stop()


@pytest.mark.usefixtures("setup_backfill")
class BackfillTest(unittest.TestCase):
    """Test backfill functionality"""

    def get_cluster_stats(self, cluster: Cluster, index_name: str = None):
        """Get document count and size stats for a cluster"""
        try:
            if index_name:
                path = f"/{index_name}/_stats"
            else:
                path = "/_stats"

            stats = execute_api_call(cluster=cluster, method=HttpMethod.GET, path=path).json()
            total_docs = stats['_all']['total']['docs']['count']
            total_size_bytes = stats['_all']['total']['store']['size_in_bytes']
            total_size_mb = total_size_bytes / (1024 * 1024)
            
            return total_docs, total_size_mb
        except Exception as e:
            logger.error(f"Error getting cluster stats: {str(e)}")
            return 0, 0

    def wait_for_backfill_completion(self, target_cluster: Cluster, index_name: str):
        """Wait until document count stabilizes or bulk-loader pods terminate"""
        previous_count = -1
        stable_count = 0
        max_stable_checks = 3  # Number of consecutive stable counts needed
        
        for _ in range(30):  # Max 30 attempts
            target_response = execute_api_call(cluster=target_cluster, method=HttpMethod.GET, path=f"/{index_name}/_count?format=json")
            current_count = target_response.json()['count']
            logger.info(f"Current doc count in target index {index_name}: {current_count}")
            
            if current_count == previous_count:
                stable_count += 1
                if stable_count >= max_stable_checks:
                    logger.info(f"Document count stabilized at {current_count} for {max_stable_checks} consecutive checks")
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
        index_name = f"largetest_{pytest.unique_id}"
        backfill = pytest.console_env.backfill

        # Initial source stats
        source_docs, source_size = self.get_cluster_stats(source, index_name)
        logger.info("=== Initial Source Cluster Stats ===")
        logger.info(f"Documents: {source_docs:,}")
        logger.info(f"Index Size: {source_size:.2f} MB")

        # Monitor backfill progress
        previous_count = 0
        stable_count = 0
        max_stable_checks = 3
        check_interval = 30  # seconds
        
        logger.info("\n=== Starting Backfill Monitoring ===")
        for attempt in range(30):  # Max 30 attempts
            target_docs, target_size = self.get_cluster_stats(target, index_name)
            logger.info(f"\nBackfill Progress - Attempt {attempt + 1}/30")
            logger.info(f"Target Documents: {target_docs:,}")
            logger.info(f"Target Index Size: {target_size:.2f} MB")
            logger.info(f"Progress: {(target_docs/source_docs*100):.1f}% of source count")
            
            if target_docs == previous_count:
                stable_count += 1
                logger.info(f"Count stable for {stable_count} checks")
                if stable_count >= max_stable_checks:
                    logger.info("Document count has stabilized")
                    break
            else:
                stable_count = 0
                previous_count = target_docs
            
            time.sleep(check_interval)

        # Stop backfill
        logger.info("\n=== Stopping Backfill ===")
        stop_result: CommandResult = backfill.stop()
        self.assertTrue(stop_result.success, "Failed to stop backfill")
        time.sleep(30)  # Wait for stop to complete

        # Final stats for all indices
        logger.info("\n=== Final Cluster Stats ===")
        source_total_docs, source_total_size = self.get_cluster_stats(source)
        target_total_docs, target_total_size = self.get_cluster_stats(target)
        
        logger.info("Source Cluster:")
        logger.info(f"- Total Documents: {source_total_docs:,}")
        logger.info(f"- Total Size: {source_total_size:.2f} MB")
        
        logger.info("\nTarget Cluster:")
        logger.info(f"- Total Documents: {target_total_docs:,}")
        logger.info(f"- Total Size: {target_total_size:.2f} MB")

        logger.info("\n=== Test Complete ===")
