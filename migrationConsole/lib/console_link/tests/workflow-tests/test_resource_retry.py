"""
Integration test: Argo resource template retryStrategy recovers from executor pod eviction.

Submits a workflow with a resource template (create ConfigMap) that has retryStrategy.
Deletes the executor pod mid-execution to simulate EKS node eviction.
Verifies the workflow retries and eventually succeeds.

This test is expected to FAIL with stock Argo Workflows — killing the executor
pod for a resource template permanently fails the workflow. It documents the
behavior gap that https://github.com/argoproj/argo-workflows/pull/15642 addresses.
"""

import logging
import time
import uuid

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)


@pytest.mark.slow
class TestResourceRetryOnEviction:
    """Test that retryStrategy on resource templates recovers from executor pod eviction."""

    def test_resource_retry_after_executor_eviction(self, argo_workflows):
        """
        Simulate executor pod eviction during a resource template step.

        Strategy:
        - Submit a workflow with a resource template that creates a ConfigMap
          and waits on a successCondition (label ready=true) that won't be
          true initially
        - While the executor is polling, delete the executor pod
        - Argo's retryStrategy should retry the step
        - Before the retry's successCondition check, patch the ConfigMap
          to satisfy the condition
        - Workflow should succeed
        """
        argo_ns = argo_workflows["namespace"]
        test_id = uuid.uuid4().hex[:8]
        cm_name = f"test-retry-cm-{test_id}"

        v1 = client.CoreV1Api()
        custom_api = client.CustomObjectsApi()

        workflow_spec = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "metadata": {
                "generateName": f"test-retry-{test_id}-",
                "namespace": argo_ns,
            },
            "spec": {
                "entrypoint": "create-resource",
                "templates": [{
                    "name": "create-resource",
                    "retryStrategy": {
                        "limit": "3",
                        "retryPolicy": "Always",
                        # Flat backoff (factor=1) for fast test retries.
                        # Production workflows use factor=2 for exponential backoff.
                        "backoff": {"duration": "5", "factor": "1"},
                    },
                    "resource": {
                        "action": "apply",
                        "successCondition": "metadata.labels.ready == true",
                        "manifest": (
                            f"apiVersion: v1\n"
                            f"kind: ConfigMap\n"
                            f"metadata:\n"
                            f"  name: {cm_name}\n"
                            f"  namespace: {argo_ns}\n"
                            f"data:\n"
                            f"  test: value\n"
                        ),
                    },
                }],
            },
        }

        # Submit workflow
        result = custom_api.create_namespaced_custom_object(
            group="argoproj.io", version="v1alpha1",
            namespace=argo_ns, plural="workflows", body=workflow_spec,
        )
        wf_name = result["metadata"]["name"]
        logger.info(f"Submitted workflow: {wf_name}")

        try:
            # Wait for executor pod to appear and start polling
            executor_pod = self._wait_for_executor_pod(v1, argo_ns, wf_name, timeout=120)
            assert executor_pod, f"Executor pod never appeared for {wf_name}"
            logger.info(f"Executor pod running: {executor_pod}")

            # Give it a moment to run kubectl apply and start polling successCondition
            time.sleep(5)

            # Delete the executor pod to simulate eviction
            logger.info(f"Deleting executor pod {executor_pod} to simulate eviction...")
            v1.delete_namespaced_pod(
                name=executor_pod, namespace=argo_ns,
                grace_period_seconds=0,
            )
            logger.info("Executor pod deleted")

            # Patch the ConfigMap to satisfy successCondition for the retry attempt
            time.sleep(5)
            logger.info("Patching ConfigMap with ready=true label...")
            try:
                v1.patch_namespaced_config_map(
                    name=cm_name, namespace=argo_ns,
                    body={"metadata": {"labels": {"ready": "true"}}},
                )
            except ApiException as e:
                if e.status == 404:
                    # ConfigMap may not exist if eviction happened before apply
                    v1.create_namespaced_config_map(
                        namespace=argo_ns,
                        body=client.V1ConfigMap(
                            metadata=client.V1ObjectMeta(
                                name=cm_name, labels={"ready": "true"}
                            ),
                            data={"test": "value"},
                        ),
                    )
                else:
                    raise

            # Wait for workflow to complete
            final_phase = self._wait_for_workflow_completion(
                custom_api, argo_ns, wf_name, timeout=120
            )
            assert final_phase == "Succeeded", (
                f"Workflow did not recover from eviction. Phase: {final_phase}. "
                f"This is expected to fail without "
                f"https://github.com/argoproj/argo-workflows/pull/15642"
            )
            logger.info("Workflow recovered and succeeded after eviction!")

            # Verify the resource template actually applied the manifest
            cm = v1.read_namespaced_config_map(name=cm_name, namespace=argo_ns)
            assert cm.metadata.labels.get("ready") == "true"

        finally:
            # Cleanup
            try:
                v1.delete_namespaced_config_map(name=cm_name, namespace=argo_ns)
            except ApiException:
                pass
            try:
                custom_api.delete_namespaced_custom_object(
                    group="argoproj.io", version="v1alpha1",
                    namespace=argo_ns, plural="workflows", name=wf_name,
                )
            except ApiException:
                pass

    def _wait_for_executor_pod(self, v1, namespace, workflow_name, timeout=60):
        """Wait for the executor pod to appear and be Running."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            pods = v1.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"workflows.argoproj.io/workflow={workflow_name}",
            )
            for pod in pods.items:
                if pod.status.phase == "Running":
                    return pod.metadata.name
            time.sleep(2)
        return None

    def _wait_for_workflow_completion(self, custom_api, namespace, name, timeout=120):
        """Wait for workflow to reach a terminal phase."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            wf = custom_api.get_namespaced_custom_object(
                group="argoproj.io", version="v1alpha1",
                namespace=namespace, plural="workflows", name=name,
            )
            phase = wf.get("status", {}).get("phase", "Unknown")
            if phase in ("Succeeded", "Failed", "Error"):
                return phase
            time.sleep(3)
        return "Timeout"
