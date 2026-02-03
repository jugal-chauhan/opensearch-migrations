import {BaseExpression, expr, Serialized} from "@opensearch-migrations/argo-workflow-builders";
import {RFS_OPTIONS} from "@opensearch-migrations/schemas";
import {z} from "zod";

/**
 * Determines whether a separate coordinator cluster should be deployed for RFS.
 * 
 * @param documentBackfillConfig - Serialized RFS configuration
 * @returns true if Target Cluster is used for work coordination (no new cluster)
 * 
 * @example
 * // Use in workflow step conditions:
 * { when: { templateExp: shouldDeployCoordinatorCluster(b.inputs.documentBackfillConfig) } }
 */
export function shouldDeployCoordinatorCluster(
    documentBackfillConfig: BaseExpression<Serialized<z.infer<typeof RFS_OPTIONS>>>
): BaseExpression<boolean> {
    return expr.not(
        expr.dig(
            expr.deserializeRecord(documentBackfillConfig),
            ["useTargetClusterForWorkCoordination"],
            true  // Default: use target cluster
        )
    );
}
