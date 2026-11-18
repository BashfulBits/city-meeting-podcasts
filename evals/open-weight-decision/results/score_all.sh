#!/bin/zsh
# Score every Phase 3 answer file for one split: score_all.sh dev|holdout|all
# Writes results/2026-11-18-summary-<split>.json and prints its path (stdout is not the JSON).
# Run from the repository root. `NAME` is the grouped (main) run; `NAME+single` replaces its chapter
# answers with the one-question-per-request run, so its chapter lane is the single-question number.
S=$1; R=evals/open-weight-decision/results; D=2026-11-18
args=()
# add NAME FILE [EXTRA]: FILE and EXTRA hold `@` where the split goes; `all` pools dev and holdout.
# A missing file stops the run, except for the models in OPTIONAL (dev-only or no single-question
# run), which are skipped.
OPTIONAL=" arsenelupinstral glide-9b+single karpo-9b-fp8-rtx karpo-9b-bf16-rtx karpo-9b-fp8-rtx+single karpo-9b-bf16-rtx+single "  # the last two: dev-only speed runs

add() {
  local sp files="" f x
  for sp in ${=SPLITS}; do
    for f in "$2" $3; do
      x="$R/$D-${f//@/$sp}"
      if [ ! -f "$x" ]; then
        [[ "$OPTIONAL" == *" $1 "* ]] && return
        echo "missing answer file: $x" >&2; exit 1
      fi
      files+="${files:+,}$x"
    done
  done
  args+=("$1=$files")
}
[ $S = all ] && SPLITS="dev holdout" || SPLITS=$S
add perplexity-v1.3 perplexity-v1.3-@-answers.json
add gemini-decision-1 gemini-decision-1-@-answers.json
for m in liquid-d1-2b liquid-d1-3b minicpm5-2b-jev arsenelupinstral; do add $m $m-@-answers.json; done
for m in karpo-9b-fp8 karpo-9b-bf16 karpo-12b-fp8 karpo-12b-bf16 karpo-31b-fp8 karpo-31b-bf16 glide-9b elephant-1-lean copernicus-31b-bf16 karpo-9b-fp8-rtx karpo-9b-bf16-rtx; do
  add $m @-$m-@-main.json
  add $m+single @-$m-@-main.json @-$m-@-chapters-single.json
done
ref=""; for sp in ${=SPLITS}; do ref+="${ref:+,}$R/$D-perplexity-v1.3-$sp-answers.json"; done
python3 -m scripts.open_weight_eval score --split $S --reference "$ref" \
  --reps ${REPS:-2000} --answers "${args[@]}" --out $R/$D-summary-$S.json > /dev/null && echo $R/$D-summary-$S.json
