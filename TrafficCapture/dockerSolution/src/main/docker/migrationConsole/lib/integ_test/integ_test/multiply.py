import logging
import pytest
import unittest
import json
from http import HTTPStatus
from console_link.middleware.clusters import connection_check, clear_cluster, ConnectionResult
from console_link.models.cluster import Cluster
from console_link.models.backfill_base import Backfill
from console_link.models.command_result import CommandResult
from console_link.models.metadata import Metadata
from console_link.cli import Context
from .default_operations import DefaultOperationsLibrary
from datetime import datetime
import time

logger = logging.getLogger(__name__)
ops = DefaultOperationsLibrary()

def create_target_index(source_cluster: Cluster, target_cluster: Cluster, index_name: str):
    """Create target index with same settings as source"""
    # Get source index settings
    source_settings_response = ops.execute_api_call(cluster=source_cluster, path=f"/{index_name}/_settings")
    source_settings = source_settings_response.json()
    logger.info("Source index settings for %s: %s", index_name, source_settings)

    # Create target index with same settings
    target_settings = {
        "settings": {
            "number_of_shards": source_settings[index_name]["settings"]["index"]["number_of_shards"],
            "number_of_replicas": source_settings[index_name]["settings"]["index"]["number_of_replicas"]
        },
        "mappings": source_settings[index_name]["mappings"]
    }
    logger.info("Creating target index %s with settings: %s", index_name, target_settings)
    ops.create_index(cluster=target_cluster, index_name=index_name, data=json.dumps(target_settings))


def create_test_data(source_cluster: Cluster):
    """Create test index and documents in source cluster"""
    # Create index with 50 shards
    index_settings = {
        "settings": {
            "number_of_shards": 50,
            "number_of_replicas": 1
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
    logger.info("Creating largetest index with settings: %s", index_settings)
    ops.create_index(cluster=source_cluster, index_name="largetest", data=json.dumps(index_settings))

    # Create 10 documents with timestamp
    for i in range(10):
        doc_id = f"doc_{i}"
        doc_body = {
            "timestamp": datetime.now().isoformat(),
            "value": f"test_value_{i}",
            "doc_number": i
        }
        ops.create_document(
            cluster=source_cluster,
            index_name="largetest",
            doc_id=doc_id,
            doc_type="doc",
            data=doc_body
        )
    logger.info("Created 10 documents in largetest index")


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
    logger.info("Creating largetest index with settings: %s", index_settings)
    ops.create_index(cluster=source_cluster, index_name="largetest", data=json.dumps(index_settings))

    # Create 10 documents with timestamp
    for i in range(10):
        doc_id = f"doc_{i}"
        doc_body = {
            "timestamp": datetime.now().isoformat(),
            "value": f"test_value_{i}",
            "doc_number": i
        }
        ops.create_document(
            cluster=source_cluster,
            index_name="largetest",
            doc_id=doc_id,
            data=doc_body,
            doc_type="doc"
        )
    logger.info("Created 10 documents in largetest index")


@pytest.fixture(scope="session", autouse=True)
def setup_environment(request):
    """Initialize test environment"""
    config_path = request.config.getoption("--config_file_path")
    pytest.console_env = Context(config_path).env
    
    # Setup code
    logger.info("Starting backfill tests...")
    yield
    
    # Teardown code
    logger.info("Stopping backfill...")
    backfill: Backfill = pytest.console_env.backfill
    backfill.stop()


@pytest.fixture(scope="class")
def setup_backfill(setup_environment):
    """Test setup with backfill lifecycle management"""
    # Preload benchmark data and create target indices
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
    
    # Scale to 2 workers
    backfill_scale_result: CommandResult = backfill.scale(units=2)
    assert backfill_scale_result.success


@pytest.mark.usefixtures("setup_backfill")
class BackfillTest(unittest.TestCase):
    """Test backfill functionality"""

    def wait_for_backfill_completion(self, target_cluster: Cluster):
        """Wait until document count stabilizes"""
        previous_count = -1
        for _ in range(30):
            current_count = ops.get_doc_count(target_cluster, "largetest")
            if current_count == previous_count and current_count > 0:
                return
            previous_count = current_count
            time.sleep(30)
        self.fail("Backfill did not complete within timeout")

    def test_benchmark_data_migration(self):
        source = pytest.console_env.source_cluster
        target = pytest.console_env.target_cluster

        # Verify data exists on source
        source_count = ops.get_doc_count(source, "largetest")
        self.assertEqual(10, source_count, "Source should have 10 documents")

        # Wait for backfill completion
        self.wait_for_backfill_completion(target)

        # Verify data is backfilled to target
        target_count = ops.get_doc_count(target, "largetest")
        self.assertEqual(10000, target_count, "Target should have 10000 documents (10 source docs * 1000 multiplier)")

        # Verify a sample of document contents
        # Check first doc, last doc, and a few in between
        sample_indices = [0, 100, 1000, 5000, 9999]
        for i in sample_indices:
            doc_id = f"doc_{i}"
            # First verify source doc exists
            if i < 10:  # Only first 10 docs exist in source
                ops.check_doc_match(
                    test_case=self,
                    index_name="largetest",
                    doc_id=doc_id,
                    source_cluster=source,
                    target_cluster=target
                )
            else:
                # For multiplied docs, just verify they exist in target
                response = ops.get_document(
                    index_name="largetest",
                    doc_id=doc_id,
                    cluster=target
                )
                self.assertEqual(200, response.status_code, f"Document {doc_id} should exist in target")
