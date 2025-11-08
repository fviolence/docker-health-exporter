#!/usr/bin/env python3

import os, sys, time, socket
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone

from prometheus_client import start_http_server, Gauge
import docker
from docker.errors import DockerException

BIND_ADDR = os.environ.get("BIND_ADDR", "0.0.0.0")
PORT = int(os.environ.get("PORT", "9066"))
INTERVAL = float(os.environ.get("SCRAPE_INTERVAL", "10"))
ENV_DOCKER_HOST = os.environ.get("DOCKER_HOST", "").strip()

DEFAULT_SOCKET_CANDIDATES: List[str] = [
    "unix:///var/run/docker.sock",
    "unix:///run/user/1000/docker.sock",
]

if os.environ.get("XDG_RUNTIME_DIR"):
    DEFAULT_SOCKET_CANDIDATES.insert(1, f'unix://{os.environ["XDG_RUNTIME_DIR"].rstrip("/")}/docker.sock')
else:
    try:
        uid = os.getuid()
        DEFAULT_SOCKET_CANDIDATES.insert(1, f'unix:///run/user/{uid}/docker.sock')
    except Exception:
        pass

# ---------------- Metrics ----------------
g_health = Gauge(
    "docker_container_health",
    "Numeric health of container (1=healthy, 0.5=starting, 0=unhealthy; 1 when no healthcheck)",
    ["container", "image", "id", "hostname"],
)
g_status = Gauge(
    "docker_container_health_status",
    "Per-container health status (one-hot). status: {healthy, starting, unhealthy, none}",
    ["container", "image", "id", "hostname", "status"],
)
g_running = Gauge(
    "docker_container_running",
    "1 if Docker reports the container State.Running, else 0",
    ["container", "image", "id", "hostname"],
)
g_restart = Gauge(
    "docker_container_restart_count",
    "Docker engine RestartCount for the container (monotonic counter exposed as gauge)",
    ["container", "image", "id", "hostname"],
)
g_started_at = Gauge(
    "docker_container_started_at_seconds",
    "Container start time (unix seconds) from State.StartedAt",
    ["container", "image", "id", "hostname"],
)

# track last labelsets to remove stale series
_last_h: set = set()
_last_s: set = set()
_last_rn: set = set()
_last_rc: set = set()
_last_sa: set = set()

# ---------------- Helpers ----------------
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)

def _canonical_unix_path(url: str) -> str:
    if not url:
        return url
    low = url.lower()
    if low.startswith(("http+docker://", "http+unix://")):
        return "unix:///var/run/docker.sock"
    if low.startswith("npipe://"):
        return "npipe:////./pipe/docker_engine"
    return url

def _file_exists_for_unix(url: str) -> bool:
    if not url.startswith("unix://"):
        return False
    path = url[len("unix://"):]
    return os.path.exists(path)

def create_docker_client() -> Tuple[docker.DockerClient, str]:
    candidates: List[str] = []
    if ENV_DOCKER_HOST:
        candidates.append(_canonical_unix_path(ENV_DOCKER_HOST))
    for c in DEFAULT_SOCKET_CANDIDATES:
        c_norm = _canonical_unix_path(c)
        if c_norm not in candidates:
            candidates.append(c_norm)
    candidates.append("from_env")

    errors: Dict[str, str] = {}
    for base in candidates:
        try:
            if base == "from_env":
                cli = docker.from_env()
                base_used = getattr(cli.api, "base_url", "from_env")
            else:
                if base.startswith("unix://") and not _file_exists_for_unix(base):
                    continue
                cli = docker.DockerClient(base_url=base)
                base_used = base
            _ = cli.version()
            log(f"[info] Connected to Docker via {base_used}")
            return cli, base_used
        except Exception as e:
            errors[base] = str(e)
            continue
    for k, v in errors.items():
        log(f"[error] Tried {k}: {v}")
    raise DockerException("Unable to connect to Docker daemon via any known method.")

def status_to_num(status: str, has_check: bool) -> float:
    if not has_check:
        return 1.0
    table = {"healthy": 1.0, "starting": 0.5, "unhealthy": 0.0}
    return table.get(status, 0.0)

def set_one_hot(container, image, cid, host, status_value: str) -> None:
    global _last_s
    for s in ("healthy", "starting", "unhealthy", "none"):
        g_status.labels(container, image, cid, host, s).set(1 if s == status_value else 0)
    _last_s.update({
        (container, image, cid, host, "healthy"),
        (container, image, cid, host, "starting"),
        (container, image, cid, host, "unhealthy"),
        (container, image, cid, host, "none"),
    })

def parse_started_at(started_at_str: str) -> Optional[float]:
    # Examples: "2025-11-06T21:27:08.123456789Z" or "2025-11-06T21:27:08Z"
    if not started_at_str:
        return None
    try:
        # strip trailing Z and possible nanoseconds
        s = started_at_str.rstrip("Z")
        # keep up to microseconds for datetime.fromisoformat
        if "." in s:
            head, frac = s.split(".", 1)
            s = head + "." + (frac[:6])  # microseconds precision
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None

def scrape_once(client: docker.DockerClient) -> None:
    global _last_h, _last_s, _last_rn, _last_rc, _last_sa

    host = socket.gethostname()
    now_h, now_s, now_rn, now_rc, now_sa = set(), set(), set(), set(), set()

    containers = client.containers.list(all=True)

    for c in containers:
        try:
            c.reload()
            img = (c.image.tags[0] if c.image and c.image.tags else getattr(c.image, "short_id", "unknown")) or "unknown"
            cid = c.short_id
            name = c.name

            st = c.attrs.get("State", {}) or {}
            health = st.get("Health")
            running = 1.0 if st.get("Running") else 0.0

            # RestartCount: prefer top-level .RestartCount, fallback to .State.RestartCount
            rc = c.attrs.get("RestartCount")
            if rc is None:
                rc = st.get("RestartCount")
            try:
                restart_count = float(rc or 0)
            except Exception:
                restart_count = 0.0

            # StartedAt -> seconds since epoch
            started_at_str = st.get("StartedAt") or ""
            started_at = parse_started_at(started_at_str)
            if started_at is None:
                started_at = 0.0

            # Health metrics
            if health:
                status = (health.get("Status") or "unknown").lower()
                num = status_to_num(status, has_check=True)
                if status not in ("healthy", "starting", "unhealthy"):
                    status = "none"
            else:
                status = "none"
                num = status_to_num(status, has_check=False)

            g_health.labels(name, img, cid, host).set(num);      now_h.add((name, img, cid, host))
            set_one_hot(name, img, cid, host, status);           now_s.update({
                (name, img, cid, host, "healthy"),
                (name, img, cid, host, "starting"),
                (name, img, cid, host, "unhealthy"),
                (name, img, cid, host, "none"),
            })
            g_running.labels(name, img, cid, host).set(running); now_rn.add((name, img, cid, host))
            g_restart.labels(name, img, cid, host).set(restart_count); now_rc.add((name, img, cid, host))
            g_started_at.labels(name, img, cid, host).set(started_at);  now_sa.add((name, img, cid, host))

        except Exception as ce:
            log(f"[warn] container {getattr(c, 'name', '?')}: {ce}")

    # remove stale series
    for metric, last_set, now_set in (
        (g_health, _last_h, now_h),
        (g_running, _last_rn, now_rn),
        (g_restart, _last_rc, now_rc),
        (g_started_at, _last_sa, now_sa),
    ):
        for labels in last_set - now_set:
            try: metric.remove(*labels)
            except Exception: pass

    for labels in _last_s - now_s:
        try: g_status.remove(*labels)
        except Exception: pass

    _last_h, _last_s, _last_rn, _last_rc, _last_sa = now_h, now_s, now_rn, now_rc, now_sa

def main() -> None:
    start_http_server(PORT, addr=BIND_ADDR)
    log(f"[info] Exporter listening on http://{BIND_ADDR}:{PORT}/metrics")

    client: Optional[docker.DockerClient] = None
    backoff = 1.0
    while True:
        try:
            if client is None:
                client, used = create_docker_client()
                backoff = 1.0
                log(f"[info] Using Docker base_url: {used}")
            scrape_once(client)
        except DockerException as de:
            log(f"[error] Docker exception: {de}")
            client = None
        except Exception as e:
            log(f"[error] Unexpected error: {e}")
        time.sleep(INTERVAL if client else min(backoff, 30.0))
        if client is None:
            backoff = min(backoff * 2, 30.0)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("[info] Exiting on SIGINT")
        sys.exit(0)
