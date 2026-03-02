# NDIF CLI

Command-line interface for managing the National Deep Inference Fabric (NDIF) server.

## Overview

The NDIF CLI (`ndif`) manages the lifecycle of NDIF services and model deployments. It handles:

- **Service management**: Start, stop, and monitor NDIF services (API, Ray, broker, object store)
- **Model deployment**: Deploy, evict, and monitor ML models on the cluster
- **Observability**: View logs, queue status, and cluster state

## Quickstart

### 1. Start NDIF

```bash
# Start all services (API, Ray, broker, object store)
ndif start

# Verify everything is running
ndif info
```

### 2. Deploy a model

```bash
# Deploy a model
ndif deploy gpt2

# Check deployment status
ndif status
```

### 3. Monitor the cluster

```bash
# View cluster status
ndif status

# Watch status in real-time
ndif status --watch

# View queue state
ndif queue
```

### 4. Stop NDIF

```bash
# Stop all services
ndif stop
```

## Command Reference

Use `ndif <command> --help` for detailed options.

| Command | Description |
|---------|-------------|
| `ndif start` | Start NDIF services |
| `ndif stop` | Stop NDIF services |
| `ndif restart` | Restart a model deployment |
| `ndif deploy` | Deploy one or more models |
| `ndif evict` | Remove model deployments |
| `ndif status` | View cluster and deployment status |
| `ndif queue` | View queue and processor status |
| `ndif logs` | View service logs |
| `ndif kill` | Cancel a specific request |
| `ndif info` | Show session and configuration |
| `ndif env` | Show cluster environment info |

## Common Workflows

### Starting Services

```bash
# Start all services (default)
ndif start

# Start specific service only
ndif start api
ndif start ray
ndif start broker

# Start with verbose output (foreground mode)
ndif start --verbose

# Start as a Ray worker node (connects to existing head)
ndif start --worker --ray-address ray://head-node:10001
```

### Deploying Models

```bash
# Deploy a single model
ndif deploy gpt2

# Deploy multiple models
ndif deploy gpt2 meta-llama/Llama-3.1-8b

# Deploy with specific revision
ndif deploy meta-llama/Llama-2-7b-hf --revision main

# Deploy as dedicated (won't be evicted)
ndif deploy gpt2 --dedicated

# Deploy from config file
ndif deploy -f models.yaml
```

**Config file format** (`models.yaml`):
```yaml
models:
  - gpt2                           # Simple form
  - checkpoint: meta-llama/Llama-3.1-8b
    revision: main
    dedicated: true                # Full form with options
```

### Evicting Models

```bash
# Evict a specific model
ndif evict gpt2

# Evict multiple models
ndif evict gpt2 meta-llama/Llama-3.1-8b

# Evict all HOT deployments
ndif evict --all

# Flush WARM cache (CPU-cached models)
ndif evict --flush-cache
```

### Monitoring

```bash
# Quick status overview
ndif status

# Include COLD (downloaded but not loaded) models
ndif status --show-cold

# Detailed cluster state
ndif status --verbose

# JSON output (for scripting)
ndif status --json-output

# Real-time monitoring
ndif status --watch
ndif queue --watch
```

The Ray dashboard provides detailed cluster monitoring including actors, resources, and logs:
- Default URL: `http://localhost:8265`
- Configure with: `NDIF_RAY_DASHBOARD_PORT`

### Viewing Logs

```bash
# View API logs
ndif logs api

# View Ray logs
ndif logs ray

# Follow logs in real-time
ndif logs api --follow

# Show more lines
ndif logs api -n 500
```

### Canceling Requests

```bash
# Cancel a request by ID
ndif kill abc123
```

## Configuration

The CLI uses environment variables for default configuration. CLI arguments override environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `NDIF_BROKER_URL` | `redis://localhost:6379` | Redis broker URL |
| `NDIF_OBJECT_STORE_URL` | `http://localhost:9000` | MinIO object store URL |
| `NDIF_API_URL` | `http://localhost:5001` | API service URL |
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | Ray client address |
| `NDIF_RAY_HEAD_PORT` | `6380` | Ray head node port |
| `NDIF_RAY_DASHBOARD_PORT` | `8265` | Ray dashboard port |
| `NDIF_SESSION_ROOT` | `~/.ndif` | Session data directory |

View current configuration:
```bash
ndif info --env
```

## Session Management

NDIF maintains session state in `~/.ndif/` (or `$NDIF_SESSION_ROOT`). Each session tracks:
- Service states (running/stopped)
- Port configurations
- Service PIDs

View session info:
```bash
ndif info
ndif info --json-output
```

## Deployment Levels

Models can be in one of three states:

| Level | Description | Resources Used |
|-------|-------------|----------------|
| **HOT** | Loaded on GPU, ready for inference | GPU memory |
| **WARM** | Cached in CPU memory | CPU memory |
| **COLD** | Downloaded to disk only | Disk space |

View deployment levels:
```bash
ndif status              # Shows HOT and WARM
ndif status --show-cold  # Also shows COLD
```

## Troubleshooting

### Services won't start

```bash
# Check what's already running on ports
ndif info

# Force stop all services
ndif stop --force

# Start fresh
ndif start
```

### Can't connect to Ray

```bash
# Verify Ray is running
ndif info

# Check Ray logs
ndif logs ray

# Check Ray dashboard for cluster state
# Default: http://localhost:8265

# Restart Ray
ndif stop ray && ndif start ray
```

### Model deployment fails

```bash
# Check cluster status
ndif status --verbose

# Check Ray dashboard for actor/deployment errors
# Default: http://localhost:8265

# Check API logs
ndif logs api --follow

# Check Ray logs for controller errors
ndif logs ray
```

### Check service connectivity

```bash
# Quick connectivity check
ndif info

# Shows checkmarks for reachable services:
#   ✓ Broker reachable at redis://localhost:6379
#   ✓ Object store reachable at http://localhost:9000
#   ✓ API reachable at http://localhost:5001
#   ✓ Ray reachable at ray://localhost:10001
```

## Testing

Run CLI integration tests:

```bash
# Requires running NDIF services
pytest cli/tests/test_cli.py
```
