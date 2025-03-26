#!/bin/bash

# Creates necessary namespace if it doesn't exist
# Installs/upgrades all components using Helm
# Creates the snapshot PVC
# Waits for all pods to be ready

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print section headers
print_header() {
    echo -e "\n${GREEN}=== $1 ===${NC}\n"
}

# Function to check if a namespace exists
check_namespace() {
    kubectl get namespace $1 >/dev/null 2>&1
}

# Function to wait for pods to be ready
wait_for_pods() {
    namespace=$1
    echo -e "${YELLOW}Waiting for pods in namespace $namespace to be ready...${NC}"
    kubectl wait --for=condition=ready pod --all -n $namespace --timeout=300s
}

# Create namespace if it doesn't exist
print_header "Setting up namespace"
if ! check_namespace ma; then
    echo "Creating namespace ma"
    kubectl create namespace ma
else
    echo "Namespace ma already exists"
fi

# Install Migration Assistant
print_header "Installing Migration Assistant"
helm upgrade --install ma -n ma charts/aggregates/migrationAssistant

# Install source ES cluster
print_header "Installing source Elasticsearch cluster"
helm upgrade --install elasticsearch-source -n ma charts/components/elasticsearchCluster \
    -f charts/components/elasticsearchCluster/environments/es-5-6-single-node-cluster.yaml \
    --set elasticsearch.clusterName=elasticsearch-source

# Install target ES cluster
print_header "Installing target Elasticsearch cluster"
helm upgrade --install elasticsearch-target -n ma charts/components/elasticsearchCluster \
    -f charts/components/elasticsearchCluster/environments/es-5-6-single-node-cluster.yaml \
    --set elasticsearch.clusterName=elasticsearch-target

# Create snapshot PVC
print_header "Creating snapshot PVC"
kubectl apply -f snapshot-pvc.yaml

# Wait for pods to be ready
print_header "Waiting for pods to be ready"
wait_for_pods ma

print_header "Setup Complete!"
echo -e "
To access the clusters and console, run these commands in separate terminals:

Source cluster:
kubectl port-forward -n ma svc/elasticsearch-source 9200:9200

Target cluster:
kubectl port-forward -n ma svc/elasticsearch-target 9201:9200

Migration console:
kubectl exec -it -n ma \$(kubectl get pods -n ma -l app=ma-migration-console -o jsonpath=\"{.items[0].metadata.name}\") -c console -- /bin/bash

You can then access:
- Source Elasticsearch: http://localhost:9200
- Target Elasticsearch: http://localhost:9201"
