#!/bin/bash
#SBATCH -p low
#SBATCH --cpus-per-task=24
#SBATCH --mem=180G
#SBATCH -t 08:00:00
# Evaluation of the inference methods of a run (submit with --dependency=afterany:<repredict and timing jobs>):
# merge, routes, exercises (BD TOPO and OSM), mismatch and gaps, uncertainty, CPU pipeline timing, report, commit, bundle.
set -uo pipefail
cd "$SLURM_SUBMIT_DIR"
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK
O=outputs/hackathon100k; P=$O/prc; PDF=examples/exercise_1-to-10-1.pdf; PY=.venv/bin/python; RUN=mix12k
B=$O/pdf_exercises/builtup_areas.geojson
METHODS="base tta overlap tta_overlap"
step() { echo; echo "=== $(date '+%F %T') $*"; }
run() { "$@"; local rc=$?; [ $rc -ne 0 ] && echo "!!! FAILED ($rc): $*"; return 0; }

step "merge"
for m in $METHODS; do run $PY scripts/prc_unet.py merge --out $P --name ${RUN}_$m; done
ALONE=(); RASTERS=(); for m in $METHODS; do [ -f $P/${RUN}_$m/unet_oof.tif ] && { ALONE+=(--alone "PRC ${RUN}_$m=$P/${RUN}_$m"); RASTERS+=(--raster "${RUN}_$m=$P/${RUN}_$m"); }; done
step "routes"
run $PY -u scripts/scan_ablation.py --data examples --cache $O/cache --learn $O/learn_full "${ALONE[@]}" --out $O/ablation_inference
step "exercises against BD TOPO and OSM"
run $PY -u scripts/exercise_ablation.py --data examples --pdf $PDF --cache $O/cache --builtup $B "${ALONE[@]}" --out $O/exercise_ablation_inference
for m in $METHODS; do
  step "mismatch and gaps: $m"
  run $PY -u scripts/mismatch_map.py --data examples --pdf $PDF --cache $O/cache --builtup $B --unet $P/${RUN}_$m --out $O/mismatch_inference_$m
  step "uncertainty: $m"
  run $PY -u scripts/uncertainty_map.py --data examples --pdf $PDF --cache $O/cache --prc $P --run ${RUN}_$m --builtup $B \
      --mismatch $O/mismatch_inference_$m --out $O/uncertainty_inference_$m --no-sweep
done
step "CPU pipeline timing"
run $PY -u scripts/time_pipeline.py --pdf $PDF --builtup $B "${RASTERS[@]}" --out $O/pipeline_timing
step "report"
run $PY scripts/inference_report.py --root $O --run $RUN --doc docs/prc_inference.md
step "tests, commit, bundle"
$PY -m pytest -q 2>&1 | tail -1
git add docs scripts src
git -c user.name="nouhaila_aitbella" -c user.email="zigbou111@gmail.com" commit -q -m "PRC inference methods (TTA, overlap blending), time and hardware report

prc_unet.py repredict / infer --method base|tta|overlap|tta_overlap (timed, CUDA-synchronised); the held-out
folds of $RUN predicted again with each method and evaluated on the examples data: pixel scores, routes, the
10 exercises against BD TOPO and OSM, gaps of the reconstructed network, uncertainty. Training and inference
time and hardware: docs/prc_inference.md.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" && echo committed
git branch -f main scan-roads
git bundle create ~/bouzid/Hackathon_AMIAD_main.bundle main scan-roads && git bundle verify ~/bouzid/Hackathon_AMIAD_main.bundle 2>&1 | tail -1
step "done"
