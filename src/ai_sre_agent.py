#!/usr/bin/env python3
"""
AI SRE Agent v3 - Production Ready with Safety Features
========================================================
- Flask webhook receiver for Prometheus Alertmanager
- Loki integration for log querying
- Real K8s actions (scale, restart, rollback)
- Incident logging (SQLite)
- Groq/Llama AI for decision making
- SAFETY: Confidence thresholds + human approval + post-action verification
"""

import os
import json
import time
import sqlite3
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from kubernetes import client, config
from dotenv import load_dotenv
from groq import Groq
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

# Vector search for RAG
try:
    from vector_search import get_vector_search
    VECTOR_SEARCH_AVAILABLE = True
except ImportError:
    VECTOR_SEARCH_AVAILABLE = False
    get_vector_search = lambda: None

load_dotenv()

# =============================================================================
# PROMETHEUS METRICS
# =============================================================================
INCIDENTS_TOTAL = Counter('ai_sre_incidents_total', 'Total incidents processed', ['severity', 'alert_name'])
ACTIONS_TOTAL = Counter('ai_sre_actions_total', 'Total actions taken', ['action_type', 'success'])
CONFIDENCE_HISTOGRAM = Histogram('ai_sre_confidence', 'AI confidence scores', buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
PENDING_APPROVALS = Gauge('ai_sre_pending_approvals', 'Number of pending approvals')
AGENT_HEALTHY = Gauge('ai_sre_agent_healthy', 'Agent health status')

# =============================================================================
# CONFIGURATION
# =============================================================================
LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://139.59.77.78:30850")
TARGET_NAMESPACE = os.environ.get("TARGET_NAMESPACE", "ai-sre")
TARGET_DEPLOYMENT = os.environ.get("TARGET_DEPLOYMENT", "ai-sre-target")
DB_FILE = "incidents.db"

# SAFETY CONFIGURATION
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.8"))
AUTO_ACTION_ENABLED = os.environ.get("AUTO_ACTION_ENABLED", "true").lower() == "true"
REQUIRE_APPROVAL_FOR = os.environ.get("REQUIRE_APPROVAL_FOR", "rollback")  # comma-separated

app = Flask(__name__, static_folder='../static', static_url_path='/static')

# Pending approvals store
pending_approvals = {}

# =============================================================================
# KUBERNETES SETUP
# =============================================================================
try:
    # Try in-cluster config first (when running in K8s)
    try:
        config.load_incluster_config()
        print("✅ Kubernetes in-cluster config loaded")
    except:
        config.load_kube_config()
        print("✅ Kubernetes local config loaded")
    k8s_apps = client.AppsV1Api()
    k8s_core = client.CoreV1Api()
except Exception as e:
    print(f"⚠️ K8s connection failed: {e}")
    k8s_apps = None
    k8s_core = None

# =============================================================================
# GROQ SETUP
# =============================================================================
groq_client = None

def get_groq():
    global groq_client
    if not groq_client:
        api_key = os.environ.get("GROQ_API_KEY")
        if api_key:
            groq_client = Groq(api_key=api_key)
            print("✅ Groq client initialized")
    return groq_client

# =============================================================================
# DATABASE SETUP
# =============================================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS incidents
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timestamp TEXT,
                  alert_name TEXT,
                  severity TEXT,
                  namespace TEXT,
                  pod TEXT,
                  description TEXT,
                  logs TEXT,
                  ai_analysis TEXT,
                  confidence REAL,
                  action_taken TEXT,
                  action_verified INTEGER,
                  status TEXT)''')
    conn.commit()
    conn.close()
    print("✅ Incident database initialized")

def log_incident(alert_name, severity, namespace, pod, description, logs, 
                 ai_analysis, confidence, action_taken, verified=False, status="resolved"):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO incidents 
                 (timestamp, alert_name, severity, namespace, pod, description, logs, 
                  ai_analysis, confidence, action_taken, action_verified, status)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (datetime.utcnow().isoformat(), alert_name, severity, namespace, pod, 
               description, logs[:2000] if logs else "", ai_analysis, confidence,
               action_taken, 1 if verified else 0, status))
    conn.commit()
    incident_id = c.lastrowid
    conn.close()
    return incident_id

# =============================================================================
# LOKI INTEGRATION
# =============================================================================
def query_loki_logs(namespace, pod_name=None, limit=50):
    """Query Loki for recent logs from a pod or namespace."""
    try:
        if pod_name:
            query = f'{{namespace="{namespace}", pod=~"{pod_name}.*"}}'
        else:
            query = f'{{namespace="{namespace}"}}'
        
        response = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={
                "query": query,
                "limit": limit,
                "start": str(int((time.time() - 300) * 1e9)),
                "end": str(int(time.time() * 1e9))
            },
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            logs = []
            for stream in data.get("data", {}).get("result", []):
                for value in stream.get("values", []):
                    logs.append(value[1])
            return "\n".join(logs[-limit:])
        return None
    except Exception as e:
        print(f"Loki error: {e}")
        return None

# =============================================================================
# KUBERNETES ACTIONS
# =============================================================================
def get_pod_status(namespace):
    """Check for unhealthy pods in namespace."""
    if not k8s_core:
        return None
    
    try:
        pods = k8s_core.list_namespaced_pod(namespace)
        issues = []
        
        for pod in pods.items:
            if pod.status.container_statuses:
                for status in pod.status.container_statuses:
                    if status.state.waiting:
                        reason = status.state.waiting.reason
                        if reason in ["CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "OOMKilled"]:
                            issues.append({
                                "pod": pod.metadata.name,
                                "issue": reason,
                                "message": status.state.waiting.message
                            })
                    if status.state.terminated and status.state.terminated.reason == "OOMKilled":
                        issues.append({
                            "pod": pod.metadata.name,
                            "issue": "OOMKilled",
                            "exit_code": status.state.terminated.exit_code
                        })
        return issues
    except Exception as e:
        print(f"Error checking pods: {e}")
        return None

def fetch_pod_logs(namespace, pod_name):
    """Fetch logs from K8s API."""
    if not k8s_core:
        return "K8s not connected"
    
    try:
        logs = k8s_core.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=50
        )
        return logs
    except Exception as e:
        return f"Error fetching logs: {e}"

def scale_deployment(namespace, deployment, replicas):
    """Scale a deployment to specified replicas."""
    if not k8s_apps:
        return False, "K8s not connected"
    
    try:
        current = k8s_apps.read_namespaced_deployment_scale(deployment, namespace)
        if current.spec.replicas == replicas:
            return True, f"Already at {replicas} replicas"
        
        body = {"spec": {"replicas": replicas}}
        k8s_apps.patch_namespaced_deployment_scale(deployment, namespace, body)
        return True, f"Scaled {deployment} to {replicas} replicas"
    except Exception as e:
        return False, f"Scale failed: {e}"

def restart_deployment(namespace, deployment):
    """Perform rolling restart of deployment."""
    if not k8s_apps:
        return False, "K8s not connected"
    
    try:
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": datetime.utcnow().isoformat()
                        }
                    }
                }
            }
        }
        k8s_apps.patch_namespaced_deployment(deployment, namespace, body)
        return True, f"Restarted {deployment}"
    except Exception as e:
        return False, f"Restart failed: {e}"

def rollback_deployment(namespace, deployment):
    """Rollback to previous revision using ReplicaSets."""
    if not k8s_apps:
        return False, "K8s not connected"
    
    try:
        # Get all ReplicaSets for this deployment
        rs_list = k8s_apps.list_namespaced_replica_set(
            namespace,
            label_selector=f"app={deployment.replace('-deployment', '')}"
        )
        
        if len(rs_list.items) < 2:
            return False, "No previous revision available"
        
        # Sort by creation timestamp
        sorted_rs = sorted(rs_list.items, key=lambda x: x.metadata.creation_timestamp, reverse=True)
        
        # Get previous image
        if len(sorted_rs) >= 2:
            prev_rs = sorted_rs[1]
            prev_image = prev_rs.spec.template.spec.containers[0].image
            
            # Patch deployment with previous image
            body = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{
                                "name": prev_rs.spec.template.spec.containers[0].name,
                                "image": prev_image
                            }]
                        }
                    }
                }
            }
            k8s_apps.patch_namespaced_deployment(deployment, namespace, body)
            return True, f"Rolled back {deployment} to {prev_image}"
        
        return False, "Could not determine previous revision"
    except Exception as e:
        return False, f"Rollback failed: {e}"

def verify_deployment_health(namespace, deployment, timeout=30):
    """Verify deployment is healthy after action."""
    if not k8s_apps:
        return False, "K8s not connected"
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            dep = k8s_apps.read_namespaced_deployment(deployment, namespace)
            ready = dep.status.ready_replicas or 0
            desired = dep.spec.replicas or 1
            
            if ready >= desired:
                return True, f"Healthy: {ready}/{desired} replicas ready"
            
            time.sleep(5)
        except Exception as e:
            return False, f"Verification failed: {e}"
    
    return False, f"Timeout: deployment not healthy after {timeout}s"

# =============================================================================
# EMAIL NOTIFICATION
# =============================================================================
def send_email(subject, message):
    """Send notification via Slack (preferred) or Email."""
    import smtplib
    import requests
    import json
    from email.message import EmailMessage
    
    slack_webhook = os.environ.get('SLACK_WEBHOOK_URL')
    if slack_webhook:
        try:
            payload = {
                "text": f"🚨 *AI SRE Alert: {subject}*\n{message}"
            }
            response = requests.post(slack_webhook, json=payload, timeout=5)
            if response.status_code == 200:
                print(f"✅ Slack notification sent: {subject}")
                return True, "Slack notification sent"
            else:
                print(f"⚠️ Slack failed: {response.text}")
        except Exception as e:
            print(f"⚠️ Slack error: {e}")

    # Fallback to Gmail
    gmail_user = os.environ.get('GMAIL_USER')
    gmail_password = os.environ.get('GMAIL_APP_PASSWORD')
    
    if not gmail_user or not gmail_password:
        print(f"📧 [SIMULATED] {subject}: {message[:100]}...")
        return True, "Notification simulated (no credentials)"
    
    try:
        msg = EmailMessage()
        msg.set_content(message)
        msg['Subject'] = f"🚨 AI SRE: {subject}"
        msg['From'] = gmail_user
        msg['To'] = gmail_user
        
        # Try port 465 (SSL)
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(gmail_user, gmail_password)
            smtp.send_message(msg)
        
        print(f"📧 Email sent: {subject}")
        return True, "Email sent"
    except Exception as e:
        return False, f"Notification failed: {e}"

# =============================================================================
# AI DECISION ENGINE WITH SAFETY
# =============================================================================

# Import extended actions
try:
    from extended_actions import (
        get_deployment_status, get_pod_events, query_prometheus, check_node_health,
        cordon_node, uncordon_node, delete_pod, update_hpa, patch_resource_limits,
        drain_node, delete_deployment, apply_manifest, exec_in_pod,
        set_k8s_clients, get_action_risk
    )
    set_k8s_clients(k8s_core, k8s_apps)
    EXTENDED_ACTIONS_AVAILABLE = True
except ImportError:
    EXTENDED_ACTIONS_AVAILABLE = False

TOOLS = [
    # Basic Actions
    {"type": "function", "function": {
        "name": "scale_deployment",
        "description": "Scale a Kubernetes deployment to handle more load",
        "parameters": {"type": "object", "properties": {
            "replicas": {"type": "integer", "description": "Target replica count (1-10)"}
        }, "required": ["replicas"]}
    }},
    {"type": "function", "function": {
        "name": "restart_deployment",
        "description": "Restart the deployment to fix crashes or stuck pods",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "rollback_deployment",
        "description": "Rollback to previous version (use when restart doesn't help)",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "send_notification",
        "description": "Send email notification to the admin team",
        "parameters": {"type": "object", "properties": {
            "subject": {"type": "string", "description": "Email subject"},
            "message": {"type": "string", "description": "Email body"}
        }, "required": ["subject", "message"]}
    }},
    
    # Safe Actions (Read-only)
    {"type": "function", "function": {
        "name": "get_deployment_status",
        "description": "Get detailed status of a deployment (replicas, image, health)",
        "parameters": {"type": "object", "properties": {
            "namespace": {"type": "string", "description": "Kubernetes namespace"},
            "deployment": {"type": "string", "description": "Deployment name"}
        }, "required": ["namespace", "deployment"]}
    }},
    {"type": "function", "function": {
        "name": "get_pod_events",
        "description": "Get recent Kubernetes events for a pod (useful for debugging)",
        "parameters": {"type": "object", "properties": {
            "namespace": {"type": "string", "description": "Kubernetes namespace"},
            "pod_name": {"type": "string", "description": "Pod name (optional, leave empty for all events)"}
        }, "required": ["namespace"]}
    }},
    {"type": "function", "function": {
        "name": "query_prometheus",
        "description": "Execute a PromQL query to get metrics",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "PromQL query string"}
        }, "required": ["query"]}
    }},
    {"type": "function", "function": {
        "name": "check_node_health",
        "description": "Check health status of all cluster nodes",
        "parameters": {"type": "object", "properties": {}}
    }},
    
    # Medium Risk Actions
    {"type": "function", "function": {
        "name": "cordon_node",
        "description": "Mark a node as unschedulable (no new pods will be placed)",
        "parameters": {"type": "object", "properties": {
            "node_name": {"type": "string", "description": "Name of the node to cordon"}
        }, "required": ["node_name"]}
    }},
    {"type": "function", "function": {
        "name": "uncordon_node",
        "description": "Mark a node as schedulable again",
        "parameters": {"type": "object", "properties": {
            "node_name": {"type": "string", "description": "Name of the node to uncordon"}
        }, "required": ["node_name"]}
    }},
    {"type": "function", "function": {
        "name": "delete_pod",
        "description": "Delete a specific pod (useful for stuck pods)",
        "parameters": {"type": "object", "properties": {
            "namespace": {"type": "string", "description": "Kubernetes namespace"},
            "pod_name": {"type": "string", "description": "Pod name to delete"},
            "force": {"type": "boolean", "description": "Force delete without grace period"}
        }, "required": ["namespace", "pod_name"]}
    }},
    {"type": "function", "function": {
        "name": "patch_resource_limits",
        "description": "Update CPU/memory limits for a deployment",
        "parameters": {"type": "object", "properties": {
            "namespace": {"type": "string"},
            "deployment": {"type": "string"},
            "cpu_limit": {"type": "string", "description": "e.g., '500m' or '1'"},
            "memory_limit": {"type": "string", "description": "e.g., '256Mi' or '1Gi'"}
        }, "required": ["namespace", "deployment"]}
    }},
    
    # High Risk Actions (always require approval)
    {"type": "function", "function": {
        "name": "drain_node",
        "description": "Drain all pods from a node (for maintenance)",
        "parameters": {"type": "object", "properties": {
            "node_name": {"type": "string", "description": "Node to drain"}
        }, "required": ["node_name"]}
    }},
    {"type": "function", "function": {
        "name": "delete_deployment",
        "description": "Delete an entire deployment (DESTRUCTIVE)",
        "parameters": {"type": "object", "properties": {
            "namespace": {"type": "string"},
            "deployment": {"type": "string"}
        }, "required": ["namespace", "deployment"]}
    }},
    {"type": "function", "function": {
        "name": "exec_in_pod",
        "description": "Execute a command inside a pod (debugging)",
        "parameters": {"type": "object", "properties": {
            "namespace": {"type": "string"},
            "pod_name": {"type": "string"},
            "command": {"type": "string", "description": "Shell command to run"}
        }, "required": ["namespace", "pod_name", "command"]}
    }}
]

# Actions that always require approval (high risk)
HIGH_RISK_ACTIONS = ["drain_node", "delete_deployment", "exec_in_pod", "apply_manifest", "rollback_deployment"]

def ai_analyze_and_act(alert_data, logs):
    """Use AI to analyze alert and decide on action with confidence scoring."""
    client = get_groq()
    if not client:
        return None, 0.0, "AI not available", False
    
    # Get similar incidents from vector search (RAG)
    similar_context = ""
    vs = get_vector_search()
    if vs:
        similar_context = vs.get_context_prompt(alert_data)
    
    prompt = f"""You are an AI SRE Agent. Analyze this alert and decide on action.

ALERT DATA:
{json.dumps(alert_data, indent=2)}

RECENT LOGS:
{logs[:3000] if logs else "No logs available"}
{similar_context}
AVAILABLE ACTIONS:
1. scale_deployment(replicas=N) - Scale to N replicas for load/resource issues
2. restart_deployment() - Restart for crashes, stuck pods, or config changes
3. rollback_deployment() - Rollback to previous version (when current version is broken)
4. send_notification(subject, message) - Notify admin

RESPOND IN THIS EXACT FORMAT:
CONFIDENCE: [0.0-1.0 how confident you are in your diagnosis]
ROOT_CAUSE: [brief explanation]
RECOMMENDED_ACTION: [action name]

Then call the appropriate tools. Always send a notification summarizing the incident."""

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=1000
        )
        
        message = response.choices[0].message
        analysis = message.content or ""
        
        # Extract confidence from response
        confidence = 0.5  # Default
        for line in analysis.split('\n'):
            if line.startswith('CONFIDENCE:'):
                try:
                    confidence = float(line.split(':')[1].strip())
                except:
                    pass
        
        actions_taken = []
        all_verified = True
        
        if message.tool_calls:
            for tool_call in message.tool_calls:
                func_name = tool_call.function.name
                func_args = json.loads(tool_call.function.arguments)
                
                # Check if action requires approval
                requires_approval = func_name in REQUIRE_APPROVAL_FOR.split(',')
                
                # Check confidence threshold
                if confidence < CONFIDENCE_THRESHOLD and func_name != "send_notification":
                    print(f"⚠️ Low confidence ({confidence:.2f}), requiring approval for {func_name}")
                    pending_approvals[len(pending_approvals)] = {
                        "action": func_name,
                        "args": func_args,
                        "alert": alert_data,
                        "confidence": confidence
                    }
                    actions_taken.append(f"{func_name}: PENDING APPROVAL (confidence: {confidence:.2f})")
                    all_verified = False
                    continue
                
                if not AUTO_ACTION_ENABLED and func_name != "send_notification":
                    print(f"⚠️ Auto-action disabled, requiring approval for {func_name}")
                    pending_approvals[len(pending_approvals)] = {
                        "action": func_name,
                        "args": func_args,
                        "alert": alert_data,
                        "confidence": confidence
                    }
                    actions_taken.append(f"{func_name}: PENDING APPROVAL (auto-action disabled)")
                    all_verified = False
                    continue
                
                # Check if this is a high-risk action requiring approval
                if func_name in HIGH_RISK_ACTIONS:
                    print(f"⚠️ High-risk action, requiring approval for {func_name}")
                    pending_approvals[len(pending_approvals)] = {
                        "action": func_name,
                        "args": func_args,
                        "alert": alert_data,
                        "confidence": confidence
                    }
                    actions_taken.append(f"{func_name}: PENDING APPROVAL (high-risk)")
                    all_verified = False
                    continue
                
                print(f"🤖 AI Action: {func_name}({func_args}) [confidence: {confidence:.2f}]")
                
                # Execute action based on type
                success, result = False, "Unknown action"
                
                # Basic actions
                if func_name == "scale_deployment":
                    replicas = min(max(func_args.get("replicas", 2), 1), 10)
                    success, result = scale_deployment(TARGET_NAMESPACE, TARGET_DEPLOYMENT, replicas)
                elif func_name == "restart_deployment":
                    success, result = restart_deployment(TARGET_NAMESPACE, TARGET_DEPLOYMENT)
                elif func_name == "rollback_deployment":
                    success, result = rollback_deployment(TARGET_NAMESPACE, TARGET_DEPLOYMENT)
                elif func_name == "send_notification":
                    success, result = send_email(func_args.get("subject", "Alert"), func_args.get("message", ""))
                
                # Safe read-only actions
                elif func_name == "get_deployment_status" and EXTENDED_ACTIONS_AVAILABLE:
                    success, result = get_deployment_status(
                        func_args.get("namespace", TARGET_NAMESPACE),
                        func_args.get("deployment", TARGET_DEPLOYMENT)
                    )
                elif func_name == "get_pod_events" and EXTENDED_ACTIONS_AVAILABLE:
                    success, result = get_pod_events(
                        func_args.get("namespace", TARGET_NAMESPACE),
                        func_args.get("pod_name", "")
                    )
                elif func_name == "query_prometheus" and EXTENDED_ACTIONS_AVAILABLE:
                    success, result = query_prometheus(func_args.get("query", "up"))
                elif func_name == "check_node_health" and EXTENDED_ACTIONS_AVAILABLE:
                    success, result = check_node_health()
                
                # Medium risk actions
                elif func_name == "cordon_node" and EXTENDED_ACTIONS_AVAILABLE:
                    success, result = cordon_node(func_args.get("node_name"))
                elif func_name == "uncordon_node" and EXTENDED_ACTIONS_AVAILABLE:
                    success, result = uncordon_node(func_args.get("node_name"))
                elif func_name == "delete_pod" and EXTENDED_ACTIONS_AVAILABLE:
                    success, result = delete_pod(
                        func_args.get("namespace", TARGET_NAMESPACE),
                        func_args.get("pod_name"),
                        func_args.get("force", False)
                    )
                elif func_name == "patch_resource_limits" and EXTENDED_ACTIONS_AVAILABLE:
                    success, result = patch_resource_limits(
                        func_args.get("namespace", TARGET_NAMESPACE),
                        func_args.get("deployment", TARGET_DEPLOYMENT),
                        func_args.get("cpu_limit"),
                        func_args.get("memory_limit")
                    )
                
                # High risk actions (shouldn't reach here due to approval check)
                elif func_name == "drain_node" and EXTENDED_ACTIONS_AVAILABLE:
                    success, result = drain_node(func_args.get("node_name"))
                elif func_name == "delete_deployment" and EXTENDED_ACTIONS_AVAILABLE:
                    success, result = delete_deployment(
                        func_args.get("namespace", TARGET_NAMESPACE),
                        func_args.get("deployment")
                    )
                elif func_name == "exec_in_pod" and EXTENDED_ACTIONS_AVAILABLE:
                    success, result = exec_in_pod(
                        func_args.get("namespace", TARGET_NAMESPACE),
                        func_args.get("pod_name"),
                        func_args.get("command")
                    )
                
                actions_taken.append(f"{func_name}: {result}")
                
                # Record action metric
                ACTIONS_TOTAL.labels(action_type=func_name, success=str(success)).inc()
                
                # Verify deployment-related actions
                if func_name in ["scale_deployment", "restart_deployment", "rollback_deployment", "patch_resource_limits"]:
                    print("🔍 Verifying action...")
                    verified, verify_msg = verify_deployment_health(TARGET_NAMESPACE, TARGET_DEPLOYMENT)
                    if not verified:
                        all_verified = False
                        actions_taken.append(f"VERIFICATION FAILED: {verify_msg}")
                    else:
                        actions_taken.append(f"VERIFIED: {verify_msg}")
        
        return analysis, confidence, ", ".join(actions_taken) if actions_taken else "No action taken", all_verified
        
    except Exception as e:
        return None, 0.0, f"AI error: {e}", False

# =============================================================================
# WEBHOOK ENDPOINT
# =============================================================================
@app.route('/webhook', methods=['POST'])
def alertmanager_webhook():
    """Receive alerts from Prometheus Alertmanager."""
    data = request.json
    print(f"\n{'='*60}")
    print(f"🚨 ALERT RECEIVED at {datetime.now().isoformat()}")
    print(f"{'='*60}")
    
    alerts = data.get('alerts', [])
    results = []
    
    for alert in alerts:
        alert_name = alert.get('labels', {}).get('alertname', 'Unknown')
        severity = alert.get('labels', {}).get('severity', 'warning')
        namespace = alert.get('labels', {}).get('namespace', TARGET_NAMESPACE)
        pod = alert.get('labels', {}).get('pod', '')
        description = alert.get('annotations', {}).get('description', '')
        status = alert.get('status', 'firing')
        
        # Skip resolved alerts
        if status == 'resolved':
            print(f"  ✅ Alert {alert_name} resolved, skipping")
            continue
        
        print(f"  Alert: {alert_name}")
        print(f"  Severity: {severity}")
        print(f"  Pod: {pod}")
        
        # Fetch logs
        logs = ""
        if pod:
            logs = query_loki_logs(namespace, pod) or fetch_pod_logs(namespace, pod)
        
        # AI Analysis with confidence
        ai_analysis, confidence, action_taken, verified = ai_analyze_and_act(alert, logs)
        
        # Record metrics
        INCIDENTS_TOTAL.labels(severity=severity, alert_name=alert_name).inc()
        CONFIDENCE_HISTOGRAM.observe(confidence)
        
        # Log incident
        incident_id = log_incident(
            alert_name, severity, namespace, pod,
            description, logs, ai_analysis or "", confidence,
            action_taken, verified
        )
        
        # Store in vector DB for future RAG
        vs = get_vector_search()
        if vs:
            vs.store_incident({
                "id": incident_id,
                "alert_name": alert_name,
                "severity": severity,
                "description": description,
                "ai_analysis": ai_analysis,
                "action_taken": action_taken,
                "verified": verified,
                "timestamp": datetime.now().isoformat()
            })
        
        print(f"  Confidence: {confidence:.2f}")
        print(f"  Action: {action_taken}")
        print(f"  Verified: {verified}")
        print(f"  Incident ID: {incident_id}")
        
        results.append({
            "incident_id": incident_id,
            "confidence": confidence,
            "action": action_taken,
            "verified": verified
        })
    
    return jsonify({"status": "processed", "alerts": len(alerts), "results": results})

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    AGENT_HEALTHY.set(1)
    return jsonify({
        "status": "healthy",
        "k8s": k8s_apps is not None,
        "groq": get_groq() is not None,
        "loki": LOKI_URL,
        "auto_action": AUTO_ACTION_ENABLED,
        "confidence_threshold": CONFIDENCE_THRESHOLD
    })

@app.route('/metrics', methods=['GET'])
def metrics():
    """Prometheus metrics endpoint."""
    AGENT_HEALTHY.set(1 if k8s_apps else 0)
    PENDING_APPROVALS.set(len(pending_approvals))
    from flask import Response
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

@app.route('/incidents', methods=['GET'])
def list_incidents():
    """List recent incidents."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM incidents ORDER BY id DESC LIMIT 20')
    rows = c.fetchall()
    conn.close()
    
    incidents = []
    for row in rows:
        incidents.append({
            "id": row[0],
            "timestamp": row[1],
            "alert": row[2],
            "severity": row[3],
            "pod": row[5],
            "confidence": row[9],
            "action": row[10],
            "verified": bool(row[11]),
            "status": row[12]
        })
    
    return jsonify(incidents)

@app.route('/pending', methods=['GET'])
def list_pending():
    """List pending approval requests."""
    return jsonify(pending_approvals)

@app.route('/approve/<int:approval_id>', methods=['POST'])
def approve_action(approval_id):
    """Approve a pending action."""
    if approval_id not in pending_approvals:
        return jsonify({"error": "Approval not found"}), 404
    
    approval = pending_approvals.pop(approval_id)
    func_name = approval["action"]
    func_args = approval.get("args", {})
    
    print(f"✅ Approved: {func_name}({func_args})")
    
    if func_name == "scale_deployment":
        success, result = scale_deployment(TARGET_NAMESPACE, TARGET_DEPLOYMENT, func_args.get("replicas", 2))
    elif func_name == "restart_deployment":
        success, result = restart_deployment(TARGET_NAMESPACE, TARGET_DEPLOYMENT)
    elif func_name == "rollback_deployment":
        success, result = rollback_deployment(TARGET_NAMESPACE, TARGET_DEPLOYMENT)
    else:
        return jsonify({"error": "Unknown action"}), 400
    
    # Verify
    verified, verify_msg = verify_deployment_health(TARGET_NAMESPACE, TARGET_DEPLOYMENT)
    
    return jsonify({
        "action": func_name,
        "result": result,
        "verified": verified,
        "verification": verify_msg
    })

@app.route('/reject/<int:approval_id>', methods=['POST'])
def reject_action(approval_id):
    """Reject a pending action."""
    if approval_id not in pending_approvals:
        return jsonify({"error": "Approval not found"}), 404
    
    approval = pending_approvals.pop(approval_id)
    print(f"❌ Rejected: {approval['action']}")
    
    return jsonify({"status": "rejected", "action": approval["action"]})

@app.route('/trigger-test', methods=['POST'])
def trigger_test():
    """Manually trigger a test alert."""
    test_alert = {
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": "ManualTest",
                "severity": "critical",
                "namespace": TARGET_NAMESPACE,
                "pod": ""
            },
            "annotations": {
                "description": "Manual test alert triggered via API"
            }
        }]
    }
    
    issues = get_pod_status(TARGET_NAMESPACE)
    if issues:
        test_alert["alerts"][0]["labels"]["pod"] = issues[0]["pod"]
        test_alert["alerts"][0]["annotations"]["description"] = f"Detected: {issues[0]['issue']}"
    
    with app.test_client() as client:
        response = client.post('/webhook', json=test_alert)
        return response.json

@app.route('/config', methods=['GET', 'POST'])
def config_endpoint():
    """Get/Set runtime configuration."""
    global AUTO_ACTION_ENABLED, CONFIDENCE_THRESHOLD
    
    if request.method == 'POST':
        data = request.json
        if 'auto_action' in data:
            AUTO_ACTION_ENABLED = bool(data['auto_action'])
        if 'confidence_threshold' in data:
            CONFIDENCE_THRESHOLD = float(data['confidence_threshold'])
    
    return jsonify({
        "auto_action_enabled": AUTO_ACTION_ENABLED,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "require_approval_for": REQUIRE_APPROVAL_FOR.split(',')
    })

# =============================================================================
# CHAT ENDPOINT WITH TOOL CALLING AND MEMORY
# =============================================================================

# Conversation memory (simple in-memory store, keyed by session)
conversation_history = {}

# Chat-specific tools (INVESTIGATION ONLY - no destructive actions)
CHAT_TOOLS = [
    {"type": "function", "function": {
        "name": "get_cluster_summary",
        "description": "Get a summary of all pods and namespaces in the entire cluster",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "get_namespace_pods",
        "description": "Get pods in a SPECIFIC namespace (use this when user asks about a specific namespace)",
        "parameters": {"type": "object", "properties": {
            "namespace": {"type": "string", "description": "The namespace to query"}
        }, "required": ["namespace"]}
    }},
    {"type": "function", "function": {
        "name": "get_recent_incidents",
        "description": "Get recent incidents and actions taken by the agent",
        "parameters": {"type": "object", "properties": {}}
    }}
]

def execute_chat_action(func_name: str, func_args: dict) -> str:
    """Execute an action from chat and return result message."""
    try:
        if func_name == "delete_problem_pod":
            pod_name = func_args.get("pod_name")
            namespace = func_args.get("namespace", TARGET_NAMESPACE)
            k8s_core.delete_namespaced_pod(pod_name, namespace)
            return f"✅ Deleted pod {pod_name} in {namespace}"
        
        elif func_name == "restart_deployment":
            namespace = func_args.get("namespace", TARGET_NAMESPACE)
            deployment = func_args.get("deployment", TARGET_DEPLOYMENT)
            success, msg = restart_deployment(namespace, deployment)
            return f"✅ {msg}" if success else f"❌ {msg}"
        
        elif func_name == "scale_deployment":
            replicas = func_args.get("replicas", 2)
            namespace = func_args.get("namespace", TARGET_NAMESPACE)
            deployment = func_args.get("deployment", TARGET_DEPLOYMENT)
            success, msg = scale_deployment(namespace, deployment, replicas)
            return f"✅ {msg}" if success else f"❌ {msg}"
        
        elif func_name == "get_cluster_summary":
            all_pods = k8s_core.list_pod_for_all_namespaces()
            ns_counts = {}
            problems = []
            for p in all_pods.items:
                ns = p.metadata.namespace
                ns_counts[ns] = ns_counts.get(ns, 0) + 1
                for c in (p.status.container_statuses or []):
                    if c.state and c.state.waiting and c.state.waiting.reason in ["ImagePullBackOff", "CrashLoopBackOff"]:
                        problems.append(f"{ns}/{p.metadata.name}: {c.state.waiting.reason}")
            
            summary = f"Total: {len(all_pods.items)} pods across {len(ns_counts)} namespaces.\n"
            summary += f"Namespaces: {', '.join(sorted(ns_counts.keys()))}\n"
            if problems:
                summary += f"Problem pods: {', '.join(problems[:3])}"
            else:
                summary += "No problem pods found."
            return summary
        
        elif func_name == "get_namespace_pods":
            namespace = func_args.get("namespace", "default")
            pods = k8s_core.list_namespaced_pod(namespace)
            running = sum(1 for p in pods.items if p.status.phase == "Running")
            pod_list = []
            for p in pods.items[:10]:  # Limit to 10 pods
                status = p.status.phase
                pod_list.append(f"  - {p.metadata.name}: {status}")
            
            result = f"Namespace '{namespace}' has {len(pods.items)} pods ({running} running):\n"
            result += "\n".join(pod_list)
            return result
        
        elif func_name == "get_recent_incidents":
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT timestamp, alert_name, severity, pod, action_taken, action_verified FROM incidents ORDER BY id DESC LIMIT 5')
            rows = c.fetchall()
            conn.close()
            
            if not rows:
                return "No incidents recorded yet. The cluster has been healthy!"
            
            result = f"Last {len(rows)} incidents:\n"
            for row in rows:
                timestamp, alert, severity, pod, action, verified = row
                status = "✅ Verified" if verified else "⏳ Pending"
                result += f"  • {timestamp[:16]} | {alert} | {severity} | {action or 'none'} | {status}\n"
            return result
        
        return f"Unknown action: {func_name}"
    except Exception as e:
        return f"❌ Action failed: {e}"

@app.route('/ask', methods=['POST'])
def ask_agent():
    """Natural language chat endpoint with tool calling and memory."""
    data = request.json
    question = data.get('question', '')
    session_id = data.get('session_id', 'default')
    
    if not question:
        return jsonify({"error": "No question provided"}), 400
    
    # Get/create conversation history
    if session_id not in conversation_history:
        conversation_history[session_id] = []
    history = conversation_history[session_id]
    
    # Get cluster context
    try:
        all_pods = k8s_core.list_pod_for_all_namespaces()
        ns_counts = {}
        problem_pods = []
        running = 0
        
        for p in all_pods.items:
            ns = p.metadata.namespace
            ns_counts[ns] = ns_counts.get(ns, 0) + 1
            if p.status.phase == "Running":
                running += 1
            for c in (p.status.container_statuses or []):
                if c.state and c.state.waiting:
                    reason = c.state.waiting.reason
                    if reason in ["ImagePullBackOff", "CrashLoopBackOff", "ErrImagePull"]:
                        problem_pods.append({"name": p.metadata.name, "namespace": ns, "reason": reason})
        
        cluster_context = f"""CLUSTER STATUS:
- Total pods: {len(all_pods.items)} ({running} running)
- Namespaces ({len(ns_counts)}): {', '.join(sorted(ns_counts.keys()))}
- Problem pods: {len(problem_pods)}"""
        
        if problem_pods:
            cluster_context += "\n- Issues: " + ", ".join(f"{p['namespace']}/{p['name']} ({p['reason']})" for p in problem_pods[:3])
    except Exception as e:
        cluster_context = f"CLUSTER: Error fetching - {e}"
    
    # Build messages with history
    system_prompt = f"""You are an AI SRE Agent for INVESTIGATION ONLY. You can answer questions but CANNOT take actions.

{cluster_context}

RULES:
1. You can ONLY query information - no delete, restart, or scale
2. Use get_namespace_pods to list pods in a specific namespace
3. Use get_cluster_summary for full cluster overview
4. Use get_recent_incidents to show past alerts and actions
5. Be helpful and conversational

INVESTIGATION TOOLS:
- get_namespace_pods: List pods in a specific namespace
- get_cluster_summary: Full cluster overview
- get_recent_incidents: Show past incidents and actions"""

    messages = [{"role": "system", "content": system_prompt}]
    
    # Add conversation history (last 6 messages)
    for msg in history[-6:]:
        messages.append(msg)
    
    # Add current question
    messages.append({"role": "user", "content": question})
    
    # Get AI response with tools
    client = get_groq()
    if not client:
        return jsonify({"answer": "AI is not available.", "incidents": [], "action_taken": None})
    
    action_result = None
    
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=CHAT_TOOLS,
            tool_choice="auto",
            max_tokens=300
        )
        
        message = response.choices[0].message
        answer = message.content or ""
        
        # Track if a MUTATING action was taken (not just queries)
        mutating_action_taken = None
        read_only_actions = ["get_cluster_summary", "get_namespace_pods"]
        
        # Execute any tool calls
        if message.tool_calls:
            for tool_call in message.tool_calls:
                func_name = tool_call.function.name
                func_args = json.loads(tool_call.function.arguments)
                
                print(f"🤖 Chat executing: {func_name}({func_args})")
                result = execute_chat_action(func_name, func_args)
                
                # Only mark as "action taken" for mutating actions
                if func_name not in read_only_actions:
                    mutating_action_taken = result
                
                # Add result to answer
                if not answer:
                    answer = result
                else:
                    answer += f"\n\n{result}"
        
        # Save to history
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
        
        # Trim history if too long
        if len(history) > 20:
            history = history[-20:]
        conversation_history[session_id] = history
        
        return jsonify({
            "answer": answer,
            "incidents": [],
            "action_taken": mutating_action_taken  # Only set for mutating actions
        })
        
    except Exception as e:
        return jsonify({"answer": f"Oops! Something went wrong: {e}", "incidents": [], "action_taken": None})

@app.route('/')
def serve_frontend():
    """Serve the chat frontend."""
    return app.send_static_file('index.html')

# Enable CORS for frontend
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

# =============================================================================
# MAIN
# =============================================================================
# =============================================================================
# SLACK CHATOPS INTEGRATION
# =============================================================================

SYSTEM_PROMPT = """You are an AI SRE Agent for INVESTIGATION ONLY. You can answer questions but CANNOT take actions.

RULES:
1. You can ONLY query information - no delete, restart, or scale
2. Use get_namespace_pods to list pods in a specific namespace
3. Use get_cluster_summary for full cluster overview
4. Use get_recent_incidents to show past alerts and actions
5. Be helpful and conversational

INVESTIGATION TOOLS:
- get_namespace_pods: List pods in a specific namespace
- get_cluster_summary: Full cluster overview
- get_recent_incidents: Show past incidents and actions"""

def run_slack_bot():
    """Run Slack Socket Mode listener in a background thread."""
    if not os.environ.get("SLACK_APP_TOKEN") or not os.environ.get("SLACK_BOT_TOKEN"):
        print("⚠️ Slack tokens not found. ChatOps disabled.")
        return

    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        
        slack_app = App(token=os.environ["SLACK_BOT_TOKEN"])
        
        @slack_app.event("app_mention")
        def handle_mentions(body, say):
            text = body["event"]["text"]
            user = body["event"]["user"]
            thread_ts = body["event"].get("thread_ts", body["event"]["ts"])
            
            # Clean text (remove @bot mention)
            question = text.split(">", 1)[1].strip() if ">" in text else text
            
            print(f"💬 Slack Question from {user}: {question}")
            
            # Reuse logic (Simplified for Slack) - acting as a proxy to Groq
            try:
                # 1. Get History
                session_id = f"slack-{user}"
                history = conversation_history.get(session_id, [])
                
                # 2. Build Prompt
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT}
                ]
                messages.extend(history[-6:]) # Last 6
                messages.append({"role": "user", "content": question})
                
                # 3. Call AI
                response = groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=messages,
                    tools=CHAT_TOOLS,
                    tool_choice="auto",
                    max_tokens=300
                )
                
                ai_msg = response.choices[0].message
                answer = ai_msg.content or ""
                
                # 4. Handle Tools
                if ai_msg.tool_calls:
                    say(f"⚡ _Processing {len(ai_msg.tool_calls)} actions..._", thread_ts=thread_ts)
                    for tool_call in ai_msg.tool_calls:
                        func_name = tool_call.function.name
                        func_args = json.loads(tool_call.function.arguments)
                        
                        result = execute_chat_action(func_name, func_args)
                        answer += f"\n\n*Action {func_name}*: {result}"
                
                # 5. Reply
                say(f"🤖 {answer}", thread_ts=thread_ts)
                
                # 6. Save History
                history.append({"role": "user", "content": question})
                history.append({"role": "assistant", "content": answer})
                conversation_history[session_id] = history[-20:]
                
            except Exception as e:
                say(f"❌ Error processing request: {str(e)}", thread_ts=thread_ts)

        print("🟢 Starting Slack Socket Mode...")
        handler = SocketModeHandler(slack_app, os.environ["SLACK_APP_TOKEN"])
        handler.start()
        
    except ImportError:
        print("⚠️ Slack libraries not installed.")
    except Exception as e:
        print(f"❌ Slack Helper Error: {e}")

if __name__ == '__main__':
    init_db()
    
    # Start Slack Thread
    import threading
    threading.Thread(target=run_slack_bot, daemon=True).start()
    
    print("\n" + "="*60)
    print("🚀 AI SRE Agent v3 - Production Ready with Safety")
    print("="*60)
    print(f"  Webhook:    http://0.0.0.0:5000/webhook")
    print(f"  Health:     http://0.0.0.0:5000/health")
    print(f"  Incidents:  http://0.0.0.0:5000/incidents")
    print(f"  Pending:    http://0.0.0.0:5000/pending")
    print(f"  Config:     http://0.0.0.0:5000/config")
    print(f"  Test:       curl -X POST http://localhost:5000/trigger-test")
    print("="*60)
    print(f"  Auto-Action: {AUTO_ACTION_ENABLED}")
    print(f"  Confidence Threshold: {CONFIDENCE_THRESHOLD}")
    print("="*60 + "\n")
    
    app.run(host='0.0.0.0', port=5000, debug=False)
