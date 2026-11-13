#!/bin/bash
#SBATCH -p low
#SBATCH --cpus-per-task=24
#SBATCH --mem=180G
#SBATCH -t 08:00:00
# Experiment 2: road loss ablation against mix12k. Examples data only.
set -uo pipefail
cd "$SLURM_SUBMIT_DIR"
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK
O=outputs/hackathon100k; P=$O/prc; PDF=examples/exercise_1-to-10-1.pdf; PY=.venv/bin/python; B=$O/pdf_exercises/builtup_areas.geojson
NEW="mix12k_nodice mix12k_dice05 mix12k_nodice_w1"; ALL="mix12k $NEW"
step() { echo; echo "=== $(date '+%F %T') $*"; }
run() { "$@"; local rc=$?; [ $rc -ne 0 ] && echo "!!! FAILED ($rc): $*"; return 0; }
step "merge"
for t in $NEW; do
  if ls $P/pred_${t}_fold{0,1,2,3}.npy >/dev/null 2>&1; then run $PY scripts/prc_unet.py merge --out $P --name $t
  else echo "run $t incomplete: $(ls $P/pred_${t}_fold*.npy 2>/dev/null | wc -l)/4 folds"; fi
done
for t in $ALL; do step "reliability: $t"; run $PY scripts/calibrate.py --prc $P --run $t --reliability-only --out $P/$t; done
ALONE=(); for t in $ALL; do [ -f $P/$t/unet_oof.tif ] && ALONE+=(--alone "PRC $t=$P/$t"); done
step "routes"
run $PY -u scripts/scan_ablation.py --data examples --cache $O/cache --learn $O/learn_full "${ALONE[@]}" --out $O/ablation_loss
step "exercises against BD TOPO and OSM"
run $PY -u scripts/exercise_ablation.py --data examples --pdf $PDF --cache $O/cache --builtup $B "${ALONE[@]}" --out $O/exercise_ablation_loss
for t in $ALL; do
  [ -f $P/$t/unet_oof.tif ] || continue
  step "missing roads: $t"
  run $PY -u scripts/mismatch_map.py --data examples --pdf $PDF --cache $O/cache --builtup $B --unet $P/$t --out $O/mismatch_loss_$t
  run $PY scripts/gap_runs.py --mismatch $O/mismatch_loss_$t --network $P/$t --out $O/mismatch_loss_$t/gap_runs.json
done
step "report"
(cd scripts && ../$PY calib_loss_report.py loss --root ../$O --runs $ALL --doc ../docs/prc_loss_results.md) || echo "!!! FAILED: report"
step "done"
