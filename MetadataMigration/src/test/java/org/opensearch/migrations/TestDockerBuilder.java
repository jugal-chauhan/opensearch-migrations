package org.opensearch.migrations;

import org.opensearch.migrations.utils.DockerImageBuilder;
import java.nio.file.Path;

/**
 * Simple test class for DockerImageBuilder
 */
public class TestDockerBuilder {
    public static void main(String[] args) {
        if (args.length < 1 || !args[0].equals("test-docker-builder")) {
            return;
        }
        
        String buildId = args.length > 1 ? args[1] : "test";
        
        // Get path to test Dockerfile
        Path dockerfileDir = DockerImageBuilder.getResourcePath("docker/test-image");
        
        // Build the image
        String imageId = DockerImageBuilder.buildImage(dockerfileDir, "docker-image-builder-test:latest");
        
        System.out.println("DockerImageBuilder: Built image with ID: " + imageId + " (Build: " + buildId + ")");
    }
}
