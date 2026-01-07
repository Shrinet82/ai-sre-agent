"""
Extended K8s Actions for AI SRE Agent
=====================================
Additional actions beyond basic scale/restart/rollback.
Organized by risk level.
"""

import os
import yaml
import time
from datetime import datetime
from typing import Tuple, Optional, Dict, Any

# Import K8s clients from main agent (will be set by main module)
k8s_core = None
k8s_apps = None

def set_k8s_clients(core, apps):
    """Set K8s clients from main module."""
    global k8s_core, k8s_apps
    k8s_core = core
    k8s_apps = apps

# =============================================================================
# SAFE ACTIONS (Read-only, auto-approve)
# =============================================================================

def get_deployment_status(namespace: str, deployment: str) -> Tuple[bool, str]:
    """Get deployment health status."""
    if not k8s_apps:
        return False, "K8s not connected"
    
    try:
        dep = k8s_apps.read_namespaced_deployment(deployment, namespace)
        
        replicas = dep.spec.replicas or 0
        ready = dep.status.ready_replicas or 0
        available = dep.status.available_replicas or 0
        updated = dep.status.updated_replicas or 0
        
        status = f"""Deployment: {deployment}
Namespace: {namespace}
Replicas: {ready}/{replicas} ready
Available: {available}
Updated: {updated}
Strategy: {dep.spec.strategy.type}
Image: {dep.spec.template.spec.containers[0].image}"""
        
        return True, status
    except Exception as e:
        return False, f"Failed: {e}"

def get_pod_events(namespace: str, pod_name: str = "") -> Tuple[bool, str]:
    """Get recent events for a pod or namespace."""
    if not k8s_core:
        return False, "K8s not connected"
    
    try:
        if pod_name:
            field_selector = f"involvedObject.name={pod_name}"
        else:
            field_selector = None
        
        events = k8s_core.list_namespaced_event(
            namespace,
            field_selector=field_selector,
            limit=20
        )
        
        if not events.items:
            return True, "No events found"
        
        event_list = []
        for e in sorted(events.items, key=lambda x: x.last_timestamp or datetime.min, reverse=True)[:10]:
            event_list.append(f"[{e.type}] {e.reason}: {e.message}")
        
        return True, "\n".join(event_list)
    except Exception as e:
        return False, f"Failed: {e}"

def query_prometheus(query: str, prometheus_url: str = None) -> Tuple[bool, str]:
    """Execute PromQL query."""
    import requests
    
    url = prometheus_url or os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
    
    try:
        response = requests.get(
            f"{url}/api/v1/query",
            params={"query": query},
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success":
                results = data.get("data", {}).get("result", [])
                if not results:
                    return True, "No data returned"
                
                output = []
                for r in results[:10]:
                    metric = r.get("metric", {})
                    value = r.get("value", [None, None])[1]
                    output.append(f"{metric}: {value}")
                
                return True, "\n".join(output)
        
        return False, f"Query failed: {response.text}"
    except Exception as e:
        return False, f"Failed: {e}"

def check_node_health() -> Tuple[bool, str]:
    """Check health of all nodes."""
    if not k8s_core:
        return False, "K8s not connected"
    
    try:
        nodes = k8s_core.list_node()
        
        node_statuses = []
        for node in nodes.items:
            name = node.metadata.name
            conditions = {c.type: c.status for c in node.status.conditions}
            
            ready = conditions.get("Ready", "Unknown")
            disk = conditions.get("DiskPressure", "Unknown")
            memory = conditions.get("MemoryPressure", "Unknown")
            
            status = "✅" if ready == "True" else "❌"
            node_statuses.append(f"{status} {name}: Ready={ready}, Disk={disk}, Memory={memory}")
        
        return True, "\n".join(node_statuses)
    except Exception as e:
        return False, f"Failed: {e}"

# =============================================================================
# MEDIUM RISK ACTIONS (Confidence-gated)
# =============================================================================

def cordon_node(node_name: str) -> Tuple[bool, str]:
    """Mark node as unschedulable."""
    if not k8s_core:
        return False, "K8s not connected"
    
    try:
        body = {"spec": {"unschedulable": True}}
        k8s_core.patch_node(node_name, body)
        return True, f"Node {node_name} cordoned (unschedulable)"
    except Exception as e:
        return False, f"Failed: {e}"

def uncordon_node(node_name: str) -> Tuple[bool, str]:
    """Mark node as schedulable."""
    if not k8s_core:
        return False, "K8s not connected"
    
    try:
        body = {"spec": {"unschedulable": False}}
        k8s_core.patch_node(node_name, body)
        return True, f"Node {node_name} uncordoned (schedulable)"
    except Exception as e:
        return False, f"Failed: {e}"

def delete_pod(namespace: str, pod_name: str, force: bool = False) -> Tuple[bool, str]:
    """Delete a pod (force delete if stuck)."""
    if not k8s_core:
        return False, "K8s not connected"
    
    try:
        from kubernetes.client import V1DeleteOptions
        
        if force:
            body = V1DeleteOptions(grace_period_seconds=0)
        else:
            body = V1DeleteOptions()
        
        k8s_core.delete_namespaced_pod(pod_name, namespace, body=body)
        return True, f"Pod {pod_name} deleted" + (" (force)" if force else "")
    except Exception as e:
        return False, f"Failed: {e}"

def update_hpa(namespace: str, hpa_name: str, min_replicas: int = None, 
               max_replicas: int = None) -> Tuple[bool, str]:
    """Update HorizontalPodAutoscaler limits."""
    try:
        from kubernetes import client
        autoscaling = client.AutoscalingV2Api()
        
        hpa = autoscaling.read_namespaced_horizontal_pod_autoscaler(hpa_name, namespace)
        
        if min_replicas:
            hpa.spec.min_replicas = min_replicas
        if max_replicas:
            hpa.spec.max_replicas = max_replicas
        
        autoscaling.patch_namespaced_horizontal_pod_autoscaler(hpa_name, namespace, hpa)
        
        return True, f"HPA {hpa_name} updated: min={min_replicas or 'unchanged'}, max={max_replicas or 'unchanged'}"
    except Exception as e:
        return False, f"Failed: {e}"

def patch_resource_limits(namespace: str, deployment: str, 
                          cpu_limit: str = None, memory_limit: str = None,
                          cpu_request: str = None, memory_request: str = None) -> Tuple[bool, str]:
    """Update resource limits/requests for a deployment."""
    if not k8s_apps:
        return False, "K8s not connected"
    
    try:
        resources = {}
        if cpu_limit or memory_limit:
            resources["limits"] = {}
            if cpu_limit:
                resources["limits"]["cpu"] = cpu_limit
            if memory_limit:
                resources["limits"]["memory"] = memory_limit
        
        if cpu_request or memory_request:
            resources["requests"] = {}
            if cpu_request:
                resources["requests"]["cpu"] = cpu_request
            if memory_request:
                resources["requests"]["memory"] = memory_request
        
        if not resources:
            return False, "No resource changes specified"
        
        body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "name": deployment.replace("-deployment", ""),
                            "resources": resources
                        }]
                    }
                }
            }
        }
        
        k8s_apps.patch_namespaced_deployment(deployment, namespace, body)
        return True, f"Resources updated for {deployment}: {resources}"
    except Exception as e:
        return False, f"Failed: {e}"

# =============================================================================
# HIGH RISK ACTIONS (Always require approval)
# =============================================================================

def drain_node(node_name: str, delete_local_data: bool = False, 
               ignore_daemonsets: bool = True) -> Tuple[bool, str]:
    """Drain all pods from a node."""
    if not k8s_core:
        return False, "K8s not connected"
    
    try:
        # First cordon the node
        success, msg = cordon_node(node_name)
        if not success:
            return False, f"Failed to cordon: {msg}"
        
        # Get all pods on the node
        pods = k8s_core.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={node_name}"
        )
        
        evicted = 0
        skipped = 0
        
        for pod in pods.items:
            # Skip daemonset pods if configured
            if ignore_daemonsets:
                for ref in (pod.metadata.owner_references or []):
                    if ref.kind == "DaemonSet":
                        skipped += 1
                        continue
            
            # Skip system pods
            if pod.metadata.namespace == "kube-system":
                skipped += 1
                continue
            
            try:
                k8s_core.delete_namespaced_pod(
                    pod.metadata.name, 
                    pod.metadata.namespace,
                    grace_period_seconds=30
                )
                evicted += 1
            except:
                pass
        
        return True, f"Node {node_name} drained: {evicted} pods evicted, {skipped} skipped"
    except Exception as e:
        return False, f"Failed: {e}"

def delete_deployment(namespace: str, deployment: str) -> Tuple[bool, str]:
    """Delete a deployment entirely."""
    if not k8s_apps:
        return False, "K8s not connected"
    
    try:
        k8s_apps.delete_namespaced_deployment(deployment, namespace)
        return True, f"Deployment {deployment} deleted from {namespace}"
    except Exception as e:
        return False, f"Failed: {e}"

def apply_manifest(manifest_yaml: str, namespace: str = "default") -> Tuple[bool, str]:
    """Apply a YAML manifest to the cluster."""
    if not k8s_core:
        return False, "K8s not connected"
    
    try:
        from kubernetes import utils
        from kubernetes.client import ApiClient
        
        # Parse YAML
        docs = list(yaml.safe_load_all(manifest_yaml))
        
        applied = []
        for doc in docs:
            if not doc:
                continue
            
            kind = doc.get("kind", "Unknown")
            name = doc.get("metadata", {}).get("name", "unknown")
            
            # Use dynamic client
            api_client = ApiClient()
            utils.create_from_dict(api_client, doc, namespace=namespace)
            applied.append(f"{kind}/{name}")
        
        return True, f"Applied: {', '.join(applied)}"
    except Exception as e:
        return False, f"Failed: {e}"

def exec_in_pod(namespace: str, pod_name: str, command: str, 
                container: str = None) -> Tuple[bool, str]:
    """Execute a command inside a pod."""
    if not k8s_core:
        return False, "K8s not connected"
    
    try:
        from kubernetes.stream import stream
        
        exec_command = ['/bin/sh', '-c', command]
        
        resp = stream(
            k8s_core.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container=container,
            command=exec_command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False
        )
        
        return True, f"Output:\n{resp}"
    except Exception as e:
        return False, f"Failed: {e}"

# =============================================================================
# ACTION REGISTRY
# =============================================================================

ACTIONS = {
    # Safe (auto-approve)
    "get_deployment_status": {"func": get_deployment_status, "risk": "safe"},
    "get_pod_events": {"func": get_pod_events, "risk": "safe"},
    "query_prometheus": {"func": query_prometheus, "risk": "safe"},
    "check_node_health": {"func": check_node_health, "risk": "safe"},
    
    # Medium risk (confidence-gated)
    "cordon_node": {"func": cordon_node, "risk": "medium"},
    "uncordon_node": {"func": uncordon_node, "risk": "medium"},
    "delete_pod": {"func": delete_pod, "risk": "medium"},
    "update_hpa": {"func": update_hpa, "risk": "medium"},
    "patch_resource_limits": {"func": patch_resource_limits, "risk": "medium"},
    
    # High risk (always approval)
    "drain_node": {"func": drain_node, "risk": "high"},
    "delete_deployment": {"func": delete_deployment, "risk": "high"},
    "apply_manifest": {"func": apply_manifest, "risk": "high"},
    "exec_in_pod": {"func": exec_in_pod, "risk": "high"},
}

def get_action_risk(action_name: str) -> str:
    """Get risk level for an action."""
    return ACTIONS.get(action_name, {}).get("risk", "high")
