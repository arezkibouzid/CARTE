### Planning: baselines and COA-Net

100 seeds; means over seeds.

| pipeline | road-valid | valid (all levels) | SPOF groups | reducible SPOF groups | groups with ASR | ASR/MSR time | resilience (veh.min) | exposure | travel time (veh.min) | network km | on time (sim) | weighted delay (sim) | reached lost element (sim) | design s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B0 | 95% | 1% | 100% | 95% | 0% | - | 177.0 | 0.00 | 676 | 29.9 | 100.0% | 42.4 | 24.3% | 0.03 |
| B1 | 100% | 1% | 100% | 95% | 0% | - | 175.3 | 0.00 | 670 | 29.0 | 100.0% | 42.8 | 23.2% | 0.00 |
| B2 | 98% | 25% | 47% | 43% | 100% | 1.15 | 125.2 | 0.00 | 694 | 57.0 | 100.0% | 41.1 | 24.1% | 0.01 |
| B3 | 93% | 86% | 6% | 2% | 100% | 1.12 | 79.1 | 0.00 | 694 | 63.0 | 100.0% | 41.4 | 24.1% | 0.01 |
| COA-Net (greedy) | 100% | 100% | 4% | 0% | 96% | 1.14 | 77.1 | 0.00 | 708 | 47.1 | 100.0% | 39.3 | 17.6% | 0.00 |
| COA-Net | 100% | 100% | 1% | 0% | 99% | 1.16 | 71.5 | 0.00 | 700 | 46.1 | 100.0% | 38.3 | 17.4% | 0.09 |

95 % bootstrap intervals (over seeds):

- reducible_spof_groups: B0 [0.92, 0.98]; B1 [0.92, 0.98]; B2 [0.37, 0.50]; B3 [0.01, 0.04]; COA-Net (greedy) [0.00, 0.00]; COA-Net [0.00, 0.00]
- resilience: B0 [150.98, 208.83]; B1 [148.93, 207.41]; B2 [100.98, 154.38]; B3 [58.75, 102.24]; COA-Net (greedy) [56.55, 99.73]; COA-Net [51.37, 93.51]
- weighted_delay: B0 [36.09, 49.61]; B1 [36.28, 50.45]; B2 [35.05, 48.17]; B3 [35.26, 48.27]; COA-Net (greedy) [33.07, 46.45]; COA-Net [32.49, 45.24]

### Incident loop: with and without playbooks

| variant | incidents | modes | mean s | max s | valid after |
| --- | --- | --- | --- | --- | --- |
| with playbooks | 100 | logged 48, playbook 28, repair 24 | 0.17 | 1.47 | 92% |
| without playbooks (A7) | 100 | logged 48, repair 52 | 0.18 | 1.42 | 90% |
