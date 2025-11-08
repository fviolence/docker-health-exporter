# Docker Health Exporter

Tiny Prometheus exporter that turns Docker container **health** and **runtime state** into clean metrics.  
Works even if containers **don’t define a HEALTHCHECK**.

- **Image:** `fviolence/docker-health-exporter`
- **Docker Hub:** https://hub.docker.com/r/fviolence/docker-health-exporter

---

## Quick start

### Docker (CLI)
```bash
docker run -d \
  --name docker-health-exporter \
  -p 9066:9066 \
  -e PORT=9066 \
  -e SCRAPE_INTERVAL=10 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  fviolence/docker-health-exporter:latest
```
### Docker Compose
```yaml
services:
  docker-health-exporter:
    image: fviolence/docker-health-exporter:latest
    container_name: docker-health-exporter
    restart: unless-stopped
    environment:
      - PORT=9066
      - SCRAPE_INTERVAL=10
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    ports:
      - "9066:9066"
```
`Now visit: http://<host>:9066/metrics`
### Prometheus scrape config
```yaml
scrape_configs:
  - job_name: 'docker-health-exporter'
    static_configs:
      - targets: ['<host-or-ip>:9066']
        labels:
          instance: '<host-descriptive-name>'
```
If Prometheus runs in Docker on the same host, use the host’s LAN IP or place both into the same Docker network and target `docker-health-exporter:9066`.

## Metrics
#### Labels: All metrics include container, image, id, hostname (and status for the one-hot series).
| Metric                                | Extra Labels | Description                                                                                   | Values / Notes                                                                                                                    |
| ------------------------------------- | ------------ | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `docker_container_health`             | —            | Numeric container health (uses Docker healthchecks if present; otherwise treated as healthy). | `1.0` = healthy, `0.5` = starting, `0.0` = unhealthy. **No healthcheck → `1.0`**.                                                 |
| `docker_container_health_status`      | `status`     | One-hot health status series.                                                                 | `status ∈ {healthy, starting, unhealthy, none}`; value is `1` for the current status else `0`. (`none` = no healthcheck defined.) |
| `docker_container_running`            | —            | Container running state (from `State.Running`).                                               | `1` = running, `0` = not running.                                                                                                 |
| `docker_container_restart_count`      | —            | Docker `RestartCount` as a gauge.                                                             | Monotonic per container instance (increments on restarts).                                                                        |
| `docker_container_started_at_seconds` | —            | Start time in Unix seconds (`State.StartedAt`).                                               | `0` if unknown.                                                                                                                   |
Old/container-gone series are removed automatically to avoid stale labels.

## Example Grafana queries
* Unhealthy containers (list):
```promql
docker_container_health_status{status="unhealthy"} == 1
```
* Running vs stopped (per host):
```promql
sum by (hostname) (docker_container_running)
```
* Restart spikes (top 5 / 1h):
```promql
topk(5, increase(docker_container_restart_count[1h]))
```
## Example alerts
```yaml
groups:
- name: docker-health
  rules:
  - alert: ContainerUnhealthy
    expr: docker_container_health_status{status="unhealthy"} == 1
    for: 2m
    labels: { severity: critical }
    annotations:
      summary: "Container unhealthy ({{ $labels.container }})"
      description: "Container {{ $labels.container }} on {{ $labels.hostname }} is unhealthy"

  - alert: ContainerNotRunning
    expr: docker_container_running == 0
    for: 5m
    labels: { severity: warning }
    annotations:
      summary: "Container not running ({{ $labels.container }})"
      description: "Container {{ $labels.container }} on {{ $labels.hostname }} is not running"

  - alert: ContainerRestartSpike
    expr: increase(docker_container_restart_count[30m]) > 3
    for: 1m
    labels: { severity: warning }
    annotations:
      summary: "Container restart spike ({{ $labels.container }})"
      description: "Container {{ $labels.container }} on {{ $labels.hostname }} restarted >3 times in 30m"
```
## Configuration
#### Environment variables:
| Variable          | Default   | Description                                                                                                                                                                          |
| ----------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `PORT`            | `9066`    | HTTP port for `/metrics`.                                                                                                                                                            |
| `BIND_ADDR`       | `0.0.0.0` | Address to bind the HTTP server.                                                                                                                                                     |
| `SCRAPE_INTERVAL` | `10`      | How often (seconds) to poll the Docker Engine.                                                                                                                                       |
| `DOCKER_HOST`     | *(empty)* | Optional Docker endpoint override (e.g. `unix:///var/run/docker.sock`, `tcp://host:2375`). If unset, the exporter auto-probes common sockets then falls back to `docker.from_env()`. |
#### Security notes:
* Mount the Docker socket read-only.
* If using a TCP Docker daemon, secure it with TLS.
## Build locally
```bash
# Build
docker build -t docker-health-exporter:dev .

# Run
docker run --rm -p 9066:9066 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  docker-health-exporter:dev
```
