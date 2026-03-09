"""Pytest configuration for workflow tests."""

import logging
import os
import socket
import tempfile
import time
import uuid
import warnings
from pathlib import Path

import pytest
import requests
import subprocess
import re

from kubernetes import client, config, utils
from kubernetes.client.rest import ApiException
from testcontainers.k3s import K3SContainer

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def set_config_processor_dir(monkeypatch):
    # Set required ETCD environment variables for tests
    monkeypatch.setenv("ETCD_SERVICE_HOST", "localhost")
    monkeypatch.setenv("ETCD_SERVICE_PORT_CLIENT", "2379")


# ============================================================================
# Kubernetes / Argo Fixtures (shared across workflow test files)
# ============================================================================

def _detect_existing_kubernetes_cluster():
    """Detect if a Kubernetes cluster is already available and accessible."""
    try:
        config.load_kube_config()
        v1 = client.CoreV1Api()
        namespaces = v1.list_namespace(timeout_seconds=10)
        logger.info("✓ Detected existing Kubernetes cluster")
        logger.info(f"  Found {len(namespaces.items)} namespaces")
        contexts, active_context = config.list_kube_config_contexts()
        if active_context:
            cluster_name = active_context.get('context', {}).get('cluster', 'unknown')
            logger.info(f"  Active context: {active_context.get('name', 'unknown')}")
            logger.info(f"  Cluster: {cluster_name}")
        return True
    except config.ConfigException as e:
        logger.info(f"No kubeconfig found: {e}")
        return False
    except ApiException as e:
        logger.info(f"Kubernetes API error: {e}")
        return False
    except Exception as e:
        logger.info(f"Failed to connect to existing cluster: {e}")
        return False


def _get_kubernetes_client():
    """Get a configured Kubernetes client from an existing cluster."""
    try:
        config.load_kube_config()
        v1 = client.CoreV1Api()
        v1.list_namespace(timeout_seconds=10)
        return v1
    except Exception as e:
        logger.error(f"Failed to create Kubernetes client: {e}")
        return None


def _wait_for_port_forward(process, port, timeout=10):
    """Wait for a port-forward process to establish connection."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if process.poll() is not None:
            _, stderr = process.communicate()
            logger.warning(f"Port-forward failed to start: {stderr.decode()}")
            return False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            if sock.connect_ex(('localhost', port)) == 0:
                sock.close()
                return True
            sock.close()
        except Exception:
            pass
        time.sleep(0.5)
    return False


class _ArgoWorkflowsWaiter:
    """Handles waiting for Argo Workflows to be ready."""

    def __init__(self, namespace, timeout=300, check_interval=15):
        self.namespace = namespace
        self.timeout = timeout
        self.check_interval = check_interval
        self.v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self.deployments = ["argo-server", "workflow-controller"]

    def wait_for_ready(self, logger_obj):
        logger_obj.info(f"Waiting for Argo Workflows (timeout: {self.timeout}s)...")
        start_time = time.time()
        last_log_time = 0
        while time.time() - start_time < self.timeout:
            elapsed = time.time() - start_time
            all_ready = True
            for dep_name in self.deployments:
                try:
                    dep = self.apps_v1.read_namespaced_deployment(
                        name=dep_name, namespace=self.namespace)
                    if not (dep.status.ready_replicas or 0) >= 1:
                        all_ready = False
                except ApiException:
                    all_ready = False
            if all_ready:
                logger_obj.info(f"\n[{elapsed:.0f}s] ✓ Argo Workflows is ready!")
                return True
            if elapsed - last_log_time >= self.check_interval:
                logger_obj.info(f"\n[{elapsed:.0f}s] Still waiting for Argo deployments...")
                last_log_time = elapsed
            time.sleep(5)
        logger_obj.error("\n❌ Argo Workflows did not become ready in time")
        try:
            pods = self.v1.list_namespaced_pod(namespace=self.namespace)
            for pod in pods.items:
                logger_obj.error(f"  Pod {pod.metadata.name}: {pod.status.phase}")
                for cs in (pod.status.container_statuses or []):
                    if cs.state.waiting:
                        logger_obj.error(f"    {cs.name}: {cs.state.waiting.reason} - {cs.state.waiting.message or ''}")
            events = self.v1.list_namespaced_event(
                namespace=self.namespace, field_selector="type=Warning", limit=10)
            for ev in events.items:
                logger_obj.error(f"  Event: {ev.involved_object.name}: {ev.reason} - {ev.message}")
        except Exception:
            pass
        return False


@pytest.fixture(scope="session")
def k3s_container():
    """
    Set up Kubernetes cluster for all workflow tests.

    Supports three modes:
    1. GitHub Actions: Uses existing Kind cluster
    2. Local with existing cluster: Auto-detects minikube/k3s/kind
    3. Local without cluster: Falls back to creating k3s container
    """
    has_existing_cluster = _detect_existing_kubernetes_cluster()

    if has_existing_cluster:
        logger.info("\n=== Using existing Kubernetes cluster ===")
        k8s_client = _get_kubernetes_client()
        if k8s_client is None:
            pytest.fail("Detected existing cluster but failed to create client")
        yield {"mode": "existing-cluster", "container": None}
        logger.info("\nUsing existing cluster - no cleanup needed")
    else:
        logger.info("\n=== No existing cluster detected ===")
        logger.info("Starting k3s container for workflow tests...")
        container = K3SContainer(image="rancher/k3s:latest")
        container.start()
        kubeconfig = container.config_yaml()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(kubeconfig)
            kubeconfig_path = f.name
        os.environ['KUBECONFIG'] = kubeconfig_path
        config.load_kube_config(config_file=kubeconfig_path)
        yield {"mode": "k3s-container", "container": container}
        logger.info("\nCleaning up k3s container...")
        container.stop()
        if os.path.exists(kubeconfig_path):
            os.unlink(kubeconfig_path)
        if 'KUBECONFIG' in os.environ:
            del os.environ['KUBECONFIG']


@pytest.fixture(scope="session")
def argo_workflows(k3s_container):
    """Install Argo Workflows in the cluster and yield connection info."""
    argo_version = "v3.7.3"
    argo_namespace = "argo"
    v1 = client.CoreV1Api()

    namespace = client.V1Namespace(metadata=client.V1ObjectMeta(name=argo_namespace))
    try:
        v1.create_namespace(body=namespace)
    except ApiException as e:
        if e.status != 409:
            raise

    manifest_url = (
        f"https://github.com/argoproj/argo-workflows/releases/download/"
        f"{argo_version}/quick-start-minimal.yaml"
    )
    try:
        response = requests.get(manifest_url, timeout=30)
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(response.text)
            manifest_path = f.name
        k8s_client = client.ApiClient()
        try:
            utils.create_from_yaml(k8s_client, manifest_path, namespace=argo_namespace)
        except Exception as apply_error:
            logger.info(f"Note during apply: {apply_error}")
        if os.path.exists(manifest_path):
            os.unlink(manifest_path)
    except Exception as e:
        logger.info(f"Error installing Argo Workflows: {e}")
        raise

    waiter = _ArgoWorkflowsWaiter(argo_namespace, timeout=300, check_interval=15)
    if not waiter.wait_for_ready(logger):
        raise TimeoutError("Argo Workflows pods did not become ready in time")

    port_forward_process = None
    try:
        port_forward_cmd = [
            "kubectl", "port-forward",
            f"--namespace={argo_namespace}",
            "svc/argo-server", "2746:2746"
        ]
        port_forward_process = subprocess.Popen(
            port_forward_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if _wait_for_port_forward(port_forward_process, 2746):
            logger.info("✓ Port-forward to argo-server established on localhost:2746")
        else:
            logger.warning("Port-forward not ready - output command tests may fail")
    except Exception as e:
        logger.warning(f"Failed to set up port-forward: {e}")

    yield {
        "namespace": argo_namespace,
        "version": argo_version,
        "port_forward_process": port_forward_process,
    }

    if port_forward_process and port_forward_process.poll() is None:
        port_forward_process.terminate()
        try:
            port_forward_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            port_forward_process.kill()


@pytest.fixture(scope="session")
def test_namespace(k3s_container):
    """Create a unique namespace for the entire test session."""
    namespace_name = f"test-workflow-{uuid.uuid4().hex[:8]}"
    v1 = client.CoreV1Api()
    namespace = client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace_name))
    try:
        v1.create_namespace(body=namespace)
    except ApiException as e:
        if e.status != 409:
            raise
    yield namespace_name
    try:
        v1.delete_namespace(name=namespace_name)
    except ApiException:
        pass


def agradle(*args):
    """
    Mimics the shell function:
    - Walk upward from cwd to root looking for ./gradlew
    - Run that gradlew with the provided args
    - Return stdout (stripped)
    """
    dir_path = Path(os.getcwd())
    for parent in [dir_path] + list(dir_path.parents):
        gradlew = parent / "gradlew"
        if gradlew.exists() and os.access(gradlew, os.X_OK):
            result = subprocess.run(
                [str(gradlew), *args],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            return result.stdout.strip()
    raise RuntimeError("No gradlew script found in ancestor directories")


CONFIG_RE = re.compile(r"CONFIG_PROCESSOR_DIR=(.*)")
NODE_RE = re.compile(r"NODEJS_BIN=(.*)")


@pytest.fixture(scope="session", autouse=True)
def ensure_config_processor_dir():
    """
    Ensures CONFIG_PROCESSOR_DIR and NODEJS are set.
    If not set, runs the Gradle confirmConfigProcessorStagingPath task once
    and extracts the values from its output.
    """
    config_already_set = "CONFIG_PROCESSOR_DIR" in os.environ
    node_already_set = "NODEJS" in os.environ

    if config_already_set and node_already_set:
        return  # both already set externally

    print("CONFIG_PROCESSOR_DIR or NODEJS not set — running Gradle task...")

    output = agradle(
        ":migrationConsole:confirmConfigProcessorStagingPath",
        "-q",
        "--console=plain"
    )

    # Only set CONFIG_PROCESSOR_DIR if not already set
    if not config_already_set:
        config_match = CONFIG_RE.search(output)
        if not config_match:
            raise ValueError(f"Gradle did not output CONFIG_PROCESSOR_DIR=. Received:\n{output}")
        config_path = config_match.group(1).strip()
        os.environ["CONFIG_PROCESSOR_DIR"] = config_path
        print("CONFIG_PROCESSOR_DIR set to: {config_path}")

    # Only set NODEJS if not already set
    if not node_already_set:
        node_match = NODE_RE.search(output)
        if not node_match:
            raise ValueError(f"Gradle did not output NODEJS=. Received:\n{output}")
        node_path = node_match.group(1).strip()
        os.environ["NODEJS"] = node_path
        print(f"NODEJS set to: {node_path}")
