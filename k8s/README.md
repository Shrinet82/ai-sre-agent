# Kubernetes Infrastructure ☸️

This directory contains the Declarative Manifests (`.yaml`) required to deploy the Observability Stack and the Target Workload.

## 🏗️ Architecture

The cluster is divided into two logical namespaces:

1.  **`monitoring`**: Hosts the SRE tools (Prometheus, Grafana).
2.  **`app`**: Hosts the target application (Nginx).

## 📄 Manifests Explained

### 1. Global Config

- **`00-namespaces.yaml`**: Creates the `monitoring` and `app` namespaces to ensure isolation.

### 2. Observability Stack (Prometheus)

- **`01-prometheus.yaml`**:
  - **ConfigMap**: Defines `prometheus.yml` scrape configs.
    - _Job `kubernetes-cadvisor`_: Scrapes container metrics (CPU, RAM, Network) from Kubelet via HTTPS.
    - _Job `kubernetes-pods`_: Scrapes application metrics from Pods with `@prometheus.io/scrape` annotations.
  - **Deployment**: Runs the Prometheus server (v2.x).
  - **Service**: NodePort `30009` (Access UI at `http://localhost:30009`).
  - **RBAC**: Grants permission to read Nodes, Pods, and Services from the K8s API.

### 3. Visualization (Grafana)

- **`02-grafana.yaml`**:
  - **Deployment**: runs Grafana.
    - Mounts `grafana-datasources` to auto-connect to Prometheus.
    - Mounts `grafana-dashboards` to auto-load the "AI SRE" dashboard.
  - **Service**: NodePort `30003` (Access UI at `http://localhost:30003`).
- **`04-dashboards.yaml`**:
  - Contains the JSON Model for the **"AI SRE: Nginx Monitor"** dashboard.
  - Uses Grafana Provisioning to "bake in" the dashboard as code (GitOps).

### 4. Target Application

- **`03-nginx.yaml`**:
  - **Deployment**: Nginx Web Server.
  - **Resources**: Hard limits set to `cpu: 200m`. _Crucial for forcing CPU spikes during load testing._
  - **Service**: NodePort `30007` (Access App at `http://localhost:30007`).

## 🚀 Deployment Order

Kubernetes applies files generally in order, but it's best to apply namespaces first.

```bash
# 1. Create Namespaces
kubectl apply -f 00-namespaces.yaml

# 2. Deploy Prometheus (so it's ready to scrape)
kubectl apply -f 01-prometheus.yaml

# 3. Configure Dashboards
kubectl apply -f 04-dashboards.yaml

# 4. Deploy Grafana
kubectl apply -f 02-grafana.yaml

# 5. Deploy App
kubectl apply -f 03-nginx.yaml
```
