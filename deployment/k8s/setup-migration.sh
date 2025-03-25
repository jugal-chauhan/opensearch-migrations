#!/bin/bash

# Creates necessary namespaces if they don't exist
# Installs/upgrades all components using Helm
# Creates the snapshot PVC
# Waits for all pods to be ready
# Provides instructions for accessing the clusters and console

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

# Create namespaces if they don't exist
print_header "Setting up namespaces"
for ns in ma ma-target; do
    if ! check_namespace $ns; then
        echo "Creating namespace $ns"
        kubectl create namespace $ns
    else
        echo "Namespace $ns already exists"
    fi
done

# Install migration assistant
print_header "Installing Migration Assistant"
helm upgrade --install ma -n ma charts/aggregates/migrationAssistant

# Install source cluster
print_header "Installing source Elasticsearch cluster"
helm upgrade --install tc-source -n ma charts/components/elasticsearchCluster \
    -f charts/components/elasticsearchCluster/environments/es-5-6-single-node-cluster.yaml

# Install target cluster
print_header "Installing target Elasticsearch cluster"
helm upgrade --install tc-target -n ma-target charts/components/elasticsearchCluster \
    -f charts/components/elasticsearchCluster/environments/es-5-6-single-node-cluster.yaml

# Create snapshot PVC in target namespace
print_header "Creating snapshot PVC"
kubectl apply -f deployment/k8s/snapshot-pvc.yaml -n ma-target

# Wait for pods to be ready
print_header "Waiting for pods to be ready"
wait_for_pods "ma"
wait_for_pods "ma-target"

print_header "Setup Complete!"
echo -e "To access the clusters and console, run these commands in separate terminals:"
echo -e "\n${YELLOW}Source cluster:${NC}"
echo "kubectl port-forward -n ma svc/elasticsearch-master 9200:9200"
echo -e "\n${YELLOW}Target cluster:${NC}"
echo "kubectl port-forward -n ma-target svc/elasticsearch-master 9201:9200"
echo -e "\n${YELLOW}Migration console:${NC}"
echo 'kubectl exec -it -n ma $(kubectl get pods -n ma -l app=ma-migration-console -o jsonpath="{.items[0].metadata.name}") -c console -- /bin/bash'

echo -e "\n${GREEN}You can then access:${NC}"
echo "- Source Elasticsearch: http://localhost:9200"
echo "- Target Elasticsearch: http://localhost:9201"
