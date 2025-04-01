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

    # Delete existing snapshot if it exists
    try:
        execute_api_call(
            cluster=pytest.console_env.source_cluster,
            method=HttpMethod.DELETE,
            path="/_snapshot/migration_assistant_repo/migration-assistant-snapshot",
            expected_status_code=200
        )
        logger.info("Deleted existing snapshot")
    except Exception as e:
        logger.info(f"No existing snapshot to delete or error deleting: {e}")

    # Create backfill and snapshot
    backfill.create()
    snapshot_result: CommandResult = pytest.console_env.snapshot.create(wait=True)
    assert snapshot_result.success

    # Start and scale backfill
    backfill_start_result: CommandResult = backfill.start()
    assert backfill_start_result.success
    backfill_scale_result: CommandResult = backfill.scale(units=2)
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
        source = pytest.console_env.source_cluster
        target = pytest.console_env.target_cluster
        index_name = f"largetest_{pytest.unique_id}"

        # Verify source data
        source_response = execute_api_call(cluster=source, method=HttpMethod.GET, path=f"/{index_name}/_count?format=json")
        source_count = source_response.json()['count']
        logger.info(f"Source index {index_name} document count: {source_count}")
        self.assertEqual(100, source_count, "Source should have 100 documents")

        # Wait for backfill completion
        self.wait_for_backfill_completion(target, index_name)

        # Log final target stats
        target_response = execute_api_call(cluster=target, method=HttpMethod.GET, path=f"/{index_name}/_count?format=json")
        target_count = target_response.json()['count']
        logger.info(f"Target index {index_name} final document count: {target_count}")
        
        # Get index stats for size information
        target_stats = execute_api_call(cluster=target, method=HttpMethod.GET, path=f"/{index_name}/_stats").json()
        logger.info(f"Target index {index_name} stats: {json.dumps(target_stats, indent=2)}")

        # Verify a sample of document contents
        sample_indices = [0, 10, 50, 99]  # Sample from original docs
        for i in sample_indices:
            doc_id = f"doc_{i}"
            # Verify original document was copied
            ops.check_doc_match(
                test_case=self,
                index_name=index_name,
                doc_id=doc_id,
                source_cluster=source,
                target_cluster=target
            )
