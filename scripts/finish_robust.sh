#!/bin/bash
#SBATCH -p low
#SBATCH --cpus-per-task=24
#SBATCH --mem=180G
#SBATCH -t 08:00:00
# Evaluation of the robustness runs (clDice, uncertainty-guided sampling) against mix12k, on the examples data only.
set -uo pipefail
cd "$SLURM_SUBMIT_DIR"
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK
O=outputs/hackathon100k; P=$O/prc; PDF=examples/exercise_1-to-10-1.pdf; PY=.venv/bin/python; REF=mix12k
B=$O/pdf_exercises/builtup_areas.geojson
TAGS="mix12k_cld mix12k_hard mix12k_cld_hard"
step() { echo; echo "=== $(date '+%F %T') $*"; }
run() { "$@"; local rc=$?; [ $rc -ne 0 ] && echo "!!! FAILED ($rc): $*"; return 0; }

step "merge"
for t in $TAGS; do
  if ls $P/pred_${t}_fold{0,1,2,3}.npy >/dev/null 2>&1; then run $PY scripts/prc_unet.py merge --out $P --name $t
  else echo "run $t incomplete: $(ls $P/pred_${t}_fold*.npy 2>/dev/null | wc -l)/4 folds"; fi
done
ALONE=(); RASTERS=(); for t in $REF $TAGS; do [ -f $P/$t/unet_oof.tif ] && { ALONE+=(--alone "PRC $t=$P/$t"); RASTERS+=(--raster "$t=$P/$t"); }; done
step "routes"
run $PY -u scripts/scan_ablation.py --data examples --cache $O/cache --learn $O/learn_full "${ALONE[@]}" --out $O/ablation_robust
step "exercises against BD TOPO and OSM"
run $PY -u scripts/exercise_ablation.py --data examples --pdf $PDF --cache $O/cache --builtup $B "${ALONE[@]}" --out $O/exercise_ablation_robust
for t in $REF $TAGS; do
  [ -f $P/$t/unet_oof.tif ] || continue
  step "mismatch and gaps: $t"
  run $PY -u scripts/mismatch_map.py --data examples --pdf $PDF --cache $O/cache --builtup $B --unet $P/$t --out $O/mismatch_robust_$t
  step "uncertainty: $t"
  run $PY -u scripts/uncertainty_map.py --data examples --pdf $PDF --cache $O/cache --prc $P --run $t --builtup $B \
      --mismatch $O/mismatch_robust_$t --out $O/uncertainty_robust_$t --no-sweep
done
step "CPU pipeline timing (network size)"
run $PY -u scripts/time_pipeline.py --pdf $PDF --builtup $B "${RASTERS[@]}" --out $O/pipeline_timing_robust
step "report"
(cd scripts && ../$PY robust_report.py --root ../$O --ref $REF --tags $TAGS --doc ../docs/prc_robust_results.md) || echo "!!! FAILED: report"
step "tests, commit, bundle"
$PY -m pytest -q 2>&1 | tail -1
git add docs scripts src
git -c user.name="nouhaila_aitbella" -c user.email="zigbou111@gmail.com" commit -q -m "PRC robustness runs: clDice connectivity loss, uncertainty-guided sampling

prc_unet.py finetune --cldice W (soft-skeleton clDice on the road output) and --hard-cells MAP (a share of the
sheet's tiles from its most uncertain cells, label-free map, training blocks only). Three runs against mix12k,
evaluated on the examples data: pixels, training time, gaps, routes, the 10 exercises against BD TOPO and OSM,
uncertainty. Report: docs/prc_robust_results.md.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" && echo committed
git branch -f main scan-roads
git bundle create ~/bouzid/Hackathon_AMIAD_main.bundle main scan-roads && git bundle verify ~/bouzid/Hackathon_AMIAD_main.bundle 2>&1 | tail -1
step "done"
