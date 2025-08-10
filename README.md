#Kubernetes AI Healing Demo

- Complete demo setup for autonomous Kubernetes healing using AI (Ollama) + GitOps workflow.

## 🚀 Environment Setup
### 1. Install & Start Kubernetes Cluster
```bash
# Install Colima
brew install colima

# Start Colima with QEMU
colima start --vm-type=qemu --cpu 4 --memory 6 --disk 20

# Verify Docker is working
docker ps

# Start Minikube with Docker driver
minikube start --driver=docker --cpus=4 --memory=6g

# Enable Ingress (optional)
minikube addons enable ingress
```
### 2. Deploy Prometheus & Grafana
```bash
# Add Prometheus Helm repo
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

# Create monitoring namespace
kubectl create namespace monitoring

# Install Prometheus + Grafana stack
helm install prom-stack prometheus-community/kube-prometheus-stack -n monitoring

# Access services via port-forward
kubectl port-forward -n monitoring svc/prom-stack-grafana 3000:80
kubectl port-forward -n monitoring svc/prom-stack-kube-prometheus-alertmanager 9093:9093
kubectl port-forward -n monitoring svc/prom-stack-kube-prometheus-prometheus 9090:9090

# Access Grafana: http://localhost:3000
# Default credentials: admin/prom-operator
# Add Slack webhook in Alerting → Contact Points
```
### 3. Deploy ArgoCD
```bash
# Create ArgoCD namespace and install
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Access ArgoCD UI
kubectl port-forward svc/argocd-server -n argocd 8081:443

# Get admin password
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d; echo
# Access ArgoCD: https://localhost:8081
# Credentials: admin/<password-from-above>
```

## 📁 Repository Structure
```text
├── ollama_alert_bridge.py     # AI healing service (Flask webhook)
├── requirements.txt           # Python dependencies
├── app/                       # Failing Kubernetes applications
│   ├── imagepullbackoff-fail.yaml
│   ├── readiness-fail.yaml
│   ├── liveness-fail.yaml
│   └── commandfail-fail.yaml
└── README.md                  # This file
```

## 🤖 AI Healing Service - ollama_alert_bridge.py
Purpose: Flask webhook service that receives Grafana alerts and automatically fixes Kubernetes pod failures using local Ollama LLM.

Key Features:

Receives webhook alerts from Grafana/Alertmanager

Analyzes failures using Ollama (Llama3 model)

Generates specific YAML fixes (no placeholders)

Creates GitHub pull requests automatically

Integrates with ArgoCD for GitOps deployment

Sends Slack notifications

Required Environment Variables:
```text
export OLLAMA_URL="http://127.0.0.1:11434/api/generate"
export GITHUB_TOKEN="your-github-token"
export GITHUB_REPO="username/repo-name"
export SLACK_WEBHOOK_URL="https://hooks.slack.com/your-webhook"
```

## 🎯 Demo Execution (3-4 minutes)

### 1. Start Ollama locally
```bash
ollama serve
```

### 2. Start AI Healing Service
```bash
# Install dependencies
pip install -r requirements.txt

# Run the Flask service
python ollama_alert_bridge.py
```

### 3. Observe App fails and alerts
- Check Grafana alerts
- Check slack for messages

### 4. MErge PR and observe auto healing
