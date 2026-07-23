import copy
import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("validate_agentic_context_reports.py")
SPEC = importlib.util.spec_from_file_location(
    "validate_agentic_context_reports", MODULE_PATH
)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)

BASE = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = Path.home() / "vllm-sr-evidence/agentic-context-customer-20260722"
MILESTONE_ROOT = Path.home() / "vllm-sr-evidence/demo-002-capacity-matrix"
PREFILL_ROOT = Path.home() / "vllm-sr-evidence/agentic-prefill-20260722"


def load_sources():
    return (
        validator._load_json(BASE / validator.FOUR_PROOF),
        validator._load_json(BASE / validator.EVIDENCE_INDEX),
    )


def load_texts():
    return {
        name: (BASE / path).read_text(encoding="utf-8")
        for name, path in validator.REPORTS.items()
    }


class ReportConsistencyTests(unittest.TestCase):
    def test_canonical_report_set_passes(self):
        self.assertEqual(validator.validate_report_set(BASE), [])

    def test_context_arithmetic_drift_fails(self):
        four, evidence = load_sources()
        four["serving_window"]["max_tested_input_tokens"] = 65536
        errors = validator.validate_structured(four, evidence)
        self.assertTrue(any("max_tested_input_tokens" in error for error in errors))
        self.assertTrue(any("context arithmetic" in error for error in errors))

    def test_capacity_unit_or_count_drift_fails(self):
        mutations = [
            ("cells_total", 174),
            ("measured_requests", 17),
            ("marker_passes", 174),
            ("failed_marker_gate_only", 9),
        ]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                four, evidence = load_sources()
                four["proofs"]["capacity"][key] = value
                errors = validator.validate_structured(four, evidence)
                self.assertTrue(
                    any(
                        f"capacity.{key}" in error or "green + failed" in error
                        for error in errors
                    )
                )

    def test_replay_abort_causality_drift_fails(self):
        four, evidence = load_sources()
        four["run_orchestration_limitation"][
            "recorded_stop_reason"
        ] = "launching SSH session ended"
        errors = validator.validate_structured(four, evidence)
        self.assertTrue(
            any("explicit user scope decision" in error for error in errors)
        )

    def test_quality_and_reliability_overclaim_fails(self):
        four, evidence = load_sources()
        four["proofs"]["quality"]["status"] = "PASS"
        four["proofs"]["reliability"]["status"] = "PASS"
        evidence["proof_statuses"]["quality"] = "PASS"
        evidence["proof_statuses"]["reliability"] = "PASS"
        errors = validator.validate_structured(four, evidence)
        self.assertTrue(
            any(
                "quality.status" in error or "proof statuses" in error
                for error in errors
            )
        )
        self.assertTrue(any("reliability" in error for error in errors))

    def test_vllm_apc_misattribution_fails(self):
        four, evidence = load_sources()
        four["proofs"]["performance"][
            "vllm_apc_crossref"
        ] = "Ollama APC: 144.3s to 30.8s"
        errors = validator.validate_structured(four, evidence)
        self.assertTrue(any("forbid attribution" in error for error in errors))

    def test_stale_blanket_vllm_claim_fails(self):
        texts = load_texts()
        texts["customer_report"] += "\nvLLM is skip-with-reason on gfx1151\n"
        errors = validator.validate_markdown(texts)
        self.assertTrue(any("stale blanket vLLM claim" in error for error in errors))

    def test_obsolete_replay_abort_wording_fails(self):
        texts = load_texts()
        texts[
            "recipe_readme"
        ] += (
            "\nstopped their background runners when the launching SSH sessions ended\n"
        )
        errors = validator.validate_markdown(texts)
        self.assertTrue(
            any("obsolete replay-abort causality" in error for error in errors)
        )

    def test_duplicate_finalization_fails(self):
        texts = load_texts()
        texts["campaign_ledger"] += "\n## 2026-07-23 finalization: duplicate\n"
        errors = validator.validate_markdown(texts)
        self.assertTrue(any("duplicate finalization" in error for error in errors))

    def test_archive_generation_drift_fails(self):
        four, evidence = load_sources()
        mutated = copy.deepcopy(four)
        mutated["evidence_integrity"]["interim_demo_manifest"]["entries"] = 151
        errors = validator.validate_structured(mutated, evidence)
        self.assertTrue(
            any("interim_demo_manifest entries" in error for error in errors)
        )

    def test_milestone_or_nested_request_drift_fails(self):
        four, evidence = load_sources()
        mutations = [
            ("demo002_ollama_milestone", "measured_requests", 8),
            ("halo_a_llamacpp_out256", "measured_requests", 6),
            ("halo_a_llamacpp_out256", "marker_passes", 20),
        ]
        for name, key, value in mutations:
            with self.subTest(name=name, key=key, value=value):
                mutated = copy.deepcopy(evidence)
                mutated["campaign_crosschecks"][name][key] = value
                errors = validator.validate_structured(four, mutated)
                self.assertTrue(
                    any(
                        f"campaign crosscheck {name}.{key}" in error for error in errors
                    )
                )

    def test_mirror_path_or_archive_hash_drift_fails(self):
        four, evidence = load_sources()
        mutated = copy.deepcopy(evidence)
        mutated["campaign_crosschecks"]["demo002_ollama_milestone"]["evidence"][
            0
        ] = "/nonexistent/milestone-summary.json"
        mutated["evidence_generations"]["immutable_v1_archives"][
            "agentic_prefill_tar_sha256"
        ] = ("0" * 64)
        errors = validator.validate_structured(four, mutated)
        self.assertTrue(any(".evidence" in error for error in errors))
        self.assertTrue(any("archive hashes" in error for error in errors))

    def test_nested_request_rollup_does_not_count_cells_as_requests(self):
        cells = [
            {
                "cell_id": "one-cell",
                "cold_requests": [
                    {"success": True, "error": "", "marker_correct": True}
                ],
                "warm_requests": [
                    {"success": True, "error": "", "marker_correct": False},
                    {"success": True, "error": "", "marker_correct": True},
                ],
            }
        ]
        rollup, errors = validator._nested_request_rollup(cells, "fixture")
        self.assertEqual(errors, [])
        self.assertEqual(
            rollup,
            {"measured_requests": 3, "http_successes": 3, "marker_passes": 2},
        )

    @unittest.skipUnless(
        MILESTONE_ROOT.exists(), "preserved milestone mirror unavailable"
    )
    def test_preserved_milestone_evidence_passes(self):
        self.assertEqual(validator.validate_milestone_evidence(MILESTONE_ROOT), [])

    @unittest.skipUnless(
        PREFILL_ROOT.exists(), "preserved Halo-A prefill evidence unavailable"
    )
    def test_preserved_llamacpp_out256_evidence_passes(self):
        self.assertEqual(validator.validate_llamacpp_out256(PREFILL_ROOT), [])

    @unittest.skipUnless(
        EVIDENCE_ROOT.exists(), "preserved evidence mirror unavailable"
    )
    def test_preserved_selected_scope_passes(self):
        summary = validator._load_json(
            EVIDENCE_ROOT / "analysis/final-selected-scope-summary.json"
        )
        self.assertEqual(validator.validate_selected_scope(summary), [])

    @unittest.skipUnless(
        EVIDENCE_ROOT.exists(), "preserved evidence mirror unavailable"
    )
    def test_preserved_capacity_summaries_pass(self):
        summary_dir = EVIDENCE_ROOT / "capacity-direct-openai/summary"
        self.assertEqual(validator.validate_capacity_summaries(summary_dir), [])

    @unittest.skipUnless(
        EVIDENCE_ROOT.exists(), "preserved evidence mirror unavailable"
    )
    def test_selected_scope_request_drift_fails(self):
        summary = validator._load_json(
            EVIDENCE_ROOT / "analysis/final-selected-scope-summary.json"
        )
        summary["capacity"]["totals"]["measured_requests"] = 17
        errors = validator.validate_selected_scope(summary)
        self.assertTrue(any("measured_requests" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
