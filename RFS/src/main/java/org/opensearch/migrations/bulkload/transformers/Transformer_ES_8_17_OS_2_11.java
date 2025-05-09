package org.opensearch.migrations.bulkload.transformers;

import java.util.List;

import org.opensearch.migrations.bulkload.models.GlobalMetadata;
import org.opensearch.migrations.bulkload.models.IndexMetadata;
import org.opensearch.migrations.bulkload.version_os_2_11.GlobalMetadataData_OS_2_11;

import com.fasterxml.jackson.databind.node.ObjectNode;
import lombok.extern.slf4j.Slf4j;
@Slf4j
public class Transformer_ES_8_17_OS_2_11 implements Transformer {
    private final Transformer_ES_7_10_OS_2_11 delegateTransformer;
    // Constructor that initializes the delegate transformer with awareness attributes
    public Transformer_ES_8_17_OS_2_11(int awarenessAttributes) {
        this.delegateTransformer = new Transformer_ES_7_10_OS_2_11(awarenessAttributes);
    }

    @Override
    public GlobalMetadata transformGlobalMetadata(GlobalMetadata metaData) {
        log.atInfo().setMessage("Delegating transformGlobalMetadata to Transformer_ES_7_10_OS_2_11").log();
        return delegateTransformer.transformGlobalMetadata(metaData);
    }

    @Override
    public List<IndexMetadata> transformIndexMetadata(IndexMetadata indexData) {
        log.atInfo().setMessage("Delegating transformIndexMetadata to Transformer_ES_7_10_OS_2_11").log();
        return delegateTransformer.transformIndexMetadata(indexData);
    }
}
