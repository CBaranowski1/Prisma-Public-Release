"""Security and wiring guards for the co-located DGX Spark Compose stack."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "docker" / "dgx-spark.yml"
MODEL_ONLY = ROOT / "deploy" / "dgx-spark" / "docker-compose.yml"
QWEN_MODEL = "${DGX_MODEL:?Set DGX_MODEL to a supported Hugging Face model handle}"
VLLM_IMAGE = (
    "${DGX_VLLM_IMAGE:-vllm/vllm-openai@sha256:"
    "ffb2d59b1c059a5bd8d781320c9f5189de8293693b7d95da54befddaa54abf52}"
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_colocated_prisma_routes_to_private_authenticated_vllm():
    services = _load(OVERLAY)["services"]
    prisma = services["prisma"]

    assert prisma["environment"] == {
        "DGX_SPARK_BASE_URL": "http://vllm:8000/v1",
        "DGX_SPARK_API_KEY": (
            "${DGX_API_KEY:?Set DGX_API_KEY to a long random secret}"
        ),
        "DGX_SPARK_MODEL": (
            "${DGX_MODEL:?Set DGX_MODEL to a supported Hugging Face model handle}"
        ),
        "DGX_SPARK_MAX_MODEL_LEN": "${DGX_MAX_MODEL_LEN:-32768}",
        "DGX_SPARK_ALLOW_INSECURE_NO_AUTH": "false",
    }
    assert prisma["depends_on"]["vllm"] == {"condition": "service_healthy"}


def test_colocated_vllm_is_private_hardened_and_gpu_backed():
    vllm = _load(OVERLAY)["services"]["vllm"]

    assert vllm["image"] == VLLM_IMAGE
    assert "ports" not in vllm
    assert vllm["expose"] == ["8000"]
    assert vllm["user"] == "${DGX_UID:-1000}:${DGX_GID:-1000}"
    assert vllm["init"] is True
    assert vllm["read_only"] is True
    assert vllm["security_opt"] == ["no-new-privileges:true"]
    assert vllm["cap_drop"] == ["ALL"]
    assert vllm["command"][-2:] == [
        "--api-key",
        "${DGX_API_KEY:?Set DGX_API_KEY to a long random secret}",
    ]
    devices = vllm["deploy"]["resources"]["reservations"]["devices"]
    assert devices == [
        {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}
    ]


def test_colocated_vllm_uses_agent_ready_qwen_recipe_and_persistent_cache():
    vllm = _load(OVERLAY)["services"]["vllm"]
    command = vllm["command"]

    assert command[0] == QWEN_MODEL
    assert command[-2:] == [
        "--api-key",
        "${DGX_API_KEY:?Set DGX_API_KEY to a long random secret}",
    ]
    assert vllm["volumes"] == [
        "${DGX_HF_CACHE:-./deploy/dgx-spark/cache}:/cache/huggingface"
    ]
    assert vllm["environment"]["USER"] == "vllm"
    assert vllm["environment"]["LOGNAME"] == "vllm"
    assert vllm["environment"]["XDG_CACHE_HOME"] == "/tmp/vllm-home/.cache"
    assert vllm["environment"]["TORCHINDUCTOR_CACHE_DIR"] == "/tmp/torchinductor"
    assert vllm["tmpfs"][-1].endswith(",exec")
    assert vllm["tmpfs"][-1].startswith("/tmp/vllm-home:")
    assert vllm["environment"]["HF_HOME"] == "/cache/huggingface"
    assert "HF_TOKEN" not in vllm["environment"]
    assert vllm["environment"]["HF_HUB_OFFLINE"] == "${HF_HUB_OFFLINE:-0}"

    required_flags = {
        "--trust-remote-code",
        "--enable-chunked-prefill",
        "--async-scheduling",
        "--enable-prefix-caching",
        "--enable-auto-tool-choice",
    }
    assert required_flags.issubset(command)
    assert command[command.index("--gpu-memory-utilization") + 1] == (
        "${DGX_GPU_MEMORY_UTILIZATION:-0.30}"
    )
    assert command[command.index("--max-model-len") + 1] == (
        "${DGX_MAX_MODEL_LEN:-32768}"
    )
    assert command[command.index("--max-num-seqs") + 1] == (
        "${DGX_MAX_NUM_SEQS:-2}"
    )
    assert command[command.index("--max-num-batched-tokens") + 1] == (
        "${DGX_MAX_NUM_BATCHED_TOKENS:-4096}"
    )
    assert command[command.index("--reasoning-parser") + 1] == "qwen3"
    assert command[command.index("--tool-call-parser") + 1] == "qwen3_xml"


def test_model_only_and_colocated_vllm_keep_same_security_contract():
    colocated = _load(OVERLAY)["services"]["vllm"]
    model_only = _load(MODEL_ONLY)["services"]["vllm"]

    assert colocated["healthcheck"]["test"][1] == "python3"

    for key in (
        "image",
        "user",
        "init",
        "read_only",
        "security_opt",
        "cap_drop",
        "shm_size",
        "tmpfs",
        "command",
        "deploy",
        "healthcheck",
        "restart",
    ):
        assert colocated[key] == model_only[key]
