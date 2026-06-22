from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
VLLM_SR_DOCKERFILE = REPO_ROOT / "src" / "vllm-sr" / "Dockerfile"
VLLM_SR_ROCM_DOCKERFILE = REPO_ROOT / "src" / "vllm-sr" / "Dockerfile.rocm"


def _venv_pip_install_block(content: str) -> str:
    """Return the venv pip install region so dependency assertions stay scoped."""

    marker = '"${VIRTUAL_ENV}/bin/pip" install'
    start = content.index(marker)
    return content[start:]


def test_cpu_router_dockerfile_ships_cli_package() -> None:
    content = VLLM_SR_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY src/vllm-sr/cli/ /app/cli/" in content


def test_cpu_router_dockerfile_venv_installs_runtime_sync_deps() -> None:
    content = VLLM_SR_DOCKERFILE.read_text(encoding="utf-8")
    block = _venv_pip_install_block(content)

    assert "COPY src/vllm-sr/requirements.txt" in content
    assert "-r" in block
    assert "requirements.txt" in block


def test_rocm_router_dockerfile_ships_cli_package() -> None:
    content = VLLM_SR_ROCM_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY src/vllm-sr/cli/ /app/cli/" in content


def test_rocm_router_dockerfile_venv_installs_runtime_sync_deps() -> None:
    content = VLLM_SR_ROCM_DOCKERFILE.read_text(encoding="utf-8")
    block = _venv_pip_install_block(content)

    assert "COPY src/vllm-sr/requirements.txt" in content
    assert "-r" in block
    assert "requirements.txt" in block
