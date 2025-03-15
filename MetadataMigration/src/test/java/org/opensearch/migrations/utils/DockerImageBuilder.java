package org.opensearch.migrations.utils;

import java.nio.file.Path;

import lombok.extern.slf4j.Slf4j;
import org.testcontainers.DockerClientFactory;
import org.testcontainers.images.builder.ImageFromDockerfile;

/**
 * Utility class to build Docker images with effective layer caching
 */
@Slf4j
public class DockerImageBuilder {
    
    /**
     * Builds a Docker image from a Dockerfile in the specified directory
     * Uses Docker's built-in layer caching for efficient rebuilds
     *
     * @param dockerfileDir Directory containing the Dockerfile
     * @param imageName Name for the built image
     * @return The built image ID
     */
    public static String buildImage(Path dockerfileDir, String imageName) {
        // Check if image already exists in Docker
        if (imageExists(imageName)) {
            log.info("Using existing Docker image: {} (CACHE HIT)", imageName);
            return imageName;
        }
        
        log.info("Building Docker image: {} from {}", imageName, dockerfileDir);
        
        // Record start time to measure build duration
        long startTime = System.currentTimeMillis();
        
        // Build the image with Docker's layer caching explicitly enabled
        ImageFromDockerfile imageBuilder = new ImageFromDockerfile(imageName, false) // false = don't delete intermediate containers
            .withDockerfile(dockerfileDir.resolve("Dockerfile"))
            // Explicitly set the build context directory to ensure consistent layer hashing
            .withFileFromPath(".", dockerfileDir);
            
        // Add build arguments to explicitly enable caching
        try {
            // Check if the image already exists in Docker registry (might be different from local)
            // If it does, use it as a cache source
            if (DockerClientFactory.instance().client()
                    .listImagesCmd()
                    .withImageNameFilter(imageName)
                    .exec().size() > 0) {
                log.info("Found existing image in registry to use as cache source: {}", imageName);
                imageBuilder = imageBuilder.withBuildArg("BUILDKIT_INLINE_CACHE", "1");
            }
        } catch (Exception e) {
            log.warn("Error checking for image in registry: {}", e.getMessage());
        }
        
        // Get the image ID
        String imageId = imageBuilder.get();
        
        // Calculate build duration
        long duration = System.currentTimeMillis() - startTime;
        
        log.info("Built Docker image: {} with ID: {} in {} ms", imageName, imageId, duration);
        
        return imageId;
    }
    
    /**
     * Utility method to get canonical paths
     */
    public static Path getResourcePath(String path) {
        try {
            return Path.of(DockerImageBuilder.class
                .getClassLoader()
                .getResource(path)
                .toURI());
        } catch (Exception e) {
            throw new RuntimeException("Failed to get resource path: " + path, e);
        }
    }
    
    /**
     * Check if an image exists in the Docker daemon
     * Uses a more reliable method than inspectImageCmd
     */
    public static boolean imageExists(String imageName) {
        try {
            // Use listImagesCmd with a filter which is more reliable
            return DockerClientFactory.instance().client()
                .listImagesCmd()
                .withImageNameFilter(imageName)
                .exec()
                .size() > 0;
        } catch (Exception e) {
            log.warn("Error checking if image exists: {}", e.getMessage());
            return false;
        }
    }
}
