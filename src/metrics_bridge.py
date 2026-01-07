import requests
import time
import json
import datetime
import sys

# Prometheus URL on DigitalOcean droplet
PROMETHEUS_URL = "http://139.59.77.78:30850"

try:
    from kubernetes import client, config
    config.load_kube_config()
    k8s_core_v1 = client.CoreV1Api()
    print("✅ ACQUIRED KUBERNETES CONNECTION (Metrics Bridge)")
except Exception as e:
    k8s_core_v1 = None
    print(f"⚠️ K8s Connection Failed: {e}")

def get_cpu_usage():
    """Queries Prometheus for Nginx CPU usage."""
    # PromQL: Average CPU rate for 'nginx' pods in 'app' namespace over 1m
    query = 'avg(rate(container_cpu_usage_seconds_total{namespace="ai-sre", container="nginx"}[1m])) * 100'
    try:
        response = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={'query': query})
        data = response.json()
        
        if data['status'] == 'success' and data['data']['result']:
            # Extract value
            val = float(data['data']['result'][0]['value'][1])
            # Limit is 50m (0.05). To get % of limit: val / 0.05 * 100 = val * 2000
            return round(val * 2000, 2)
        return 0.0
    except Exception as e:
        # print(f"Error fetching metrics: {e}", file=sys.stderr)
        return 0.0

def check_pod_health(namespace="app"):
    """Checks for non-running pods (CrashLoopBackOff, OOMKilled, etc)."""
    if not k8s_core_v1:
        return None

    try:
        pods = k8s_core_v1.list_namespaced_pod(namespace)
        for pod in pods.items:
            # Check for container statuses
            if pod.status.container_statuses:
                for status in pod.status.container_statuses:
                    # Check if waiting (CrashLoop) or Terminated (OOM)
                    if status.state.waiting:
                        reason = status.state.waiting.reason
                        if reason in ["CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"]:
                            return {
                                "pod": pod.metadata.name,
                                "issue": reason,
                                "message": status.state.waiting.message
                            }
                    if status.state.terminated:
                        reason = status.state.terminated.reason
                        if reason == "OOMKilled":
                            return {
                                "pod": pod.metadata.name,
                                "issue": reason,
                                "exit_code": status.state.terminated.exit_code
                            }
            
            # Check Phase
            if pod.status.phase in ["Failed", "Unknown"]:
                 return {"pod": pod.metadata.name, "issue": f"PodPhase-{pod.status.phase}"}
                 
        return None
    except Exception as e:
        print(f"Error checking pod health: {e}")
        return None

def main():
    print("Starting Metrics Bridge (Prometheus -> Agent)...")
    
    # Log file shared with Agent
    log_file = "server.log"
    
    while True:
        cpu = get_cpu_usage()
        
        # Simulate Disk for now (or could query it too)
        disk = 35  # Static for demo
        
        timestamp = datetime.datetime.utcnow().isoformat() + "Z"
        
        # NO SIMULATION: We rely on real load testing now.
        
        log_entry = {
            "timestamp": timestamp,
            "level": "INFO", 
            "service": "nginx-k8s",
            "metrics": {
                "source": "prometheus",
                "cpu_usage": cpu
            },
            "cpu": cpu,   # Agent expects this top-level
            "disk": disk, # Agent expects this top-level
            "message": f"Metrics Update | CPU: {cpu}%"
        }
        
        # Check K8s Status
        health_issue = check_pod_health("ai-sre")
        if health_issue:
            log_entry['level'] = "CRITICAL"
            log_entry['message'] = f"POD FAILURE: {health_issue['pod']} is {health_issue['issue']}"
            log_entry['k8s_status'] = health_issue # Pass full details
            # Override CPU-based logic if real failure found
            cpu = 99.9 # Force high severity
            log_entry['cpu'] = cpu 

        # CPU > 50 is treated as WARNING/CRITICAL by this bridge to help the Agent context
        if cpu > 50 and log_entry['level'] != "CRITICAL":
            log_entry['level'] = "ERROR"
            log_entry['message'] = f"HIGH CPU LOAD: {cpu}%"

        # Write to server.log so Agent can tail it
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
            
        print(f"Logged: CPU={cpu}%")
        time.sleep(5)

if __name__ == "__main__":
    main()
