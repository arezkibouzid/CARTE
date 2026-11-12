#!/bin/bash
#SBATCH -p low
#SBATCH --cpus-per-task=24
#SBATCH --mem=160G
#SBATCH -t 12:00:00
# Unattended evaluation of the PRC runs trained with Scenario 1 (submit with --dependency=afterany:<run job ids>).
# Every evaluation uses the examples/ data only (the Hackathon sheet); examples/otherdata is training data.
set -uo pipefail
cd "$SLURM_SUBMIT_DIR"
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK
O=outputs/hackathon100k; P=$O/prc; PDF=examples/exercise_1-to-10-1.pdf; PY=.venv/bin/python
MIX="mix6k mix12k mix24k_lr1e-4"; TAGS="finetuned $MIX"
step() { echo; echo "=== $(date '+%F %T') $*"; }
run() { "$@"; local rc=$?; [ $rc -ne 0 ] && echo "!!! FAILED ($rc): $*"; return 0; }

step "merge the runs"
for t in $MIX; do
  if ls $P/pred_${t}_fold{0,1,2,3}.npy >/dev/null 2>&1; then run $PY scripts/prc_unet.py merge --out $P --name $t
  else echo "run $t incomplete: $(ls $P/pred_${t}_fold*.npy 2>/dev/null | wc -l)/4 folds"; fi
done
step "select the best run (baseline included)"
read BEST STEPS LR BATCH < <($PY scripts/finish_sweep.py select --prc $P --tags $TAGS)
cp $P/best.json $P/best_mix.json
echo "best: $BEST (steps $STEPS, lr $LR, batch $BATCH)"

ALONE=(); for t in $TAGS; do [ -f $P/$t/unet_oof.tif ] && ALONE+=(--alone "PRC $t=$P/$t"); done
step "route ablation (examples only)"
run $PY -u scripts/scan_ablation.py --data examples --cache $O/cache --learn $O/learn_full "${ALONE[@]}" --out $O/ablation_mix
step "exercise ablation (examples only, BD TOPO and OSM)"
run $PY -u scripts/exercise_ablation.py --data examples --pdf $PDF --cache $O/cache --builtup $O/pdf_exercises/builtup_areas.geojson \
    "${ALONE[@]}" --out $O/exercise_ablation_mix
step "the 10 exercises on the best network"
run $PY -u -m coa_net pdf-exercises --data examples --pdf $PDF --network prc-alone --unet $P/$BEST --cache $O/cache --out $O/pdf_exercises_prc_mix_best
step "mismatch analysis of the best network"
run $PY -u scripts/mismatch_map.py --data examples --pdf $PDF --cache $O/cache --builtup $O/pdf_exercises/builtup_areas.geojson \
    --unet $P/$BEST --out $O/mismatch_prc_mix_best

if [[ "$BEST" == mix* ]]; then
  step "final model of $BEST: every Hackathon block + Scenario 1 (1 GPU), inference, exercises from the scan and the PDF alone"
  run sbatch --wait -J prc_final_mix -o $P/logs/final_$BEST.log scripts/prc_slurm.sh finetune $P --fold -1 --tag $BEST \
      --steps $STEPS --lr $LR --batch $BATCH --extra S1 --extra-share 0.5
  run sbatch --wait -J prc_infer_mix -o $P/logs/infer_$BEST.log scripts/prc_slurm.sh infer $P --tif examples/Hackathon_100K.tif \
      --model $P/prc_${BEST}_final.pt --dest $O/prc_final_infer_mix
  run $PY -u -m coa_net pdf-exercises --data $O/data_tif_only --pdf $PDF --network prc-alone --unet $O/prc_final_infer_mix \
      --cache $O/cache --out $O/pdf_exercises_tif_only_mix
else
  echo "the baseline stays best: no new final model"
fi

step "report"
(cd scripts && ../$PY finish_mix.py --root ../$O --tags $TAGS --best $BEST --doc ../docs/prc_mix_results.md) || echo "!!! FAILED: report"

step "tests, commit, bundle"
$PY -m pytest -q 2>&1 | tail -1
git add docs scripts
git -c user.name="nouhaila_aitbella" -c user.email="zigbou111@gmail.com" commit -q -m "PRC trained with Scenario 1: runs, evaluation on the Hackathon sheet

Three fine-tuning runs with half of the training tiles from Scenario 1 (examples/otherdata), evaluated
out of fold on the Hackathon sheet only: pixel scores, routes, the 10 PDF exercises against BD TOPO and
OSM, mismatch analysis. Best: $BEST. Report: docs/prc_mix_results.md.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" && echo committed
git branch -f main scan-roads
git bundle create ~/bouzid/Hackathon_AMIAD_main.bundle main scan-roads && git bundle verify ~/bouzid/Hackathon_AMIAD_main.bundle 2>&1 | tail -1
step "done"
