package org.opensearch.migrations.bulkload.models;

import java.io.ByteArrayInputStream;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Path;
import java.util.List;
import java.util.zip.Inflater;
import java.util.zip.InflaterInputStream;

import org.opensearch.migrations.bulkload.common.ByteArrayIndexInput;
import org.opensearch.migrations.bulkload.common.RfsException;
import org.opensearch.migrations.bulkload.common.SnapshotRepo;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.dataformat.smile.SmileFactory;
import shadow.lucene9.org.apache.lucene.codecs.CodecUtil;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Defines the behavior expected of an object that will surface the metadata of an shard stored in a snapshot
 * See: https://github.com/elastic/elasticsearch/blob/7.10/server/src/main/java/org/elasticsearch/index/snapshots/blobstore/BlobStoreIndexShardSnapshot.java#L510
 * See: https://github.com/elastic/elasticsearch/blob/6.8/server/src/main/java/org/elasticsearch/index/snapshots/blobstore/BlobStoreIndexShardSnapshot.java#L508
 */
public interface ShardMetadata {

    Logger logger = LoggerFactory.getLogger(ShardMetadata.class);
    public String getSnapshotName();

    public String getIndexName();

    public String getIndexId();

    public int getShardId();

    public int getIndexVersion();

    public long getStartTime();

    public long getTime();

    public int getNumberOfFiles();

    public long getTotalSizeBytes();

    public List<ShardFileInfo> getFiles();

    /**
    * Defines the behavior required to read a snapshot's shard metadata as JSON and convert it into a Data object
    */
    public static interface Factory {
        private JsonNode getJsonNode(String snapshotId, String indexId, int shardId, SmileFactory smileFactory) {
            Path filePath = getRepoDataProvider().getRepo().getShardMetadataFilePath(snapshotId, indexId, shardId);

            try (InputStream fis = new FileInputStream(filePath.toFile())) {
                // Don't fully understand what the value of this code is, but it progresses the stream so we need to do
                // it
                // See:
                // https://github.com/elastic/elasticsearch/blob/6.8/server/src/main/java/org/elasticsearch/repositories/blobstore/ChecksumBlobStoreFormat.java#L100
                byte[] bytes = fis.readAllBytes();
                ByteArrayIndexInput indexInput = new ByteArrayIndexInput("index-metadata", bytes);
                CodecUtil.checksumEntireFile(indexInput);
                CodecUtil.checkHeader(indexInput, "snapshot", 1, 1);
                int filePointer = (int) indexInput.getFilePointer();
                InputStream bis = new ByteArrayInputStream(bytes, filePointer, bytes.length - filePointer);
                InputStream compressedInput = new ByteArrayInputStream(bytes, filePointer, bytes.length - filePointer);
                byte[] dflHeader = new byte[4];
                if (compressedInput.read(dflHeader) != 4 || dflHeader[0] != 'D' || dflHeader[1] != 'F' || dflHeader[2] != 'L' || dflHeader[3] != 0) {
                    throw new IOException("Invalid DFL header in compressed metadata");
                }
                InflaterInputStream inflaterInput = new InflaterInputStream(compressedInput, new Inflater(true));
                ObjectMapper smileMapper = new ObjectMapper(smileFactory);
                return smileMapper.readTree(inflaterInput);
            } catch (Exception e) {
                throw new CouldNotParseShardMetadata(
                    "Could not parse shard metadata for Snapshot "
                        + snapshotId
                        + ", Index "
                        + indexId
                        + ", Shard "
                        + shardId,
                    e
                );
            }
        }

        default ShardMetadata fromRepo(String snapshotName, String indexName, int shardId) {
            SmileFactory smileFactory = getSmileFactory();
            String snapshotId = getRepoDataProvider().getSnapshotId(snapshotName);
            String indexId = getRepoDataProvider().getIndexId(indexName);
            JsonNode root = getJsonNode(snapshotId, indexId, shardId, smileFactory);
            return fromJsonNode(root, indexId, indexName, shardId);
        }

        // Version-specific implementation
        public ShardMetadata fromJsonNode(JsonNode root, String indexId, String indexName, int shardId);

        // Version-specific implementation
        public SmileFactory getSmileFactory();

        // Get the underlying SnapshotRepo Provider
        public SnapshotRepo.Provider getRepoDataProvider();
    }

    public static class CouldNotParseShardMetadata extends RfsException {
        public CouldNotParseShardMetadata(String message, Throwable cause) {
            super(message, cause);
        }
    }
}
