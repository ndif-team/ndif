"""Profile command for NDIF - collect lifecycle stats for a request."""

import os
import click
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

try:
    from influxdb_client import InfluxDBClient, QueryApi
except ImportError:
    InfluxDBClient = None
    QueryApi = None

try:
    import requests
except ImportError:
    requests = None


def get_influxdb_client() -> Optional[QueryApi]:
    """Get InfluxDB query client."""
    if InfluxDBClient is None:
        return None
    
    address = os.getenv("INFLUXDB_ADDRESS")
    token = os.getenv("INFLUXDB_ADMIN_TOKEN")
    
    if not address or not token:
        return None
    
    client = InfluxDBClient(url=address, token=token)
    return client.query_api()


def query_influxdb_metrics(
    query_api: QueryApi, request_id: str, bucket: str, org: str, range_hours: int
) -> Tuple[Dict, List[str]]:
    """Query InfluxDB for all metrics related to a request."""
    metrics = {
        "execution_time": None,
        "status_times": [],
        "gpu_mem": None,
        "model_load_time": None,
        "response_size": None,
        "network_data": None,
        "timeline": [],
    }
    errors: List[str] = []
    
    # Common filter for request_id
    request_filter = f'r["request_id"] == "{request_id}"'
    
    range_clause = f"-{range_hours}h"

    # Query execution time
    try:
        query = f'''
        from(bucket: "{bucket}")
          |> range(start: {range_clause})
          |> filter(fn: (r) => r["_measurement"] == "request_execution_time")
          |> filter(fn: (r) => {request_filter})
          |> last()
        '''
        result = query_api.query(query, org=org)
        for table in result:
            for record in table.records:
                metrics["execution_time"] = {
                    "value": record.get_value(),
                    "time": record.get_time(),
                    "model_key": record.values.get("model_key"),
                    "api_key": record.values.get("api_key"),
                }
    except Exception as e:
        errors.append(f"Ingress execution_time query failed: {e}")
    
    # Query status transition times
    try:
        query = f'''
        from(bucket: "{bucket}")
          |> range(start: {range_clause})
          |> filter(fn: (r) => r["_measurement"] == "request_status_time")
          |> filter(fn: (r) => {request_filter})
          |> sort(columns: ["_time"])
        '''
        result = query_api.query(query, org=org)
        for table in result:
            for record in table.records:
                metrics["status_times"].append({
                    "status": record.values.get("status"),
                    "duration": record.get_value(),
                    "time": record.get_time(),
                })
                metrics["timeline"].append({
                    "component": "api",
                    "event": f"status:{record.values.get('status')}",
                    "value": record.get_value(),
                    "time": record.get_time(),
                })
    except Exception as e:
        errors.append(f"Ingress status_times query failed: {e}")
    
    # Query GPU memory
    try:
        query = f'''
        from(bucket: "{bucket}")
          |> range(start: {range_clause})
          |> filter(fn: (r) => r["_measurement"] == "gpu_mem")
          |> filter(fn: (r) => {request_filter})
          |> last()
        '''
        result = query_api.query(query, org=org)
        for table in result:
            for record in table.records:
                metrics["gpu_mem"] = {
                    "value": record.get_value(),
                    "time": record.get_time(),
                    "unit": "bytes",
                }
                metrics["timeline"].append({
                    "component": "ray",
                    "event": "gpu_mem",
                    "value": record.get_value(),
                    "time": record.get_time(),
                })
    except Exception as e:
        errors.append(f"Ingress gpu_mem query failed: {e}")
    
    # Query model load time
    try:
        query = f'''
        from(bucket: "{bucket}")
          |> range(start: {range_clause})
          |> filter(fn: (r) => r["_measurement"] == "model_load_time")
          |> filter(fn: (r) => {request_filter})
          |> last()
        '''
        result = query_api.query(query, org=org)
        for table in result:
            for record in table.records:
                metrics["model_load_time"] = {
                    "value": record.get_value(),
                    "time": record.get_time(),
                    "source": record.values.get("source"),  # disk or cache
                    "model_key": record.values.get("model_key"),
                }
                metrics["timeline"].append({
                    "component": "ray",
                    "event": f"model_load:{record.values.get('source')}",
                    "value": record.get_value(),
                    "time": record.get_time(),
                })
    except Exception as e:
        errors.append(f"Ingress model_load_time query failed: {e}")
    
    # Query response size
    try:
        query = f'''
        from(bucket: "{bucket}")
          |> range(start: {range_clause})
          |> filter(fn: (r) => r["_measurement"] == "request_response_size")
          |> filter(fn: (r) => {request_filter})
          |> last()
        '''
        result = query_api.query(query, org=org)
        for table in result:
            for record in table.records:
                metrics["response_size"] = {
                    "value": record.get_value(),
                    "time": record.get_time(),
                    "unit": "bytes",
                }
                metrics["timeline"].append({
                    "component": "api",
                    "event": "response_size",
                    "value": record.get_value(),
                    "time": record.get_time(),
                })
    except Exception as e:
        errors.append(f"Ingress response_size query failed: {e}")
    
    # Query network data
    try:
        query = f'''
        from(bucket: "{bucket}")
          |> range(start: {range_clause})
          |> filter(fn: (r) => r["_measurement"] == "network_data")
          |> filter(fn: (r) => {request_filter})
          |> last()
        '''
        result = query_api.query(query, org=org)
        for table in result:
            for record in table.records:
                metrics["network_data"] = {
                    "value": record.get_value(),
                    "time": record.get_time(),
                }
    except Exception as e:
        errors.append(f"Ingress network_data query failed: {e}")

    metrics["timeline"] = sorted(
        metrics["timeline"],
        key=lambda item: item.get("time") or datetime.min,
    )

    return metrics, errors


def query_loki_logs(
    request_id: str, loki_url: Optional[str], range_hours: int
) -> Tuple[List[Dict], Optional[str]]:
    """Query Loki for logs related to a request."""
    if requests is None or not loki_url:
        return [], None
    
    # Remove /loki/api/v1/push if present, add query endpoint
    base_url = loki_url.replace("/loki/api/v1/push", "")
    query_url = f"{base_url}/loki/api/v1/query_range"
    
    # Query logs from last N hours
    end_time = datetime.now()
    start_time = end_time - timedelta(hours=range_hours)
    
    params = {
        "query": f'{{job=~".*"}} |= "{request_id}"',
        "start": int(start_time.timestamp() * 1e9),  # nanoseconds
        "end": int(end_time.timestamp() * 1e9),
        "limit": 1000,
    }
    
    try:
        response = requests.get(query_url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            logs = []
            if "data" in data and "result" in data["data"]:
                for stream in data["data"]["result"]:
                    for entry in stream.get("values", []):
                        logs.append({
                            "timestamp": entry[0],
                            "message": entry[1],
                            "labels": stream.get("stream", {}),
                        })
            return sorted(logs, key=lambda x: x["timestamp"]), None
        return [], f"Loki HTTP {response.status_code}: {response.text[:200]}"
    except Exception as e:
        return [], f"Loki query failed: {e}"


def format_bytes(bytes_value: Optional[float]) -> str:
    """Format bytes to human-readable format."""
    if bytes_value is None:
        return "N/A"
    
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_value < 1024.0:
            return f"{bytes_value:.2f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.2f} PB"


def format_duration(seconds: Optional[float]) -> str:
    """Format duration to human-readable format."""
    if seconds is None:
        return "N/A"
    
    if seconds < 1:
        return f"{seconds * 1000:.2f} ms"
    elif seconds < 60:
        return f"{seconds:.2f} s"
    else:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}m {secs:.2f}s"


def format_timestamp(timestamp) -> str:
    """Format timestamp to readable format."""
    if timestamp is None:
        return "N/A"
    
    if isinstance(timestamp, datetime):
        return timestamp.strftime("%Y-%m-%d %H:%M:%S")
    elif hasattr(timestamp, 'strftime'):
        return timestamp.strftime("%Y-%m-%d %H:%M:%S")
    else:
        return str(timestamp)


def fetch_response_status(
    request_id: str, api_url: Optional[str]
) -> Tuple[Optional[Dict], Optional[str]]:
    """Fetch response status from the API if available."""
    if requests is None or not api_url:
        return None, None
    try:
        resp = requests.get(f"{api_url}/response/{request_id}", timeout=10)
        if resp.status_code == 200:
            return resp.json(), None
        return None, f"API HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return None, f"API status query failed: {e}"
    """Format timestamp to readable format."""
    if timestamp is None:
        return "N/A"
    
    if isinstance(timestamp, datetime):
        return timestamp.strftime("%Y-%m-%d %H:%M:%S")
    elif hasattr(timestamp, 'strftime'):
        return timestamp.strftime("%Y-%m-%d %H:%M:%S")
    else:
        return str(timestamp)


@click.command()
@click.argument("request_id", type=str)
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.option("--include-logs", is_flag=True, help="Include log entries")
@click.option("--influxdb-address", envvar="INFLUXDB_ADDRESS", help="InfluxDB address")
@click.option("--influxdb-token", envvar="INFLUXDB_ADMIN_TOKEN", help="InfluxDB token")
@click.option("--influxdb-org", envvar="INFLUXDB_ORG", default="ndif", help="InfluxDB organization")
@click.option("--influxdb-bucket", envvar="INFLUXDB_BUCKET", default="ndif", help="InfluxDB bucket")
@click.option("--api-url", envvar="API_URL", help="NDIF API base URL (optional)")
@click.option("--loki-url", envvar="LOKI_URL", help="Loki URL (optional)")
@click.option("--range-hours", default=24, show_default=True, help="Lookback window in hours")
def profile(
    request_id: str,
    json_output: bool,
    include_logs: bool,
    influxdb_address: Optional[str],
    influxdb_token: Optional[str],
    influxdb_org: str,
    influxdb_bucket: str,
    api_url: Optional[str],
    loki_url: Optional[str],
    range_hours: int,
):
    """Collect lifecycle stats for a request ID.
    
    Queries InfluxDB for metrics and optionally Loki for logs related to the request.
    
    Examples:
        ndif profile abc123-request-id
        ndif profile abc123 --include-logs
        ndif profile abc123 --json
    """
    # Set environment variables if provided via CLI
    if influxdb_address:
        os.environ["INFLUXDB_ADDRESS"] = influxdb_address
    if influxdb_token:
        os.environ["INFLUXDB_ADMIN_TOKEN"] = influxdb_token
    if loki_url:
        os.environ["LOKI_URL"] = loki_url
    if api_url:
        os.environ["API_URL"] = api_url
    
    # Get InfluxDB client
    query_api = get_influxdb_client()
    
    if query_api is None:
        click.echo("Error: InfluxDB not configured. Set INFLUXDB_ADDRESS and INFLUXDB_ADMIN_TOKEN.", err=True)
        return
    
    # Query metrics
    click.echo(f"Querying metrics for request: {request_id}...", err=True)
    metrics, metric_errors = query_influxdb_metrics(
        query_api, request_id, influxdb_bucket, influxdb_org, range_hours
    )
    
    # Query logs if requested
    logs: List[Dict] = []
    log_error = None
    if include_logs:
        click.echo("Querying logs...", err=True)
        logs, log_error = query_loki_logs(request_id, loki_url, range_hours)

    response_status, response_error = fetch_response_status(request_id, api_url)
    
    # Output results
    if json_output:
        import json
        output = {
            "request_id": request_id,
            "metrics": metrics,
            "errors": {
                "metrics": metric_errors,
                "logs": log_error,
                "api": response_error,
            },
            "response": response_status,
        }
        if include_logs:
            output["logs"] = logs
        click.echo(json.dumps(output, indent=2, default=str))
    else:
        # Pretty print
        click.echo(f"\n{'='*60}")
        click.echo(f"Request Lifecycle Profile: {request_id}")
        click.echo(f"{'='*60}\n")
        
        # Execution metrics
        click.echo("📊 Execution Metrics")
        click.echo("-" * 60)
        
        if metrics["execution_time"]:
            et = metrics["execution_time"]
            click.echo(f"  Execution Time: {format_duration(et['value'])}")
            click.echo(f"  Model Key: {et.get('model_key', 'N/A')}")
            click.echo(f"  Timestamp: {format_timestamp(et.get('time'))}")
        else:
            click.echo("  Execution Time: Not found")
        
        click.echo()
        
        # Status transitions
        if metrics["status_times"]:
            click.echo("🔄 Status Transitions")
            click.echo("-" * 60)
            total_time = 0.0
            for status_info in metrics["status_times"]:
                status = status_info["status"]
                duration = status_info["duration"]
                total_time += duration
                click.echo(f"  {status:20s}: {format_duration(duration)}")
            click.echo(f"  {'Total':20s}: {format_duration(total_time)}")
        else:
            click.echo("🔄 Status Transitions: Not found")
        
        click.echo()
        
        # Resource metrics
        click.echo("💾 Resource Usage")
        click.echo("-" * 60)
        
        if metrics["gpu_mem"]:
            gpu = metrics["gpu_mem"]
            click.echo(f"  GPU Memory: {format_bytes(gpu['value'])}")
        else:
            click.echo("  GPU Memory: Not found")
        
        if metrics["response_size"]:
            rs = metrics["response_size"]
            click.echo(f"  Response Size: {format_bytes(rs['value'])}")
        else:
            click.echo("  Response Size: Not found")
        
        if metrics["model_load_time"]:
            mlt = metrics["model_load_time"]
            click.echo(f"  Model Load Time: {format_duration(mlt['value'])}")
            click.echo(f"  Load Source: {mlt.get('source', 'N/A')}")
        else:
            click.echo("  Model Load Time: Not found")
        
        click.echo()

        # Response status
        click.echo("✅ Response Status")
        click.echo("-" * 60)
        if response_status:
            click.echo(f"  Status: {response_status.get('status', 'N/A')}")
            click.echo(f"  Description: {response_status.get('description', 'N/A')}")
        else:
            click.echo("  Response: Not found (API_URL not set or not reachable)")
        click.echo()

        # Timeline
        if metrics["timeline"]:
            click.echo("🧭 Timeline")
            click.echo("-" * 60)
            for event in metrics["timeline"]:
                click.echo(
                    f"  {format_timestamp(event.get('time'))} "
                    f"[{event.get('component')}] "
                    f"{event.get('event')} = {event.get('value')}"
                )
            click.echo()
        
        # Logs
        if include_logs and logs:
            click.echo("📝 Logs")
            click.echo("-" * 60)
            for log in logs[:50]:  # Limit to 50 logs
                timestamp_ns = int(log["timestamp"])
                timestamp = datetime.fromtimestamp(timestamp_ns / 1e9)
                job = log["labels"].get("job", "unknown")
                click.echo(f"  [{timestamp.strftime('%H:%M:%S')}] [{job}] {log['message'][:100]}")
            
            if len(logs) > 50:
                click.echo(f"\n  ... and {len(logs) - 50} more log entries")
        elif include_logs:
            click.echo("📝 Logs: Not found")
        if metric_errors or log_error or response_error:
            click.echo()
            click.echo("⚠️  Warnings")
            click.echo("-" * 60)
            for err in metric_errors:
                click.echo(f"  {err}")
            if log_error:
                click.echo(f"  {log_error}")
            if response_error:
                click.echo(f"  {response_error}")

        click.echo()
