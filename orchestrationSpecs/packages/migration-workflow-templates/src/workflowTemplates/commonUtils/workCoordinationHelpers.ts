import {BaseExpression, expr, Serialized} from "@opensearch-migrations/argo-workflow-builders";
import {RFS_OPTIONS} from "@opensearch-migrations/schemas";
import {z} from "zod";

/**
 * Determines whether a separate coordinator cluster should be deployed for RFS.
 * 
 * @param documentBackfillConfig - Serialized RFS configuration
 * @returns true if a new coordinator cluster should be created (when NOT using target cluster)
 * 
 * @example
 * // Use in workflow step conditions:
 * { when: { templateExp: shouldCreateRFSWorkCoordinationCluster(b.inputs.documentBackfillConfig) } }
 */
export function shouldCreateRFSWorkCoordinationCluster(
    documentBackfillConfig: BaseExpression<Serialized<z.infer<typeof RFS_OPTIONS>>>
): BaseExpression<boolean, "complicatedExpression"> {
    return expr.not(
        expr.dig(
            expr.deserializeRecord(documentBackfillConfig),
            ["useTargetClusterForWorkCoordination"],
            true  // Default: use target cluster
        )
    ) as BaseExpression<boolean, "complicatedExpression">;
}
