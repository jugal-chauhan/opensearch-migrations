"""
Integration tests for workflow CLI commands using Kubernetes clusters.

These tests support three execution modes:

1. **GitHub Actions (CI)**: Uses a pre-configured Kind cluster set up by the workflow.
   The cluster is created before tests run and is automatically detected.

2. **Local with existing cluster**: Auto-detects and uses any accessible Kubernetes
   cluster (minikube, k3s, kind, etc.) configured in your kubeconfig.

3. **Local without cluster**: Falls back to creating a k3s container using
   testcontainers-python for a lightweight, isolated test environment.

The test suite automatically detects which mode to use based on cluster availability.
No manual configuration is required - the tests adapt to the environment.

## Running Tests Locally

### With existing cluster (fastest):
```bash
# Start minikube, kind, or k3s first
minikube start  # or: kind create cluster
pytest tests/workflow-tests/test_workflow_integration.py
```

### Without existing cluster (automatic fallback):
```bash
# Tests will automatically create k3s container
pytest tests/workflow-tests/test_workflow_integration.py
```

## Architecture

The cluster detection logic and shared fixtures (k3s_container, argo_workflows,
test_namespace) are in conftest.py. Tests in this file use those fixtures directly.
"""

import logging
import os
import pytest
import subprocess
import time
import uuid
from click.testing import CliRunner
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from console_link.workflow.cli import workflow_cli
from console_link.workflow.models.config import WorkflowConfig
from console_link.workflow.models.workflow_config_store import WorkflowConfigStore
from testcontainers.core.container import DockerContainer

logger = logging.getLogger(__name__)


# ============================================================================
# Test Fixtures (test-file-specific)
# ============================================================================

# k3s_container, argo_workflows, and test_namespace fixtures are in conftest.py


@pytest.fixture
def k8s_workflow_store(test_namespace):
    """Create a WorkflowConfigStore connected to k3s Kubernetes in the test namespace"""
    try:
        # Create a Kubernetes client using the already loaded configuration
        # The kubeconfig from k3s already has admin permissions
        v1 = client.CoreV1Api()

        # Verify the connection is working before creating the store
        try:
            v1.list_namespace()
        except Exception as e:
            pytest.skip(f"Kubernetes connection lost: {e}")

        # Create the WorkflowConfigStore with the pre-configured client for the test namespace
        store = WorkflowConfigStore(
            namespace=test_namespace,
            config_map_prefix="workflow-test",
            k8s_client=v1
        )

        # Clean up any existing test ConfigMaps in this namespace
        try:
            config_maps = v1.list_namespaced_config_map(
                namespace=test_namespace,
                label_selector="app=migration-assistant,component=workflow-config"
            )
            for cm in config_maps.items:
                if cm.metadata.name.startswith("workflow-test"):
                    v1.delete_namespaced_config_map(name=cm.metadata.name, namespace=test_namespace)
        except ApiException:
            pass

        yield store
    except Exception as e:
        pytest.skip(f"Failed to create WorkflowConfigStore: {e}")


@pytest.fixture
def runner():
    """CLI runner for testing"""
    return CliRunner()


@pytest.fixture
def sample_workflow_config():
    """Sample workflow configuration for testing"""
    data = {
        "targets": {
            "production": {
                "endpoint": "https://target-os.example.com:9200",
                "auth": {
                    "username": "admin",
                    "password": "test-password"
                },
                "allow_insecure": False
            }
        },
        "source-migration-configurations": [
            {
                "source": {
                    "endpoint": "https://source-es.example.com:9200",
                    "auth": {
                        "username": "admin",
                        "password": "test-password"
                    },
                    "allow_insecure": True
                }
            }
        ]
    }

    return WorkflowConfig(data)


@pytest.mark.slow
class TestWorkflowCLICommands:
    """Integration tests for workflow CLI commands using real k3s"""

    def test_workflow_help(self, runner):
        """Test workflow help command"""
        result = runner.invoke(workflow_cli, ['--help'])
        assert result.exit_code == 0
        assert "Workflow-based migration management CLI" in result.output
        assert "configure" in result.output
        assert "util" in result.output

    def test_workflow_configure_view_no_config(self, runner, k8s_workflow_store):
        """Test workflow configure view when no config exists"""
        session_name = "default"

        # Clean up any existing test data
        try:
            k8s_workflow_store.delete_config(session_name)
        except ApiException:
            pass  # Ignore if doesn't exist

        result = runner.invoke(workflow_cli, ['configure', 'view'], obj={
                               'store': k8s_workflow_store, 'namespace': k8s_workflow_store.namespace})

        assert result.exit_code == 0
        assert "No configuration found" in result.output

    def test_workflow_configure_view_existing_config(self, runner, k8s_workflow_store, sample_workflow_config):
        """Test workflow configure view with existing config"""
        session_name = "default"

        # Clean up any existing test data
        try:
            k8s_workflow_store.delete_config(session_name)
        except ApiException:
            pass  # Ignore if doesn't exist

        # Create a test config
        data = {
            "targets": {
                "test": {
                    "endpoint": "https://test.com:9200",
                    "auth": {
                        "username": "admin",
                        "password": "password"
                    }
                }
            }
        }
        config = WorkflowConfig(data)

        # Save config to k3s
        message = k8s_workflow_store.save_config(config, session_name)
        assert "created" in message or "updated" in message

        result = runner.invoke(workflow_cli, ['configure', 'view'], obj={
                               'store': k8s_workflow_store, 'namespace': k8s_workflow_store.namespace})

        assert result.exit_code == 0
        assert "targets:" in result.output
        assert "test:" in result.output
        # Parse YAML output to verify structure
        import yaml as yaml_parser
        config_data = yaml_parser.safe_load(result.output)
        endpoint = config_data['targets']['test']['endpoint']
        assert endpoint == "https://test.com:9200"

        # Cleanup
        try:
            k8s_workflow_store.delete_config(session_name)
        except ApiException:
            pass

    def test_workflow_configure_clear_with_confirmation(self, runner, k8s_workflow_store, sample_workflow_config):
        """Test workflow configure clear with confirmation"""
        session_name = "default"

        # Create a config to clear
        message = k8s_workflow_store.save_config(sample_workflow_config, session_name)
        assert "created" in message or "updated" in message

        result = runner.invoke(workflow_cli, ['configure', 'clear', '--confirm'],
                               obj={'store': k8s_workflow_store, 'namespace': k8s_workflow_store.namespace})

        assert result.exit_code == 0
        assert f"Cleared workflow configuration for session: {session_name}" in result.output

        # Verify config was cleared (should be empty)
        config = k8s_workflow_store.load_config(session_name)
        # Config should exist but be empty
        assert config is not None
        assert config.data == {}

    def test_workflow_configure_edit_with_stdin_json(self, runner, k8s_workflow_store):
        """Test workflow configure edit with JSON input from stdin"""
        session_name = "default"

        # Clean up any existing test data
        try:
            k8s_workflow_store.delete_config(session_name)
        except ApiException:
            pass  # Ignore if doesn't exist

        # Prepare JSON input
        json_input = '{"targets": {"test": {"endpoint": "https://test.com:9200"}}}'

        result = runner.invoke(workflow_cli, ['configure', 'edit', '--stdin'], input=json_input,
                               obj={'store': k8s_workflow_store, 'namespace': k8s_workflow_store.namespace})

        assert result.exit_code == 0
        assert "Configuration" in result.output

        # Verify config was saved
        config = k8s_workflow_store.load_config(session_name)
        assert config is not None
        assert config.get("targets")["test"]["endpoint"] == "https://test.com:9200"

        # Cleanup
        try:
            k8s_workflow_store.delete_config(session_name)
        except ApiException:
            pass

    def test_workflow_configure_edit_with_stdin_yaml(self, runner, k8s_workflow_store):
        """Test workflow configure edit with YAML input from stdin"""
        session_name = "default"

        # Clean up any existing test data
        try:
            k8s_workflow_store.delete_config(session_name)
        except ApiException:
            pass  # Ignore if doesn't exist

        # Prepare YAML input
        yaml_input = """targets:
  test:
    endpoint: https://test.com:9200
    auth:
      username: admin
      password: password
"""

        result = runner.invoke(workflow_cli, ['configure', 'edit', '--stdin'], input=yaml_input,
                               obj={'store': k8s_workflow_store, 'namespace': k8s_workflow_store.namespace})

        assert result.exit_code == 0
        assert "Configuration" in result.output

        # Verify config was saved
        config = k8s_workflow_store.load_config(session_name)
        assert config is not None
        assert config.get("targets")["test"]["endpoint"] == "https://test.com:9200"
        assert config.get("targets")["test"]["auth"]["username"] == "admin"

        # Cleanup
        try:
            k8s_workflow_store.delete_config(session_name)
        except ApiException:
            pass

    def test_workflow_configure_edit_with_stdin_empty(self, runner, k8s_workflow_store):
        """Test workflow configure edit with empty stdin input"""
        result = runner.invoke(workflow_cli, ['configure', 'edit', '--stdin'], input='',
                               obj={'store': k8s_workflow_store, 'namespace': k8s_workflow_store.namespace})

        assert result.exit_code != 0
        assert "Configuration was empty, a value is required" in result.output

    def test_workflow_configure_edit_with_stdin_invalid(self, runner, k8s_workflow_store):
        """Test workflow configure edit with invalid stdin input"""
        invalid_input = "this is not valid JSON or YAML: {{{["

        result = runner.invoke(workflow_cli, ['configure', 'edit', '--stdin'], input=invalid_input,
                               obj={'store': k8s_workflow_store, 'namespace': k8s_workflow_store.namespace})

        assert result.exit_code != 0
        assert "Failed to parse input" in result.output

    def test_workflow_util_completions_bash(self, runner):
        """Test workflow util completions for bash"""
        result = runner.invoke(workflow_cli, ['util', 'completions', 'bash'])
        assert result.exit_code == 0
        assert "_workflow_completion" in result.output.lower() or "complete" in result.output.lower()

    def test_workflow_util_completions_zsh(self, runner):
        """Test workflow util completions for zsh"""
        result = runner.invoke(workflow_cli, ['util', 'completions', 'zsh'])
        assert result.exit_code == 0
        assert "compdef" in result.output or "#compdef" in result.output

    def test_workflow_util_completions_fish(self, runner):
        """Test workflow util completions for fish"""
        result = runner.invoke(workflow_cli, ['util', 'completions', 'fish'])
        assert result.exit_code == 0
        assert "complete" in result.output


@pytest.mark.slow
class TestArgoWorkflows:
    """Integration tests for Argo Workflows installation in k3s"""

    def test_argo_workflows_installation(self, argo_workflows):
        """Test that Argo Workflows is properly installed in k3s"""
        argo_namespace = argo_workflows["namespace"]
        argo_version = argo_workflows["version"]

        logger.info(f"\nVerifying Argo Workflows {argo_version} installation in namespace {argo_namespace}")

        v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()

        # Verify argo namespace exists
        namespaces = v1.list_namespace()
        namespace_names = [ns.metadata.name for ns in namespaces.items]
        assert argo_namespace in namespace_names, f"Argo namespace {argo_namespace} not found"
        logger.info(f"✓ Namespace {argo_namespace} exists")

        # Verify argo-server deployment exists and is ready
        server_deployment = apps_v1.read_namespaced_deployment(
            name="argo-server",
            namespace=argo_namespace
        )
        assert server_deployment is not None, "argo-server deployment not found"
        assert server_deployment.status.ready_replicas >= 1, "argo-server deployment not ready"
        logger.info(f"✓ argo-server deployment is ready ({server_deployment.status.ready_replicas} replicas)")

        # Verify workflow-controller deployment exists and is ready
        controller_deployment = apps_v1.read_namespaced_deployment(
            name="workflow-controller",
            namespace=argo_namespace
        )
        assert controller_deployment is not None, "workflow-controller deployment not found"
        assert controller_deployment.status.ready_replicas >= 1, "workflow-controller deployment not ready"
        logger.info(
            "✓ workflow-controller deployment is ready (%s replicas)",
            controller_deployment.status.ready_replicas
        )

        # Verify argo-server service exists
        services = v1.list_namespaced_service(namespace=argo_namespace)
        service_names = [svc.metadata.name for svc in services.items]
        assert "argo-server" in service_names, "argo-server service not found"
        logger.info("✓ argo-server service exists")

        # Verify pods are running
        pods = v1.list_namespaced_pod(namespace=argo_namespace)
        running_pods = [pod for pod in pods.items if pod.status.phase == "Running"]
        assert len(running_pods) >= 2, f"Expected at least 2 running pods, found {len(running_pods)}"
        logger.info(f"✓ Found {len(running_pods)} running pods in {argo_namespace} namespace")

        for pod in running_pods:
            logger.info(f"  - {pod.metadata.name}: {pod.status.phase}")

        logger.info(f"\n✓ Argo Workflows {argo_version} is successfully installed and running!")

    def test_workflow_submit_hello_world(self, argo_workflows):
        """Test submitting a hello-world workflow to Argo via Kubernetes API with output verification"""
        argo_namespace = argo_workflows["namespace"]

        logger.info(f"\nTesting workflow submission to Argo in namespace {argo_namespace}")

        # Create unique message for this test
        test_message = f"hello world from test {uuid.uuid4().hex[:8]}"

        # Create workflow specification as a Kubernetes custom resource with output parameter
        workflow_spec = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "metadata": {
                "generateName": "test-hello-world-",
                "namespace": argo_namespace,
                "labels": {
                    "workflows.argoproj.io/completed": "false"
                }
            },
            "spec": {
                # Use default service account which has executor role bound in quickstart
                # The executor role grants permission to create workflowtaskresults
                "podMetadata": {
                    "labels": {
                        "test-workflow": "hello-world"
                    }
                },
                "templates": [
                    {
                        "name": "hello-world",
                        "outputs": {
                            "parameters": [
                                {
                                    "name": "message",
                                    "valueFrom": {
                                        "path": "/tmp/message.txt"
                                    }
                                }
                            ]
                        },
                        "container": {
                            "image": "busybox",
                            "command": ["sh", "-c"],
                            "args": [f'echo "{test_message}" | tee /tmp/message.txt']
                        }
                    }
                ],
                "entrypoint": "hello-world"
            }
        }

        # Submit workflow using Kubernetes API
        custom_api = client.CustomObjectsApi()

        try:
            logger.info("Submitting workflow via Kubernetes API...")

            # Create the workflow custom resource
            result = custom_api.create_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                namespace=argo_namespace,
                plural="workflows",
                body=workflow_spec
            )

            workflow_name = result.get("metadata", {}).get("name")
            workflow_uid = result.get("metadata", {}).get("uid")

            assert workflow_name is not None, "Workflow name not returned"
            assert workflow_name.startswith("test-hello-world-"), f"Unexpected workflow name: {workflow_name}"
            assert workflow_uid is not None, "Workflow UID not returned"

            logger.info("✓ Workflow submitted successfully!")
            logger.info(f"  Name: {workflow_name}")
            logger.info(f"  UID: {workflow_uid}")

            # Wait for workflow to complete
            logger.info("Waiting for workflow to complete...")
            max_wait = 60  # 60 seconds timeout
            start_time = time.time()
            workflow_phase = "Unknown"

            while time.time() - start_time < max_wait:
                workflow = custom_api.get_namespaced_custom_object(
                    group="argoproj.io",
                    version="v1alpha1",
                    namespace=argo_namespace,
                    plural="workflows",
                    name=workflow_name
                )

                workflow_phase = workflow.get("status", {}).get("phase", "Unknown")
                logger.info(f"  Workflow phase: {workflow_phase}")

                # Check if workflow reached a terminal state
                if workflow_phase in ["Succeeded", "Failed", "Error"]:
                    break

                time.sleep(2)

            assert workflow is not None, "Workflow not found in Kubernetes"
            assert workflow["metadata"]["name"] == workflow_name
            logger.info("✓ Workflow verified in Kubernetes")

            # If workflow failed or errored, get detailed information
            if workflow_phase in ["Failed", "Error"]:
                logger.error(f"Workflow ended in {workflow_phase} phase")

                # Get workflow status message
                status_message = workflow.get("status", {}).get("message", "No message available")
                logger.error(f"Status message: {status_message}")

                # Get node details to understand what failed
                nodes = workflow.get("status", {}).get("nodes", {})
                logger.error(f"Workflow has {len(nodes)} nodes")

                for node_id, node in nodes.items():
                    node_name = node.get("displayName", node.get("name", "unknown"))
                    node_phase = node.get("phase", "unknown")
                    node_message = node.get("message", "")
                    node_type = node.get("type", "unknown")

                    logger.error(f"\nNode: {node_name}")
                    logger.error(f"  ID: {node_id}")
                    logger.error(f"  Type: {node_type}")
                    logger.error(f"  Phase: {node_phase}")
                    if node_message:
                        logger.error(f"  Message: {node_message}")

                    # Try to get pod logs if this is a pod node
                    if node_type == "Pod":
                        try:
                            v1 = client.CoreV1Api()
                            pod_name = node.get("id", node_id)
                            logger.error(f"  Attempting to get logs for pod: {pod_name}")

                            # Get pod logs
                            logs = v1.read_namespaced_pod_log(
                                name=pod_name,
                                namespace=argo_namespace,
                                tail_lines=50
                            )
                            logger.error(f"  Pod logs:\n{logs}")
                        except Exception as log_error:
                            logger.error(f"  Could not retrieve pod logs: {log_error}")

            # Verify workflow succeeded
            assert workflow_phase == "Succeeded", f"Workflow did not succeed, phase: {workflow_phase}"
            logger.info(f"✓ Workflow completed successfully with phase: {workflow_phase}")

            # Extract and verify output parameter
            output_message = None
            nodes = workflow.get("status", {}).get("nodes", {})

            for node_id, node in nodes.items():
                outputs = node.get("outputs", {})
                parameters = outputs.get("parameters", [])

                for param in parameters:
                    if param.get("name") == "message":
                        output_message = param.get("value", "").strip()
                        break

                if output_message:
                    break

            assert output_message is not None, "Could not retrieve workflow output"
            assert test_message in output_message, \
                f"Output doesn't match expected message. Expected: '{test_message}', Got: '{output_message}'"

            logger.info(f"✓ Container output verified: {output_message}")
            logger.info("✓ Output verification successful - container executed correctly!")

            # Test output command with the completed workflow
            logger.info("\nTesting output command for completed workflow...")
            runner = CliRunner()
            _test_output_command_for_workflow(runner, workflow_name, argo_namespace, test_message)

        except ApiException as e:
            pytest.fail(f"Failed to submit workflow via Kubernetes API: {e}")

    def test_workflow_status_after_submit(self, argo_workflows):
        """Test workflow status command after submitting a workflow"""
        argo_namespace = argo_workflows["namespace"]

        logger.info(f"\nTesting workflow status command in namespace {argo_namespace}")

        # Create unique message for this test
        test_message = f"hello world from status test {uuid.uuid4().hex[:8]}"

        # Create workflow specification (same as test_workflow_submit_hello_world)
        workflow_spec = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "metadata": {
                "generateName": "test-status-",
                "namespace": argo_namespace,
                "labels": {
                    "workflows.argoproj.io/completed": "false"
                }
            },
            "spec": {
                "templates": [
                    {
                        "name": "hello-world",
                        "outputs": {
                            "parameters": [
                                {
                                    "name": "message",
                                    "valueFrom": {
                                        "path": "/tmp/message.txt"
                                    }
                                }
                            ]
                        },
                        "container": {
                            "image": "busybox",
                            "command": ["sh", "-c"],
                            "args": [f'echo "{test_message}" | tee /tmp/message.txt']
                        }
                    }
                ],
                "entrypoint": "hello-world"
            }
        }

        # Submit workflow using Kubernetes API
        custom_api = client.CustomObjectsApi()

        try:
            logger.info("Submitting workflow for status test...")

            # Create the workflow custom resource
            result = custom_api.create_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                namespace=argo_namespace,
                plural="workflows",
                body=workflow_spec
            )

            workflow_name = result.get("metadata", {}).get("name")
            assert workflow_name is not None, "Workflow name not returned"
            assert workflow_name.startswith("test-status-"), f"Unexpected workflow name: {workflow_name}"

            logger.info(f"✓ Workflow submitted: {workflow_name}")

            # Wait for workflow to complete
            logger.info("Waiting for workflow to complete...")
            max_wait = 60  # 60 seconds timeout
            start_time = time.time()
            workflow_phase = "Unknown"

            while time.time() - start_time < max_wait:
                workflow = custom_api.get_namespaced_custom_object(
                    group="argoproj.io",
                    version="v1alpha1",
                    namespace=argo_namespace,
                    plural="workflows",
                    name=workflow_name
                )

                workflow_phase = workflow.get("status", {}).get("phase", "Unknown")
                logger.info(f"  Workflow phase: {workflow_phase}")

                # Check if workflow reached a terminal state
                if workflow_phase in ["Succeeded", "Failed", "Error"]:
                    break

                time.sleep(2)

            # Verify workflow succeeded
            assert workflow_phase == "Succeeded", f"Workflow did not succeed, phase: {workflow_phase}"
            logger.info(f"✓ Workflow completed with phase: {workflow_phase}")

            # Test status command with the completed workflow
            logger.info("\nTesting status command for completed workflow...")
            runner = CliRunner()
            _test_status_command_for_workflow(runner, workflow_name, argo_namespace)

            logger.info("✓ Status command test completed successfully!")

        except ApiException as e:
            pytest.fail(f"Failed to submit workflow via Kubernetes API: {e}")


def test_k3s_container_support():
    """Test that k3s container support is available"""
    try:
        # Just verify the import works
        assert DockerContainer is not None
    except ImportError:
        pytest.skip("testcontainers not installed - run: pip install testcontainers")


def _test_output_command_for_workflow(runner, workflow_name, namespace, expected_message):
    """
    Helper function to test output command for a given workflow.

    Tests that the output command can retrieve output from a completed workflow.

    Args:
        runner: Click test runner
        workflow_name: Name of the workflow to get output for
        namespace: Kubernetes namespace
        expected_message: The message that should appear in the workflow output

    Raises:
        AssertionError: If output command fails or output is not retrieved
    """
    result = runner.invoke(
        workflow_cli,
        ['output', '--workflow-name', workflow_name, '--namespace', namespace,
         '--argo-server', 'https://localhost:2746', '--insecure',
         '--prefix', '', '-l', 'test-workflow=hello-world'],
    )

    assert result.exit_code == 0, f"Output command failed with exit code {result.exit_code}. Output: {result.output}"

    assert expected_message in result.output, \
        f"Expected message '{expected_message}' not found in output: {result.output}"

    logger.info(f"✓ Output command successfully executed for workflow {workflow_name}")
    logger.info(f"✓ Verified output contains expected message: {expected_message}")


def _test_status_command_for_workflow(runner, workflow_name, namespace):
    """
    Helper function to test status command for a given workflow.

    Tests that the status command can retrieve and display workflow status information.
    Verifies that status output contains workflow name, phase, and step information.

    Args:
        runner: Click test runner
        workflow_name: Name of the workflow to get status for
        namespace: Kubernetes namespace

    Raises:
        AssertionError: If status command fails or status information is not retrieved
    """
    # Invoke status command with explicit argo-server URL (HTTPS with insecure flag for self-signed cert)
    result = runner.invoke(
        workflow_cli,
        ['status', '--workflow-name', workflow_name, '--namespace', namespace,
         '--argo-server', 'https://localhost:2746', '--insecure']
    )

    # Verify command succeeded
    assert result.exit_code == 0, \
        f"Status command failed with exit code {result.exit_code}. Output: {result.output}"

    # Verify workflow name is in output
    assert workflow_name in result.output, \
        f"Workflow name '{workflow_name}' not found in output: {result.output}"

    # Verify phase information is present
    assert "Phase:" in result.output, \
        f"Phase information not found in output: {result.output}"

    # Verify step information is present (either tree format or flat format)
    assert "Workflow Steps" in result.output or "Steps:" in result.output, \
        f"Step information not found in output: {result.output}"

    # Verify workflow completed successfully
    assert "Succeeded" in result.output, \
        f"Workflow did not succeed. Output: {result.output}"

    logger.info(f"✓ Status command successfully retrieved status for workflow {workflow_name}")
