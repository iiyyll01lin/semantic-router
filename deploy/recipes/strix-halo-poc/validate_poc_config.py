#!/usr/bin/env python3
"""Offline structural validator for the Strix Halo PoC config (poc-strix.yaml).

This is the on-box static test for the PoC: it loads the YAML with PyYAML and
checks the internal consistency that the live Go router (`go run ./cmd/dsl
validate` on the Strix Halo) would otherwise be the first to catch. It does NOT
contact any backend, model, or running router.

Checks performed:
  1. The file parses as YAML.
  2. Collect ``providers.models[].name`` (the logical routing names).
  3. ``providers.defaults.default_model`` is one of those names.
  4. Every ``routing.decisions[].modelRefs[].model`` resolves to a model name.
  5. Every ``routing.modelCards[].name`` resolves to a model name (warning only).
  6. Every signal leaf referenced in a decision ``rules`` tree resolves to a
     defined signal (recursive AND/OR/NOT walk; leaves carry ``type``+``name``).
  7. Every model has a non-empty ``provider_model_id``.

Usage:
    python validate_poc_config.py [config_path]

``config_path`` defaults to the sibling ``poc-strix.yaml``. Exits non-zero when
any error is found.
"""

import os
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - dependency hint
    sys.stderr.write(
        "ERROR: PyYAML is required. Install it with `pip install pyyaml`.\n"
    )
    sys.exit(2)


# Maps a rule leaf ``type`` to the key under ``routing.signals`` that holds the
# matching signal definitions. Several signal collections are pluralized in the
# config (domain -> domains), and a few types resolve outside ``routing.signals``
# entirely (handled specially below).
SIGNAL_TYPE_TO_KEY = {
    "domain": "domains",
    "keyword": "keywords",
    "embedding": "embeddings",
    "user_feedback": "user_feedbacks",
    "reask": "reasks",
    "complexity": "complexity",
    "context": "context",
    "structure": "structure",
    "language": "language",
    "fact_check": "fact_check",
    "jailbreak": "jailbreak",
    "pii": "pii",
}


class Validator:
    def __init__(self, config_path):
        self.config_path = config_path
        self.errors = []
        self.warnings = []
        self.cfg = {}

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def load(self):
        with open(self.config_path, "r", encoding="utf-8") as handle:
            self.cfg = yaml.safe_load(handle)
        if not isinstance(self.cfg, dict):
            raise ValueError("top-level YAML is not a mapping")

    # -- signal registry ----------------------------------------------------
    def _signal_names_by_type(self):
        """Return {rule_type: set(names)} for every leaf type used in rules."""
        registry = {}
        signals = ((self.cfg.get("routing") or {}).get("signals")) or {}
        for rule_type, key in SIGNAL_TYPE_TO_KEY.items():
            names = set()
            for entry in signals.get(key) or []:
                if isinstance(entry, dict) and entry.get("name") is not None:
                    names.add(str(entry["name"]))
            registry[rule_type] = names

        # ``projection`` leaves reference projection mapping outputs / partition
        # members, which live under routing.projections (not routing.signals).
        projections = ((self.cfg.get("routing") or {}).get("projections")) or {}
        proj_names = set()
        for mapping in projections.get("mappings") or []:
            for output in (mapping or {}).get("outputs") or []:
                if isinstance(output, dict) and output.get("name") is not None:
                    proj_names.add(str(output["name"]))
        for partition in projections.get("partitions") or []:
            if not isinstance(partition, dict):
                continue
            for member in partition.get("members") or []:
                proj_names.add(str(member))
            if partition.get("default") is not None:
                proj_names.add(str(partition["default"]))
        registry["projection"] = proj_names
        return registry

    def _signal_leaf_ok(self, registry, leaf_type, leaf_name):
        names = registry.get(leaf_type)
        if names is None:
            # Unknown signal type: cannot verify, so flag it rather than pass.
            return False
        if leaf_name in names:
            return True
        # Complexity leaves are written as ``<signal>:<band>`` (e.g.
        # ``math_task:hard``); the band suffix is not part of the signal name.
        if leaf_type == "complexity" and ":" in leaf_name:
            return leaf_name.split(":", 1)[0] in names
        return False

    def _walk_rules(self, node, decision_name, registry, path):
        if node is None:
            return
        if not isinstance(node, dict):
            self.error(
                "decision %r has a malformed rule node at %s" % (decision_name, path)
            )
            return
        # Branch node: operator + conditions.
        if "operator" in node and "conditions" in node:
            conditions = node.get("conditions") or []
            for idx, child in enumerate(conditions):
                self._walk_rules(
                    child, decision_name, registry, "%s.conditions[%d]" % (path, idx)
                )
            return
        # A bare ``operator: AND`` with no conditions is the empty/always rule.
        if "operator" in node and "conditions" not in node:
            return
        # Leaf node: type + name.
        if "type" in node and "name" in node:
            leaf_type = str(node["type"])
            leaf_name = str(node["name"])
            if not self._signal_leaf_ok(registry, leaf_type, leaf_name):
                self.error(
                    "decision %r references unknown signal %s:%s at %s"
                    % (decision_name, leaf_type, leaf_name, path)
                )
            return
        self.error(
            "decision %r has an unrecognized rule node at %s (keys=%s)"
            % (decision_name, path, sorted(node.keys()))
        )

    # -- top-level checks ---------------------------------------------------
    def validate(self):
        providers = self.cfg.get("providers") or {}
        models = providers.get("models") or []
        if not models:
            self.error("providers.models is empty or missing")

        model_names = set()
        for idx, model in enumerate(models):
            if not isinstance(model, dict):
                self.error("providers.models[%d] is not a mapping" % idx)
                continue
            name = model.get("name")
            if not name:
                self.error("providers.models[%d] is missing a name" % idx)
            else:
                model_names.add(str(name))
            pid = model.get("provider_model_id")
            if not pid or not str(pid).strip():
                self.error(
                    "model %r has an empty provider_model_id" % (name or idx)
                )

        # default_model must resolve.
        defaults = providers.get("defaults") or {}
        default_model = defaults.get("default_model")
        if not default_model:
            self.error("providers.defaults.default_model is missing")
        elif str(default_model) not in model_names:
            self.error(
                "providers.defaults.default_model %r does not resolve to a "
                "providers.models[].name" % default_model
            )

        routing = self.cfg.get("routing") or {}

        # Every decision modelRef must resolve; walk rules for signal refs.
        registry = self._signal_names_by_type()
        decisions = routing.get("decisions") or []
        if not decisions:
            self.error("routing.decisions is empty or missing")
        for idx, decision in enumerate(decisions):
            if not isinstance(decision, dict):
                self.error("routing.decisions[%d] is not a mapping" % idx)
                continue
            dname = decision.get("name") or ("#%d" % idx)
            for ref in decision.get("modelRefs") or []:
                if not isinstance(ref, dict):
                    self.error("decision %r has a malformed modelRef" % dname)
                    continue
                model = ref.get("model")
                if not model:
                    self.error("decision %r has a modelRef without a model" % dname)
                elif str(model) not in model_names:
                    self.error(
                        "decision %r references unknown model %r" % (dname, model)
                    )
            self._walk_rules(decision.get("rules"), dname, registry, "rules")

        # modelCards should resolve (warning only).
        for card in routing.get("modelCards") or []:
            if not isinstance(card, dict):
                continue
            cname = card.get("name")
            if cname and str(cname) not in model_names:
                self.warn(
                    "modelCard %r does not resolve to a providers.models[].name"
                    % cname
                )

        return len(self.errors) == 0


def main(argv):
    if len(argv) > 1:
        config_path = argv[1]
    else:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "poc-strix.yaml")

    validator = Validator(config_path)
    print("Validating %s" % config_path)
    try:
        validator.load()
    except FileNotFoundError:
        print("FAIL: config file not found: %s" % config_path)
        return 1
    except Exception as exc:  # noqa: BLE001 - surface any parse error
        print("FAIL: could not parse YAML: %s" % exc)
        return 1

    ok = validator.validate()

    for warning in validator.warnings:
        print("  WARN: %s" % warning)
    for err in validator.errors:
        print("  ERROR: %s" % err)

    if ok:
        models = (validator.cfg.get("providers") or {}).get("models") or []
        decisions = (validator.cfg.get("routing") or {}).get("decisions") or []
        print(
            "PASS: %d models, %d decisions, %d warning(s)."
            % (len(models), len(decisions), len(validator.warnings))
        )
        return 0
    print("FAIL: %d error(s), %d warning(s)." % (len(validator.errors), len(validator.warnings)))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
