package org.opensearch.migrations.bulkload.version_es_6_8;

import org.opensearch.migrations.bulkload.models.GlobalMetadata;

import com.fasterxml.jackson.core.JsonPointer;
import com.fasterxml.jackson.databind.node.ObjectNode;

public class GlobalMetadataData_ES_6_8 implements GlobalMetadata {
    private final ObjectNode root;

    public GlobalMetadataData_ES_6_8(ObjectNode root) {
        this.root = root;
    }

    @Override
    public ObjectNode toObjectNode() {
        return root;
    }

    @Override
    public JsonPointer getTemplatesPath() {
        return JsonPointer.compile("/templates");
    }

    @Override
    public JsonPointer getIndexTemplatesPath() {
        return JsonPointer.compile("/index_template");
    }

    @Override
    public JsonPointer getComponentTemplatesPath() {
        return JsonPointer.compile("/component_template");
    }
}
