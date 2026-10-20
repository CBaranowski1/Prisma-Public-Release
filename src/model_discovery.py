import subprocess
import json
import time
import httpx
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

from src.settings import get_setting
from src.tls_overrides import llm_verify

logger = logging.getLogger(__name__)

# Last-resort fallback only — used when a live GET to https://api.openai.com/v1/models
# fails (connection error, non-2xx, or an unexpected response shape). Mirrors the
# live-fetch-then-curated-fallback idiom in routes/model_routes.py::_probe_endpoint.
_FALLBACK_OPENAI_MODELS = [
    "gpt-5.2-codex", "gpt-4o-mini", "gpt-image-1.5",
    "gpt-4o", "gpt-5.2", "gpt-5.2-pro",
]


def _fetch_openai_models(api_key: str, *, timeout: int = 5) -> Optional[List[str]]:
    """Live-fetch the OpenAI model list from GET /v1/models.

    Returns a non-empty list of model IDs on success, or None if the call
    fails (HTTP error, connection error, or an unexpected/non-dict response
    shape) so the caller can fall back to the curated list."""
    url = "https://api.openai.com/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        r = httpx.get(url, headers=headers, timeout=timeout, verify=llm_verify())
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            logger.warning(
                "openai /v1/models returned unexpected shape (%s); using fallback list",
                type(data).__name__,
            )
            return None
        models = [m.get("id") for m in data["data"] if isinstance(m, dict) and m.get("id")]
        return models or None
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        logger.warning(f"openai /v1/models live fetch failed: HTTP {status}; using fallback list")
    except Exception as e:
        logger.warning(f"openai /v1/models live fetch failed: {e}; using fallback list")
    return None

# Cache for discovered hosts
_hosts_cache: List[str] = []
_hosts_cache_time: float = 0
_HOSTS_CACHE_TTL = 60  # seconds


def _parse_tailscale_status(raw: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _first_tailscale_ipv4(value: Any) -> Optional[str]:
    if not isinstance(value, list):
        return None
    for ip in value:
        if isinstance(ip, str) and "." in ip:
            return ip
    return None


def discover_tailscale_hosts() -> List[str]:
    """Discover online Tailscale peers, returning their IPv4 addresses."""
    global _hosts_cache, _hosts_cache_time

    now = time.time()
    if _hosts_cache and (now - _hosts_cache_time) < _HOSTS_CACHE_TTL:
        return list(_hosts_cache)

    hosts = []
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return hosts

        data = _parse_tailscale_status(result.stdout)
        if not data:
            return hosts

        # Add self
        self_data = data.get("Self") if isinstance(data.get("Self"), dict) else {}
        self_ip = _first_tailscale_ipv4(self_data.get("TailscaleIPs"))
        if self_ip:
            hosts.append(self_ip)

        # Add online peers (skip funnel-ingress-nodes and android devices)
        peers = data.get("Peer") if isinstance(data.get("Peer"), dict) else {}
        for peer in peers.values():
            if not isinstance(peer, dict):
                continue
            if not peer.get("Online"):
                continue
            hostname = peer.get("HostName", "")
            if hostname == "funnel-ingress-node":
                continue
            os_name = peer.get("OS", "")
            if os_name == "android":
                continue
            peer_ip = _first_tailscale_ipv4(peer.get("TailscaleIPs"))
            if peer_ip:
                hosts.append(peer_ip)

        _hosts_cache = hosts
        _hosts_cache_time = now
        logger.info(f"Tailscale discovery found {len(hosts)} hosts: {hosts}")
    except FileNotFoundError:
        logger.debug("tailscale command not found")
    except Exception as e:
        logger.warning(f"Tailscale discovery failed: {e}")

    return hosts


class ModelDiscovery:
    def __init__(self, default_host: str, openai_api_key: Optional[str] = None):
        self.default_host = default_host
        self.openai_api_key = openai_api_key
        self.openai_compat_path = "/v1/chat/completions"
        # Custom ports from env vars, merged into the scan list by discover_models.
        self._extra_ports: set = set()

    def _get_hosts(self) -> List[str]:
        """Get all hosts to scan, using env override, Tailscale, or default."""
        self._extra_ports = set()

        def _append_host(out: List[str], host: str) -> None:
            host = (host or "").strip()
            if not host or host in out:
                return
            out.append(host)

        def _append_env_hosts(out: List[str]) -> None:
            """Add hosts (and any custom ports) from provider-specific env vars."""
            for env_name in ("OLLAMA_BASE_URL", "OLLAMA_URL", "LM_STUDIO_URL"):
                raw = os.getenv(env_name, "").strip()
                if not raw:
                    continue
                try:
                    parsed = urlparse(raw if "://" in raw else "http://" + raw)
                    _append_host(out, parsed.hostname or "")
                    if parsed.port:
                        self._extra_ports.add(parsed.port)
                except Exception:
                    pass

        # Manual override takes priority
        extra = os.getenv("LLM_HOSTS", "").strip()
        if extra:
            hosts = [h.strip() for h in extra.split(",") if h.strip()]
            # Always include the default host too
            if self.default_host not in hosts:
                hosts.insert(0, self.default_host)
            _append_host(hosts, "host.docker.internal")
            _append_env_hosts(hosts)
            return hosts

        # SEC-16: the Tailscale sweep is an opt-in reconnaissance feature —
        # default OFF. With it off, only the configured/default host is
        # returned (no broad internal-network scan). This check MUST sit
        # after the LLM_HOSTS early-return above (Pitfall 4 — explicit admin
        # config always wins) and before the Tailscale sweep below.
        if not get_setting("model_discovery_scan_enabled", False):
            hosts = [self.default_host]
            _append_host(hosts, "host.docker.internal")
            _append_env_hosts(hosts)
            return hosts

        # Try Tailscale discovery
        ts_hosts = discover_tailscale_hosts()
        if ts_hosts:
            # Ensure default_host is included
            if self.default_host not in ts_hosts:
                ts_hosts.insert(0, self.default_host)
            _append_host(ts_hosts, "host.docker.internal")
            _append_env_hosts(ts_hosts)
            return ts_hosts

        hosts = [self.default_host]
        # Docker desktop/Linux compose maps this to the host machine. That is
        # the common "I started Ollama normally on this computer" case.
        _append_host(hosts, "host.docker.internal")
        _append_env_hosts(hosts)
        return hosts

    def _fingerprint_provider(self, host: str, port: int) -> Optional[str]:
        """Identify the server software via its native API, independent of port."""
        try:
            r = httpx.get(f"http://{host}:{port}/api/v1/models", timeout=1.5)
            if r.is_success:
                models = (r.json() or {}).get("models")
                if (isinstance(models, list) and models
                        and isinstance(models[0], dict)
                        and "key" in models[0] and "architecture" in models[0]):
                    return "lmstudio"
        except Exception:
            pass
        return None

    def _check_port(self, host: str, port: int) -> Optional[Dict[str, Any]]:
        """Check a single host:port for models."""
        base = f"http://{host}:{port}/v1"
        try:
            r = httpx.get(f"{base}/models", timeout=3)
            if not r.is_success:
                return None
            data = r.json() or {}
            if not isinstance(data, dict):
                logger.warning(
                    "model discovery: %s:%s returned non-dict /v1/models shape (%s); skipping",
                    host, port, type(data).__name__,
                )
                return None
            ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
            if ids:
                return {
                    "host": host,
                    "port": port,
                    "url": f"http://{host}:{port}{self.openai_compat_path}",
                    "models": ids,
                    "models_display": [i.lstrip("/") for i in ids],
                    "provider": self._fingerprint_provider(host, port),
                }
        except Exception:
            pass
        return None

    def _discover_dgx_spark(self) -> Optional[Dict[str, Any]]:
        """Probe only the exact authenticated DGX Spark URL from the env."""
        from src.dgx_spark import load_dgx_spark_config

        try:
            config = load_dgx_spark_config()
        except ValueError as exc:
            logger.warning("DGX Spark discovery disabled: %s", exc)
            return None
        if config is None:
            return None

        try:
            response = httpx.get(
                config.models_url,
                headers=config.headers,
                timeout=3,
                verify=llm_verify(),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                return None
            ids = [
                item.get("id")
                for item in (payload.get("data") or [])
                if isinstance(item, dict) and item.get("id")
            ]
            if not ids:
                return None
            parsed = urlparse(config.base_url)
            return {
                "host": parsed.hostname,
                "port": parsed.port or (443 if parsed.scheme == "https" else 80),
                "url": config.chat_url,
                "models": ids,
                "models_display": [item.lstrip("/") for item in ids],
                "provider": "dgx-spark",
            }
        except Exception as exc:
            logger.warning("DGX Spark model discovery failed: %s", exc)
            return None

    def discover_models(self) -> Dict[str, List[Dict[str, Any]]]:
        """Discover available models from all reachable hosts."""
        hosts = self._get_hosts()

        # SEC-16: when the opt-in scan is enabled, consult the allowlist
        # (empty list = no additional filter, since ON-mode is already an
        # explicit admin opt-in).
        if get_setting("model_discovery_scan_enabled", False):
            allowed = get_setting("model_discovery_allowed_hosts", [])
            if allowed:
                hosts = [h for h in hosts if h in allowed]

        items = []
        dgx_spark = self._discover_dgx_spark()
        if dgx_spark:
            items.append(dgx_spark)

        logger.info(f"Scanning {len(hosts)} hosts for models: {hosts}")

        # Well-known ports: 8000-8020 (vLLM, llama.cpp, SGLang, Cookbook),
        # 1234 (LM Studio), 11434 (Ollama)
        ports = list(range(8000, 8021)) + [1234, 11434]
        ports += [p for p in sorted(self._extra_ports) if p not in ports]
        targets = [(h, p) for h in hosts for p in ports]

        seen_models = {
            (item["port"], tuple(sorted(item["models"]))) for item in items
        }  # dedupe by (port, model_ids) to avoid same machine via different IPs

        # SEC-16: concurrency-capped at 10 (down from 50) to bound the
        # scan's fan-out/DoS surface (T-20-03-B), even when explicitly
        # enabled.
        max_workers = max(1, min(10, len(targets) or 1))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._check_port, h, p): (h, p) for h, p in targets}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    key = (result["port"], tuple(sorted(result["models"])))
                    if key not in seen_models:
                        seen_models.add(key)
                        items.append(result)

        # Sort by host then port for consistent ordering
        items.sort(key=lambda x: (x["host"], x["port"]))

        logger.info(f"Discovered {len(items)} model endpoints across {len(hosts)} hosts")
        result = {"hosts": hosts, "items": items}
        # Additive-only: never remove/replace hosts/items (routes/model_routes.py
        # UI consumers depend on the existing shape). Only add status/remediation
        # when there is truly nothing to work with — no reachable endpoint AND
        # no OpenAI API key configured — so a fresh install gets actionable
        # guidance instead of a silent empty list.
        if not items and not getattr(self, "openai_api_key", None):
            result["status"] = "none_found"
            result["remediation"] = (
                "No LLM models were found. Start a local LLM server on a "
                "well-known port (e.g. Ollama on 11434, LM Studio on 1234, or "
                "vLLM/llama.cpp on 8000), set LLM_HOSTS to point at a reachable "
                "host, or configure an OpenAI API key."
            )
        return result

    def get_providers(self) -> Dict[str, Any]:
        """Get all available providers"""
        discovery = self.discover_models()
        items = discovery["items"]
        providers = [{"provider": "vllm", "hosts": discovery["hosts"], "items": items}]

        if self.openai_api_key:
            live = _fetch_openai_models(self.openai_api_key)
            models = live if live else _FALLBACK_OPENAI_MODELS
            logger.info("openai model list: %s", "live" if live else "fallback (live fetch failed)")
            providers.append({
                "provider": "openai",
                "items": [{
                    "url": "https://api.openai.com/v1/chat/completions",
                    "models": models
                }]
            })

        return {"providers": providers}
