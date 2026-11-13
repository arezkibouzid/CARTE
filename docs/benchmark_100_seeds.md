### Planning: baselines and COA-Net

100 seeds; means over seeds.

| pipeline | road-valid | valid (all levels) | SPOF groups | reducible SPOF groups | groups with ASR | ASR/MSR time | resilience (veh.min) | exposure | travel time (veh.min) | network km | on time (sim) | weighted delay (sim) | reached lost element (sim) | design s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B0 | 95% | 1% | 100% | 95% | 0% | - | 177.0 | 0.00 | 676 | 29.9 | 100.0% | 42.8 | 24.9% | 0.05 |
| B1 | 100% | 1% | 100% | 95% | 0% | - | 175.3 | 0.00 | 670 | 29.0 | 100.0% | 43.0 | 23.7% | 0.00 |
| COA-Net (greedy) | 100% | 100% | 4% | 0% | 96% | 1.14 | 77.1 | 0.00 | 708 | 47.1 | 100.0% | 39.6 | 18.1% | 0.00 |
| COA-Net | 100% | 100% | 1% | 0% | 99% | 1.16 | 71.5 | 0.00 | 700 | 46.1 | 100.0% | 38.5 | 17.9% | 0.15 |

95 % bootstrap intervals (over seeds):

- reducible_spof_groups: B0 [0.92, 0.98]; B1 [0.92, 0.98]; COA-Net (greedy) [0.00, 0.00]; COA-Net [0.00, 0.00]
- resilience: B0 [150.98, 208.83]; B1 [148.93, 207.41]; COA-Net (greedy) [56.55, 99.73]; COA-Net [51.37, 93.51]
- weighted_delay: B0 [36.43, 50.01]; B1 [36.58, 50.74]; COA-Net (greedy) [33.33, 46.56]; COA-Net [32.72, 45.44]

### Incident loop: with and without playbooks

| variant | incidents | modes | mean s | max s | valid after |
| --- | --- | --- | --- | --- | --- |
| with playbooks | 100 | logged 48, playbook 28, repair 24 | 0.35 | 2.81 | 92% |
| without playbooks (A7) | 100 | logged 48, repair 52 | 0.35 | 2.89 | 90% |

These figures count the 48 logged incidents, which take about 2 ms and score as valid. Over the 52 handled incidents, 85 % of plans are valid with playbooks and 81 % without, and the mean time is 0.35 to 0.67 s depending on machine load (methodology, section 11.3).
