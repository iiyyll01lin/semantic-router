"""Tests for the Strix Halo PoC offline config validator.

Verifies two things:
  * the validator PASSES on the real poc-strix.yaml, and
  * the validator FAILS on an intentionally-broken config (a decision that
    references a model that does not exist in providers.models).

Run with: python -m pytest deploy/recipes/strix-halo-poc/test_validate_poc_config.py
or directly: python deploy/recipes/strix-halo-poc/test_validate_poc_config.py
"""

import importlib.util
import os
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "poc-strix.yaml")


def _load_validator_module():
    spec = importlib.util.spec_from_file_location(
        "validate_poc_config", os.path.join(HERE, "validate_poc_config.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator_module()


def test_valid_config_passes():
    validator = VALIDATOR.Validator(CONFIG_PATH)
    validator.load()
    ok = validator.validate()
    assert ok, "expected poc-strix.yaml to validate, got errors: %s" % validator.errors
    assert not validator.errors


def test_broken_config_fails():
    # Start from the real config and corrupt one decision's modelRef so it
    # references a model that is not declared in providers.models.
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle)

    cfg["routing"]["decisions"][0]["modelRefs"] = [
        {"model": "this/model-does-not-exist", "use_reasoning": False}
    ]

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    try:
        yaml.safe_dump(cfg, tmp, allow_unicode=True)
        tmp.close()

        validator = VALIDATOR.Validator(tmp.name)
        validator.load()
        ok = validator.validate()
        assert not ok, "expected broken config to fail validation"
        assert any("unknown model" in err for err in validator.errors), (
            "expected an 'unknown model' error, got: %s" % validator.errors
        )
    finally:
        os.unlink(tmp.name)


if __name__ == "__main__":
    test_valid_config_passes()
    test_broken_config_fails()
    print("OK: both validator tests passed.")
