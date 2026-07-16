# Halo-B candidate sweep summary

Measured on Halo-B (`10.96.31.132`) with forced speed/quality residency. Quality is the same 42-question MMLU-Pro slice used by the Gemma frontier; capped P1/P2 quality rows without JSON are marked as timeout/no-result.

| Priority | Candidate | Tag | Speed tok/s | VRAM GiB | Quality | Power tok/s/W | Status | Notes |
| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- |
| P0 | Qwen3-Coder 30B A3B | `qwen3-coder:30b` | 71.0 | 18.1 | 54.8% (23/42) | 0.346 | measured | Apache-2.0 |
| P0 | Qwen3.6 27B | `qwen3.6:27b` | 13.5 | 15.6 | 69.0% (29/42) | 0.082 | measured | Apache-2.0 |
| P0 | Qwen3-Next 80B A3B | `qwen3-next:80b` | 49.6 | 47.4 | 61.9% (26/42) | 0.305 | measured | Apache-2.0 |
| P1 | EXAONE 4.0 32B | `ingu627/exaone4.0:32b` | 11.1 | 19.4 | 50.0% (21/42)† | 0.078 | measured† | quality completed later in the operating-profiles run (profiles-summary-halo-b.md); research-only/non-commercial |
| P1 | OpenThinker2 32B | `openthinker:32b-v2-q4_K_M` | 11.0 | 19.7 | timeout/no JSON | 0.077 | partial | quality pass exceeded capped runner or produced no JSON |
| P1/P2 | DeepSeek-R1 Distill 32B | `deepseek-r1:32b` | 11.0 | 19.7 | 50.0% (21/42) | 0.063 | measured | MIT |
| P1/P2 | Magistral Small 24B | `magistral:24b` | 15.2 | 14.7 | timeout/no JSON | 0.107 | partial | quality pass exceeded capped runner or produced no JSON |
| P1/P2 | Mistral Small 3.2 24B | `mistral-small:24b` | 15.2 | 14.7 | 54.8% (23/42) | 0.107 | measured | Apache-2.0 |
| P1/P2 | Phi-4 reasoning plus | `phi4-reasoning:plus` | 19.8 | 11.6 | 57.1% (24/42)† | 0.139 | measured† | quality completed later in the operating-profiles run (profiles-summary-halo-b.md) |
| P1 | GLM-4.5-Air | `MichelRosselli/GLM-4.5-Air:Q4_K_M, MichelRosselli/GLM-4.5-Air:Q2_K` | - | - | - | - | skipped | preferred Q4 community tag is 73GB and Q2 fallback is not comparable to Q4/Q8 frontier; skipped to keep P1/P2 sweep bounded |
| P1/P2 | DeepSeek-R1 Distill 70B | `deepseek-r1:70b` | - | - | - | - | skipped | lower-priority 70B reasoning tag would add large pull plus likely long 42Q quality runtime after 32B reasoning candidate was measured |
| P1/P2 | Falcon-H1 34B | `falcon-h1:34b-q4_K_M, falcon-h1:34b` | - | - | - | - | skipped | both attempted Ollama manifests returned pull model manifest: file does not exist |

## Recommendation

No swept candidate beats the existing Gemma recommendation on the combined quality/speed/VRAM/energy default criteria. Keep `gemma4:26b-a4b-it-q8_0` as the balanced local/default, `gemma4:26b` Q4_K_M as throughput/demo default, `gemma4:31b-it-qat` as quality-only local rung, and `gpt-oss:120b` as the 120B capacity/reference.

## Skips And Caps

- GLM-4.5-Air: skipped because the practical Q4 community tag is 73GB and the Q2 fallback is not comparable to the Q4/Q8 frontier.
- DeepSeek-R1 70B: skipped because lower-priority 70B reasoning quality would add a large pull and likely long 42Q runtime after DeepSeek-R1 32B was measured.
- Falcon-H1 34B: both attempted Ollama manifests failed (`pull model manifest: file does not exist`).
- EXAONE/OpenThinker/Magistral/Phi quality: speed and power captured here; quality was timeout/no JSON under the capped P1/P2 runner. † EXAONE (50.0%, 21/42) and Phi-4 (57.1%, 24/42) were **completed later** in the operating-profiles run (see `profiles-summary-halo-b.md`); OpenThinker/Magistral quality remain pending. EXAONE is research-only/non-commercial and not default-eligible.
