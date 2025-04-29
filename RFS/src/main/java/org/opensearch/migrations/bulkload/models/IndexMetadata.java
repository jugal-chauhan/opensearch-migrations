package org.opensearch.migrations.bulkload.models;

import java.io.ByteArrayInputStream;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.zip.InflaterInputStream;
import java.util.zip.Inflater;

import org.opensearch.migrations.bulkload.common.ByteArrayIndexInput;
import org.opensearch.migrations.bulkload.common.RfsException;
import org.opensearch.migrations.bulkload.common.SnapshotRepo;
import org.opensearch.migrations.transformation.entity.Index;

import com.fasterxml.jackson.annotation.JsonTypeInfo;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.dataformat.smile.SmileFactory;
import shadow.lucene9.org.apache.lucene.codecs.CodecUtil;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

// All subclasses need to be annotated with this
@JsonTypeInfo(use = JsonTypeInfo.Id.CLASS, property = "type")
public abstract class IndexMetadata implements Index {
    /*
    * Defines the behavior expected of an object that will surface the metadata of an index stored in a snapshot
    * See: https://github.com/elastic/elasticsearch/blob/v7.10.2/server/src/main/java/org/elasticsearch/cluster/metadata/IndexMetadata.java#L1475
    * See: https://github.com/elastic/elasticsearch/blob/v6.8.23/server/src/main/java/org/elasticsearch/cluster/metadata/IndexMetaData.java#L1284
    */
    private static final Logger logger = LoggerFactory.getLogger(IndexMetadata.class);
    public abstract JsonNode getAliases();

    public abstract String getId();

    public abstract JsonNode getMappings();

    public abstract int getNumberOfShards();

    public abstract JsonNode getSettings();

    public abstract IndexMetadata deepCopy();

    /**
    * Defines the behavior required to read a snapshot's index metadata as JSON and convert it into a Data object
    */
    public static interface Factory {
        private JsonNode getJsonNode(String indexId, String indexFileId, SmileFactory smileFactory) {
            Path filePath = getRepoDataProvider().getRepo().getIndexMetadataFilePath(indexId, indexFileId);

            try (InputStream fis = new FileInputStream(filePath.toFile())) {
                // Don't fully understand what the value of this code is, but it progresses the stream so we need to do
                // it
                // See:
                // https://github.com/elastic/elasticsearch/blob/6.8/server/src/main/java/org/elasticsearch/repositories/blobstore/ChecksumBlobStoreFormat.java#L100
                byte[] bytes = fis.readAllBytes();
                
                // Create hex dump for logging
                int logLength = Math.min(1000, bytes.length);
                byte[] first100Bytes = Arrays.copyOfRange(bytes, 0, logLength);
                StringBuilder hexDump = new StringBuilder();
                
                for (int i = 0; i < logLength; i++) {
                    if (i % 16 == 0) {
                        if (i > 0) hexDump.append('\n');
                        hexDump.append(String.format("%04x: ", i));
                    }
                    hexDump.append(String.format("%02x ", first100Bytes[i] & 0xFF));
                    
                    if ((i + 1) % 16 == 0 || i == logLength - 1) {
                        // Pad for incomplete lines
                        int padding = 16 - (i % 16) - 1;
                        for (int p = 0; p < padding; p++) {
                            hexDump.append("   ");
                        }
                        hexDump.append(" |");
                        // Add ASCII representation
                        int start = i - (i % 16);
                        int end = Math.min(start + 16, logLength);
                        for (int j = start; j < end; j++) {
                            char c = (char) first100Bytes[j];
                            hexDump.append(c >= 32 && c < 127 ? c : '.');
                        }
                        hexDump.append("|");
                    }
                }

                try {
                    ByteArrayIndexInput indexInput = new ByteArrayIndexInput("index-metadata", bytes);
                    CodecUtil.checksumEntireFile(indexInput);
                    CodecUtil.checkHeader(indexInput, "index-metadata", 1, 1);
                    int filePointer = (int) indexInput.getFilePointer();
                    logger.info("filePointer = {}", filePointer);
                    // filePointer += 4;
                    
                    
                    InputStream compressedInput = new ByteArrayInputStream(bytes, filePointer, bytes.length - filePointer);
                    // Check and strip DFL header
                    byte[] dflHeader = new byte[4];
                    if (compressedInput.read(dflHeader) != 4 || dflHeader[0] != 'D' || dflHeader[1] != 'F' || dflHeader[2] != 'L' || dflHeader[3] != 0) {
                        throw new IOException("Invalid DFL header in compressed metadata");
                    }
                    // InputStream bis = new java.util.zip.InflaterInputStream(compressedInput);
                    InflaterInputStream inflaterInput = new InflaterInputStream(compressedInput, new Inflater(true));
                    ObjectMapper smileMapper = new ObjectMapper(smileFactory);
                    JsonNode result = smileMapper.readTree(inflaterInput);
                    try {
                        // result = smileMapper.readTree(inflaterInput);
                        if (result.isTextual() || result.size() == 0) {
                            String resultStr = result.toString();
                            logger.error("Failed to parse metadata for index ID {}. Parsed result is not valid JSON: {}. Hex dump of first {} bytes:\n{}", indexId, resultStr, logLength, hexDump.toString());
                            throw new IOException("Invalid metadata format: " + resultStr);
                        }
                        return result;
                    } catch (IOException e) {
                        logger.error("Failed to parse metadata. Hex dump of first {} bytes:\n{}", 
                            logLength, hexDump.toString());
                        throw e;
                    }
                } catch (Exception e) {
                    logger.error("Failed to process metadata file. Hex dump of first {} bytes:\n{}", 
                        logLength, hexDump.toString());
                    throw new RfsException("Could not load index metadata file: " + filePath.toString(), e);
                }
            } catch (IOException e) {
                throw new RfsException("Could not read metadata file: " + filePath.toString(), e);
            }
        }

        default IndexMetadata fromRepo(String snapshotName, String indexName) {
            SmileFactory smileFactory = getSmileFactory();
            String indexId = getRepoDataProvider().getIndexId(indexName);
            String indexFileId = getIndexFileId(snapshotName, indexName);
            JsonNode root = getJsonNode(indexId, indexFileId, smileFactory);
            return fromJsonNode(root, indexId, indexName);
        }

        // Version-specific implementation
        IndexMetadata fromJsonNode(JsonNode root, String indexId, String indexName);

        // Version-specific implementation
        SmileFactory getSmileFactory();

        // Version-specific implementation
        String getIndexFileId(String snapshotName, String indexName);

        // Get the underlying SnapshotRepo Provider
        SnapshotRepo.Provider getRepoDataProvider();
    }
}
