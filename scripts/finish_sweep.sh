#!/bin/bash
#SBATCH -p low
#SBATCH --cpus-per-task=24
#SBATCH --mem=160G
#SBATCH -t 12:00:00
# Unattended end of the PRC sweep (submit with --dependency=afterany:<sweep job ids>):
# merge every run, pick the best, train the final model (1 GPU), infer it on the sheet (1 GPU), route ablation and
# exercise ablation of every run, the 10 exercises on the best run (checked) and from the scan + PDF alone (no BD TOPO,
# no OSM), report, git commit, bundle. A failed step is logged and the next steps go on.
set -uo pipefail
cd "$SLURM_SUBMIT_DIR"
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK
O=outputs/hackathon100k; P=$O/prc; PDF=examples/exercise_1-to-10-1.pdf; PY=.venv/bin/python
TAGS="finetuned s12k s24k s24k_lr1e-4 s12k_b32"
step() { echo; echo "=== $(date '+%F %T') $*"; }
run() { "$@"; local rc=$?; [ $rc -ne 0 ] && echo "!!! FAILED ($rc): $*"; return 0; }

step "merge the sweep runs"
for t in s12k s24k s24k_lr1e-4 s12k_b32; do
  if ls $P/pred_${t}_fold{0,1,2,3}.npy >/dev/null 2>&1; then run $PY scripts/prc_unet.py merge --out $P --name $t
  else echo "run $t incomplete: $(ls $P/pred_${t}_fold*.npy 2>/dev/null | wc -l)/4 folds"; fi
done

step "select the best run"
read BEST STEPS LR BATCH < <($PY scripts/finish_sweep.py select --prc $P --tags $TAGS)
echo "best: $BEST (steps $STEPS, lr $LR, batch $BATCH)"

step "final model on every block (1 GPU)"
run sbatch --wait -J prc_final -o $P/logs/final_$BEST.log scripts/prc_slurm.sh finetune $P --fold -1 --tag $BEST \
    --steps $STEPS --lr $LR --batch $BATCH
step "inference of the final model on the sheet (1 GPU, no labels)"
run sbatch --wait -J prc_infer -o $P/logs/infer_$BEST.log scripts/prc_slurm.sh infer $P --tif examples/Hackathon_100K.tif \
    --model $P/prc_${BEST}_final.pt --dest $O/prc_final_infer

ALONE=(); for t in $TAGS; do [ -f $P/$t/unet_oof.tif ] && ALONE+=(--alone "PRC $t alone=$P/$t"); done
step "route ablation of every run"
run $PY -u scripts/scan_ablation.py --data examples --cache $O/cache --learn $O/learn_full "${ALONE[@]}" --out $O/ablation_sweep
step "exercise ablation of every run (BD TOPO and OSM)"
run $PY -u scripts/exercise_ablation.py --data examples --pdf $PDF --cache $O/cache --builtup $O/pdf_exercises/builtup_areas.geojson \
    --corrector "fine-tuned PRC features=$O/learn_prc" "${ALONE[@]}" --out $O/exercise_ablation_sweep

step "the 10 exercises on the best run (out of fold, checked)"
run $PY -u -m coa_net pdf-exercises --data examples --pdf $PDF --network prc-alone --unet $P/$BEST --cache $O/cache \
    --out $O/pdf_exercises_prc_best
step "the 10 exercises from the scan and the PDF alone (no BD TOPO, no OSM)"
mkdir -p $O/data_tif_only && ln -sf "$PWD/examples/Hackathon_100K.tif" $O/data_tif_only/
run $PY -u -m coa_net pdf-exercises --data $O/data_tif_only --pdf $PDF --network prc-alone --unet $O/prc_final_infer \
    --cache $O/cache --out $O/pdf_exercises_tif_only

step "report"
run $PY scripts/finish_sweep.py report --root $O --doc docs/prc_sweep_results.md

step "tests, commit, bundle"
$PY -m pytest -q 2>&1 | tail -1
git add docs scripts src README.md .gitignore
git -c user.name="nouhaila_aitbella" -c user.email="zigbou111@gmail.com" commit -q -m "PRC fine-tuning sweep: best run, final model, exercises without BD TOPO or OSM

Results of scripts/finish_sweep.sh (run unattended on Slurm): every run merged out of fold, the best one
($BEST) trained on every block as the final model, its routes against BD TOPO and OSM, the 10 PDF exercises
on its network and from the scan and the PDF alone. Report: docs/prc_sweep_results.md.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" && echo committed
git branch -f main scan-roads
git bundle create ~/bouzid/Hackathon_AMIAD_main.bundle main scan-roads && git bundle verify ~/bouzid/Hackathon_AMIAD_main.bundle 2>&1 | tail -1
git log --oneline -3 main
step "done"
