# Halo-B candidate sweep skips

- `MichelRosselli/GLM-4.5-Air:Q4_K_M` / `:Q2_K`: skipped in the capped P1/P2 runner because the preferred Q4 community tag is 73GB and the Q2 fallback is not comparable to the Q4/Q8 frontier; measure separately if GLM becomes a priority.
- `deepseek-r1:70b`: skipped in the capped P1/P2 runner because the lower-priority 70B reasoning tag would add a large pull plus likely long 42Q reasoning-quality runtime after the 32B reasoning candidate is covered.
