"""
Integration test: Argo retryStrategy recovers from executor pod eviction using a Job resource.

Unlike test_resource_retry.py (which uses successCondition polling and is expected to FAIL
with stock Argo), this test uses a Job that completes on its own. When the executor pod is
killed, retryStrategy re-runs the step with a new executor that re-applies the Job (idempotent
via kubectl apply). The Job completes → successCondition satisfied → workflow Succeeded.

Expected: PASS with stock Argo + retryStrategy. Faster with PR #15642 (no retry needed).
"""

import logging
import time
import uuid

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)


@pytest.mark.slow
class TestResourceRetryResilience:
    """Test retryStrategy recovery from executor pod eviction with a Job resource."""

    def test_executor_eviction_recovery_with_retry(self, argo_workflows):
        """Kill executor pod mid-Job, verify retryStrategy recovers the workflow."""
        argo_ns = argo_workflows["namespace"]
        test_id = uuid.uuid4().hex[:8]
        job_name = f"test-resilience-job-{test_id}"

        v1 = client.CoreV1Api()
        batch_v1 = client.BatchV1Api()
        custom_api = client.CustomObjectsApi()

        workflow_spec = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "metadata": {
                "name": f"test-resilience-{test_id}",
                "namespace": argo_ns,
            },
            "spec": {
                "entrypoint": "create-job",
                "templates": [{
                    "name": "create-job",
                    "retryStrategy": {
                        "limit": "3",
                        "retryPolicy": "Always",
                        "backoff": {"duration": "5", "factor": "2"},
                    },
                    "resource": {
                        "action": "apply",
                        "successCondition": "status.succeeded > 0",
                        "manifest": (
                            f"apiVersion: batch/v1\n"
                            f"kind: Job\n"
                            f"metadata:\n"
                            f"  name: {job_name}\n"
                            f"  namespace: {argo_ns}\n"
                            f"spec:\n"
                            f"  backoffLimit: 0\n"
                            f"  template:\n"
                            f"    spec:\n"
                            f"      restartPolicy: Never\n"
                            f"      containers:\n"
                            f"      - name: work\n"
                            f"        image: busybox\n"
                            f"        command: [\"sh\", \"-c\", \"echo done && sleep 15\"]\n"
                        ),
                    },
                }],
            },
        }

        wf_name = workflow_spec["metadata"]["name"]
        result = custom_api.create_namespaced_custom_object(
            group="argoproj.io", version="v1alpha1",
            namespace=argo_ns, plural="workflows", body=workflow_spec,
        )
        logger.info(f"Submitted workflow: {wf_name}")

        try:
            executor_pod = self._wait_for_executor_pod(v1, argo_ns, wf_name, timeout=120)
            assert executor_pod, f"Executor pod never appeared for {wf_name}"
            logger.info(f"Executor pod running: {executor_pod}")

            # Wait for Job to be created by the executor
            time.sleep(5)

            # Kill the executor pod
            logger.info(f"Deleting executor pod {executor_pod} to simulate eviction...")
            v1.delete_namespaced_pod(
                name=executor_pod, namespace=argo_ns,
                grace_period_seconds=0,
            )
            logger.info("Executor pod deleted")

            # Wait for workflow terminal state (retryStrategy should kick in)
            final_phase = self._wait_for_workflow_completion(
                custom_api, argo_ns, wf_name, timeout=180
            )
            logger.info(f"Workflow final phase: {final_phase}")

            assert final_phase == "Succeeded", (
                f"Workflow did not recover. Phase: {final_phase}. "
                f"retryStrategy should have re-run the step with a new executor."
            )

            # Verify the Job actually completed
            job = batch_v1.read_namespaced_job(name=job_name, namespace=argo_ns)
            assert (job.status.succeeded or 0) > 0, "Job did not complete successfully"

        finally:
            for cleanup in [
                lambda: batch_v1.delete_namespaced_job(
                    name=job_name, namespace=argo_ns,
                    propagation_policy="Background"),
                lambda: custom_api.delete_namespaced_custom_object(
                    group="argoproj.io", version="v1alpha1",
                    namespace=argo_ns, plural="workflows", name=wf_name),
            ]:
                try:
                    cleanup()
                except ApiException:
                    pass

    def _wait_for_executor_pod(self, v1, namespace, workflow_name, timeout=120):
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

    def _wait_for_workflow_completion(self, custom_api, namespace, name, timeout=180):
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
