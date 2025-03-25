import logging
import pytest
import unittest
import json
from http import HTTPStatus
from pathlib import Path
from console_link.middleware.clusters import connection_check, clear_cluster, ConnectionResult, run_test_benchmarks
from console_link.models.cluster import Cluster
from console_link.models.backfill_base import Backfill
from console_link.models.command_result import CommandResult
from console_link.cli import Context
from .default_operations import DefaultOperationsLibrary
from .common_utils import EXPECTED_BENCHMARK_DOCS

logger = logging.getLogger(__name__)
ops = DefaultOperationsLibrary()

def create_target_index(source_cluster: Cluster, target_cluster: Cluster, index_name: str):
    """Create index in target with same settings as source"""
    # Get source index settings
    source_settings = ops.get_index_settings(cluster=source_cluster, index_name=index_name)
    logger.info(f"Source index settings for {index_name}: {source_settings}")
    
    # Extract number of shards and replicas
    number_of_shards = source_settings[index_name]["settings"]["index"]["number_of_shards"]
    number_of_replicas = source_settings[index_name]["settings"]["index"]["number_of_replicas"]
    
    # Create index in target with same settings
    create_index_body = {
        "settings": {
            "number_of_shards": number_of_shards,
            "number_of_replicas": number_of_replicas
        }
    }
    logger.info(f"Creating target index {index_name} with settings: {create_index_body}")
    ops.create_index(cluster=target_cluster, index_name=index_name, body=create_index_body)

def preload_data(source_cluster: Cluster, target_cluster: Cluster):
    """
    Setup test data and indices using benchmark data
    source_cluster and target_cluster are provided by the test config and console environment
    """
    # Confirm source and target connection
    source_con_result: ConnectionResult = connection_check(source_cluster)
    assert source_con_result.connection_established is True
    target_con_result: ConnectionResult = connection_check(target_cluster)
    assert target_con_result.connection_established is True

    # Clear all data from clusters
    clear_cluster(source_cluster)
    clear_cluster(target_cluster)

    # Load benchmark data into source
    logger.info("Loading benchmark data into source cluster...")
    run_test_benchmarks(source_cluster)

    # Create matching indices in target
    logger.info("Creating matching indices in target cluster...")
    for index_name in EXPECTED_BENCHMARK_DOCS.keys():
        create_target_index(source_cluster, target_cluster, index_name)

def print_migration_commands():
    """Print the migration console commands that will be executed"""
    commands = [
        "console snapshot create",  # Creates snapshot of source indices
        "console backfill start",   # Starts the backfill process
        "console backfill scale 10",  # Scales the backfill to 10 units
        "console backfill stop"  # Stops the backfill process
    ]
    logger.info("Migration console commands that will be executed:")
    for cmd in commands:
        logger.info(f"  {cmd}")

@pytest.fixture(scope="class")
def setup_backfill(request):
    config_path = request.config.getoption("--config_file_path")
    unique_id = request.config.getoption("--unique_id")
    pytest.console_env = Context(config_path).env
    pytest.unique_id = unique_id
    
    # Print migration commands that will be executed
    print_migration_commands()
    
    # Load and configure the document multiplier transformer
    transformer_path = Path(__file__).parent.parent.parent.parent.parent.parent.parent / "test/resources/transformers/document_multiplier.js"
    with open(transformer_path) as f:
        transformer_config = [{
            "JsonJSTransformerProvider": {
                "initializationScript": f.read(),
                "bindingsObject": "{}"
            }
        }]
    
    # Configure the backfill with the transformer
    backfill: Backfill = pytest.console_env.backfill
    assert backfill is not None
    backfill.transformer_config = json.dumps(transformer_config)
    
    # Preload benchmark data and create target indices
    preload_data(source_cluster=pytest.console_env.source_cluster,
                 target_cluster=pytest.console_env.target_cluster)
    
    # Start the backfill process directly (skip metadata migration)
    backfill.create()
    snapshot_result: CommandResult = pytest.console_env.snapshot.create(wait=True)
    assert snapshot_result.success
    backfill_start_result: CommandResult = backfill.start()
    assert backfill_start_result.success
    backfill_scale_result: CommandResult = backfill.scale(units=10)
    assert backfill_scale_result.success

@pytest.fixture(scope="session", autouse=True)
def cleanup_after_tests():
    logger.info("Starting document multiplier tests...")
    yield
    logger.info("Stopping backfill...")
    backfill: Backfill = pytest.console_env.backfill
    backfill.stop()

@pytest.mark.usefixtures("setup_backfill")
class DocumentMultiplierTests(unittest.TestCase):
    def test_document_multiplication(self):
        source_cluster: Cluster = pytest.console_env.source_cluster
        target_cluster: Cluster = pytest.console_env.target_cluster

        # For each benchmark index
        for index_name, expected in EXPECTED_BENCHMARK_DOCS.items():
            logger.info(f"Checking multiplication for index: {index_name}")
            
            # Verify source has expected documents
            source_count = ops.get_doc_count(cluster=source_cluster, index_name=index_name)
            self.assertEqual(source_count, expected["count"], 
                           f"Source index {index_name} should have exactly {expected['count']} documents")

            # Wait and verify target has 6x documents (N=5 in transformer)
            expected_target_count = expected["count"] * 6  # Original + 5 copies
            target_count = ops.get_doc_count(
                cluster=target_cluster, 
                index_name=index_name, 
                max_attempts=30, 
                delay=30.0
            )
            self.assertEqual(target_count, expected_target_count, 
                           f"Target index {index_name} should have exactly {expected_target_count} documents")
