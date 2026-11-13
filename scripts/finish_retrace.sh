#!/bin/bash
#SBATCH -p low
#SBATCH --cpus-per-task=24
#SBATCH --mem=180G
#SBATCH -t 08:00:00
# Evaluation of the tracing variants in outputs/hackathon100k/retrace (examples data only): routes, the 10 exercises
# against BD TOPO and OSM, mismatch and gaps, report.
set -uo pipefail
cd "$SLURM_SUBMIT_DIR"
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK
O=outputs/hackathon100k; R=$O/retrace; PDF=examples/exercise_1-to-10-1.pdf; PY=.venv/bin/python
B=$O/pdf_exercises/builtup_areas.geojson
VARIANTS="base hyst0.3 hyst0.2 bridge150 bridge300 hyst0.3_bridge150"
step() { echo; echo "=== $(date '+%F %T') $*"; }
run() { "$@"; local rc=$?; [ $rc -ne 0 ] && echo "!!! FAILED ($rc): $*"; return 0; }
ALONE=(); for v in $VARIANTS; do ALONE+=(--alone "PRC $v=$R/$v"); done
step "routes"
run $PY -u scripts/scan_ablation.py --data examples --cache $O/cache --learn $O/learn_full "${ALONE[@]}" --out $O/ablation_retrace
step "exercises against BD TOPO and OSM"
run $PY -u scripts/exercise_ablation.py --data examples --pdf $PDF --cache $O/cache --builtup $B "${ALONE[@]}" --out $O/exercise_ablation_retrace
for v in $VARIANTS; do
  step "mismatch and gaps: $v"
  run $PY -u scripts/mismatch_map.py --data examples --pdf $PDF --cache $O/cache --builtup $B --unet $R/$v --out $O/mismatch_retrace_$v
done
step "report"
run $PY scripts/retrace_report.py --root $O --variants $VARIANTS --doc docs/prc_retrace_results.md
step "done"
