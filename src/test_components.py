#!/usr/bin/env python3
"""
Test script to verify all AI SRE Agent components WITHOUT using Gemini API.
Tests: Prometheus, K8s API, Log Fetching, Pod Health, Email (optional).
"""

import os
import sys
import json
from dotenv import load_dotenv

load_dotenv()

print("=" * 60)
print("🧪 AI SRE AGENT - COMPONENT TEST")
print("=" * 60)

# --- TEST 1: Kubernetes Connection ---
print("\n[TEST 1] Kubernetes Connection...")
try:
    from kubernetes import client, config
    config.load_kube_config()
    k8s_apps_v1 = client.AppsV1Api()
    k8s_core_v1 = client.CoreV1Api()
    
    nodes = k8s_core_v1.list_node()
    print(f"  ✅ Connected! Found {len(nodes.items)} nodes:")
    for node in nodes.items:
        print(f"     - {node.metadata.name}")
except Exception as e:
    print(f"  ❌ FAILED: {e}")
    sys.exit(1)

# --- TEST 2: Prometheus Connection ---
print("\n[TEST 2] Prometheus Connection...")
try:
    import requests
    PROMETHEUS_URL = "http://139.59.77.78:30850"
    
    response = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={'query': 'up'}, timeout=5)
    data = response.json()
    
    if data['status'] == 'success':
        result_count = len(data['data']['result'])
        print(f"  ✅ Connected! Found {result_count} active targets.")
    else:
        print(f"  ⚠️ Prometheus returned: {data}")
except Exception as e:
    print(f"  ❌ FAILED: {e}")

# --- TEST 3: CPU Metrics for ai-sre namespace ---
print("\n[TEST 3] CPU Metrics (ai-sre namespace)...")
try:
    query = 'avg(rate(container_cpu_usage_seconds_total{namespace="ai-sre", container="nginx"}[1m])) * 100'
    response = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={'query': query}, timeout=5)
    data = response.json()
    
    if data['status'] == 'success' and data['data']['result']:
        val = float(data['data']['result'][0]['value'][1])
        # Normalize to % of 50m limit
        cpu_pct = round(val * 2000, 2)
        print(f"  ✅ CPU Usage: {cpu_pct}% (of 50m limit)")
    else:
        print(f"  ⚠️ No data yet (pod may be idle). Result: {data['data']['result']}")
except Exception as e:
    print(f"  ❌ FAILED: {e}")

# --- TEST 4: List Pods in ai-sre namespace ---
print("\n[TEST 4] Pods in 'ai-sre' namespace...")
try:
    pods = k8s_core_v1.list_namespaced_pod("ai-sre")
    
    if pods.items:
        print(f"  ✅ Found {len(pods.items)} pod(s):")
        for pod in pods.items:
            status = pod.status.phase
            print(f"     - {pod.metadata.name} [{status}]")
    else:
        print("  ⚠️ No pods found in ai-sre namespace.")
except Exception as e:
    print(f"  ❌ FAILED: {e}")

# --- TEST 5: Fetch Pod Logs ---
print("\n[TEST 5] Fetch Pod Logs...")
try:
    pods = k8s_core_v1.list_namespaced_pod("ai-sre")
    if pods.items:
        pod_name = pods.items[0].metadata.name
        logs = k8s_core_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace="ai-sre",
            tail_lines=5
        )
        print(f"  ✅ Last 5 lines from {pod_name}:")
        for line in logs.strip().split('\n'):
            print(f"     {line[:80]}")
    else:
        print("  ⚠️ No pods to fetch logs from.")
except Exception as e:
    print(f"  ❌ FAILED: {e}")

# --- TEST 6: Pod Health Check ---
print("\n[TEST 6] Pod Health Check (OOM/CrashLoop detection)...")
try:
    issue_found = None
    pods = k8s_core_v1.list_namespaced_pod("ai-sre")
    
    for pod in pods.items:
        if pod.status.container_statuses:
            for status in pod.status.container_statuses:
                if status.state.waiting:
                    reason = status.state.waiting.reason
                    if reason in ["CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "OOMKilled"]:
                        issue_found = {"pod": pod.metadata.name, "issue": reason}
                        break
                if status.state.terminated:
                    reason = status.state.terminated.reason
                    if reason == "OOMKilled":
                        issue_found = {"pod": pod.metadata.name, "issue": reason}
                        break
    
    if issue_found:
        print(f"  🚨 ISSUE DETECTED: {issue_found}")
    else:
        print("  ✅ All pods healthy!")
except Exception as e:
    print(f"  ❌ FAILED: {e}")

# --- TEST 7: Scale Deployment (Dry Run) ---
print("\n[TEST 7] Scale Deployment (checking current state)...")
try:
    scale = k8s_apps_v1.read_namespaced_deployment_scale(
        name="ai-sre-target",
        namespace="ai-sre"
    )
    print(f"  ✅ Current replicas: {scale.spec.replicas}")
    print(f"     (Agent would scale to 2 on alert)")
except Exception as e:
    print(f"  ❌ FAILED: {e}")

# --- TEST 8: Email Configuration ---
print("\n[TEST 8] Email Configuration...")
gmail_user = os.environ.get('GMAIL_USER')
gmail_password = os.environ.get('GMAIL_APP_PASSWORD')

if gmail_user and gmail_password:
    print(f"  ✅ Gmail configured: {gmail_user}")
    print(f"     Password: {'*' * len(gmail_password)}")
else:
    print("  ⚠️ Gmail not configured. Set GMAIL_USER and GMAIL_APP_PASSWORD in .env")

# --- TEST 9: Gemini API Key ---
print("\n[TEST 9] Gemini API Key...")
gemini_key = os.environ.get('GEMINI_API_KEY')
if gemini_key:
    print(f"  ✅ Gemini API Key found: {gemini_key[:8]}...")
else:
    print("  ⚠️ Gemini API Key not set (AI remediation disabled).")

print("\n" + "=" * 60)
print("🏁 TEST COMPLETE")
print("=" * 60)
