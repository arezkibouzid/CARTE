#!/bin/bash
#SBATCH -p low
#SBATCH --cpus-per-task=24
#SBATCH --mem=180G
#SBATCH -t 06:00:00
# Experiment 1: the calibrated mix12k score; bridging with calibrated thresholds (checked against BD TOPO / OSM), routes,
# exercises, uncertainty and the operator's threshold on the calibrated scale. Examples data only.
set -uo pipefail
cd "$SLURM_SUBMIT_DIR"
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK
O=outputs/hackathon100k; P=$O/prc; PDF=examples/exercise_1-to-10-1.pdf; PY=.venv/bin/python; B=$O/pdf_exercises/builtup_areas.geojson
step() { echo; echo "=== $(date '+%F %T') $*"; }
run() { "$@"; local rc=$?; [ $rc -ne 0 ] && echo "!!! FAILED ($rc): $*"; return 0; }
step "bridges with calibrated thresholds (added to the base network)"
run $PY -u scripts/retrace.py --raster $P/mix12k_cal --from-network $O/retrace/base/network.pkl --out $O/retrace_cal \
    --variant "bridgecal_15_30=bridge=150,bridge_min_p=0.15,bridge_mean_p=0.3" \
    --variant "bridgecal_30_50=bridge=150,bridge_min_p=0.3,bridge_mean_p=0.5" \
    --variant "bridgecal_40_60=bridge=150,bridge_min_p=0.4,bridge_mean_p=0.6"
for v in 15_30 30_50 40_60; do
  step "check the bridges: $v"
  run $PY -u scripts/check_bridges.py --data examples --network $O/retrace_cal/bridgecal_$v --out $O/bridge_check_cal_$v
done
ALONE=(--alone "PRC base=$O/retrace/base"); for v in 15_30 30_50 40_60; do ALONE+=(--alone "PRC bridgecal_$v=$O/retrace_cal/bridgecal_$v"); done
step "routes"
run $PY -u scripts/scan_ablation.py --data examples --cache $O/cache --learn $O/learn_full "${ALONE[@]}" --out $O/ablation_cal
step "exercises against BD TOPO and OSM"
run $PY -u scripts/exercise_ablation.py --data examples --pdf $PDF --cache $O/cache --builtup $B "${ALONE[@]}" --out $O/exercise_ablation_cal
step "uncertainty and the operator's threshold on the calibrated scale"
run $PY -u scripts/uncertainty_map.py --data examples --pdf $PDF --cache $O/cache --prc $P --run mix12k_cal --builtup $B \
    --mismatch $O/mismatch_prc_mix_best --out $O/uncertainty_mix12k_cal --map-tau 0.5 --taus 0.2 0.3 0.4 0.5 0.6
step "report"
(cd scripts && ../$PY calib_loss_report.py calibration --root ../$O --doc ../docs/prc_calibration.md) || echo "!!! FAILED: report"
step "done"
