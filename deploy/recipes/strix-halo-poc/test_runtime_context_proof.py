"""Focused tests for the Strix Halo Ollama context/provenance contract."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "poc-strix.yaml"
RUNTIME_SCRIPT = HERE / "ollama-runtime.sh"


def _load_proof_module():
    spec = importlib.util.spec_from_file_location(
        "runtime_context_proof", HERE / "runtime_context_proof.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROOF = _load_proof_module()


def _runtime_defaults():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VLLM_SR_OLLAMA_")
        and key != "VLLM_SR_ALLOW_EXPERIMENTAL_CONTEXT"
    }
    result = subprocess.run(
        ["bash", str(RUNTIME_SCRIPT), "print-config"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return json.loads(result.stdout)


def _matching_container(context=65_536, parallel=1):
    return {
        "name": "ollama",
        "config_image": (
            "ollama/ollama:rocm@sha256:"
            "4a22dbbce24e7425861020987adb99851282b5af8e433028d1c72c453eed8f75"
        ),
        "environment": {
            "OLLAMA_CONTEXT_LENGTH": str(context),
            "OLLAMA_NUM_PARALLEL": str(parallel),
        },
    }


def test_runtime_defaults_match_current_config():
    defaults = _runtime_defaults()
    assert defaults["context_length"] == 65_536
    assert defaults["num_parallel"] == 1
    assert defaults["max_loaded_models"] == 1
    assert "@sha256:" in defaults["image"]
    assert defaults["primary_model"] == "gemma4:26b-a4b-it-q8_0"

    with CONFIG_PATH.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    models = {model["name"]: model for model in config["providers"]["models"]}
    default_name = config["providers"]["defaults"]["default_model"]
    assert models[default_name]["provider_model_id"] == defaults["primary_model"]

    # Every auto-routed decision that uses Ollama must be provisioned by default.
    active_names = {
        ref["model"]
        for decision in config["routing"]["decisions"]
        for ref in decision.get("modelRefs") or []
    }
    active_ollama_tags = {
        models[name]["provider_model_id"]
        for name in active_names
        if any(
            backend.get("endpoint") == "ollama:11434"
            for backend in models[name].get("backend_refs") or []
        )
    }
    assert active_ollama_tags <= set(defaults["models"])

    # Router model cards expose the configured serving limit, not model metadata.
    provider_names = set(models)
    card_contexts = {
        card["name"]: card["context_window_size"]
        for card in config["routing"]["modelCards"]
        if card.get("name") in provider_names
    }
    assert set(card_contexts) == provider_names
    assert set(card_contexts.values()) == {defaults["context_length"]}


def test_container_provenance_uses_environment_allowlist():
    raw = {
        "Id": "container-id",
        "Name": "/ollama",
        "Config": {
            "Image": "ollama/image@sha256:abc",
            "Env": [
                "OLLAMA_CONTEXT_LENGTH=65536",
                "OLLAMA_NUM_PARALLEL=1",
                "PASSWORD=must-not-appear",
                "API_TOKEN=must-not-appear",
            ],
        },
        "HostConfig": {"Devices": [], "RestartPolicy": {"Name": "unless-stopped"}},
        "State": {"Running": True, "Status": "running"},
        "NetworkSettings": {"Networks": {}, "Ports": {}},
        "Mounts": [],
    }
    facts = PROOF.safe_container_facts(raw)
    assert facts["environment"] == {
        "OLLAMA_CONTEXT_LENGTH": "65536",
        "OLLAMA_NUM_PARALLEL": "1",
    }
    rendered = json.dumps(facts)
    assert "must-not-appear" not in rendered
    assert "PASSWORD" not in rendered
    assert "API_TOKEN" not in rendered


def test_model_context_limit_finds_architecture_metadata():
    assert (
        PROOF.model_context_limit(
            {
                "general.architecture": "gemma3",
                "gemma3.context_length": 131_072,
                "other.context_length": "32768",
            }
        )
        == 131_072
    )
    assert PROOF.model_context_limit({"general.architecture": "gemma3"}) is None


def test_loaded_64k_context_contract_passes():
    image = _matching_container()["config_image"]
    checks = PROOF.evaluate_checks(
        container=_matching_container(),
        expected_image=image,
        expected_context=65_536,
        expected_parallel=1,
        minimum_context=65_536,
        allow_experimental_context=False,
        runtime_version="0.0.test",
        model_row={"name": PROOF.DEFAULT_MODEL, "digest": "sha256:model"},
        model_limit=131_072,
        process_row={
            "name": PROOF.DEFAULT_MODEL,
            "context_length": 65_536,
            "size": 10,
            "size_vram": 10,
        },
        require_loaded=True,
    )
    assert not [check for check in checks if check["status"] == "fail"]


def test_unloaded_or_unacknowledged_context_cannot_be_claimed():
    image = _matching_container(context=131_072)["config_image"]
    checks = PROOF.evaluate_checks(
        container=_matching_container(context=131_072),
        expected_image=image,
        expected_context=131_072,
        expected_parallel=1,
        minimum_context=65_536,
        allow_experimental_context=False,
        runtime_version="0.0.test",
        model_row={"name": PROOF.DEFAULT_MODEL},
        model_limit=131_072,
        process_row=None,
        require_loaded=True,
    )
    failures = {check["name"] for check in checks if check["status"] == "fail"}
    assert "experimental_context_acknowledged" in failures
    assert "loaded_context_matches" in failures
