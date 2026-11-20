#!/usr/bin/env bash
# Whether a push to main brings a tree that Build and Test has already passed on its pull request.
#
# Writes tested=true|false to $GITHUB_OUTPUT (or stdout). A batch merges by rebase, which gives main
# new commits with the tree the pull request's run tested, so that run stands for this push when
# all of these hold:
#   1. the pushed commit belongs to a pull request merged into this repository's main, from a
#      branch of this repository (a fork's run could write any record);
#   2. a run of Build and Test on the pull request's head, from this repository, concluded success
#      and its Changes job recorded (the `tested` artifact) that it tested this pull request's merge
#      into main at BEFORE, main before the push, with exactly the pushed commit's tree;
#   3. Build and Test passed on the push of BEFORE, and the latest completed full run on main
#      passed: a nightly run, a run by hand, or a push that ran every job (its Changes job records
#      which, in the `full` artifact). A pull request's run skips the jobs its change does not
#      need, which then stand as they were on BEFORE, and a job can turn red with no change at all
#      (a service image, an upstream project); this is what says they were green. A failed
#      nightly run makes the next push a full one, whose success stands for it.
# Anything else, a failed lookup included, gives tested=false, and the push runs every job, whose
# success makes the next push eligible again.
#
# Needs GH_TOKEN (with actions and pull-requests read), REPOSITORY, SHA and BEFORE, and a clone
# that has SHA.
set -uo pipefail

RUNS="repos/${REPOSITORY}/actions/workflows/build-and-test.yml/runs"

verdict() {
  echo "$2"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then echo "tested=$1" >> "$GITHUB_OUTPUT"; else echo "tested=$1"; fi
  exit 0
}

pull=$(gh api "repos/${REPOSITORY}/commits/${SHA}/pulls" --jq "[.[] | select(.merged_at != null
  and .base.ref == \"main\" and .base.repo.full_name == \"${REPOSITORY}\")][0]
  | \"\(.number) \(.head.sha) \(.head.repo.full_name)\"") \
  || verdict false "Could not look up the pull request of ${SHA}"
read -r number head head_repo <<< "$pull"
case "$head" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
  *) verdict false "${SHA} belongs to no merged pull request into main" ;;
esac
[ "$head_repo" = "$REPOSITORY" ] || verdict false "#${number} came from ${head_repo}, not a branch of ${REPOSITORY}"

tree=$(git rev-parse "${SHA}^{tree}") || verdict false "Could not read the tree of ${SHA}"

runs=$(gh api "${RUNS}?head_sha=${head}&event=pull_request&status=success&per_page=100" \
  --jq ".workflow_runs[] | select(.head_repository.full_name == \"${REPOSITORY}\") | .id") \
  || verdict false "Could not list the runs of #${number}"

folder=$(mktemp -d "${RUNNER_TEMP:-/tmp}/tested.XXXXXX")
trap 'rm -rf "$folder"' EXIT
found=''
for run in $runs; do
  gh run download "$run" -R "$REPOSITORY" -n tested -D "${folder}/${run}" > /dev/null 2>&1 || continue
  declare -A seen=()
  while IFS='=' read -r key value; do seen[$key]=$value; done < "${folder}/${run}/tested.txt"
  if [ "${seen[number]:-}" = "$number" ] && [ "${seen[head]:-}" = "$head" ] \
    && [ "${seen[base]:-}" = "$BEFORE" ] && [ "${seen[base_ref]:-}" = main ] \
    && [ "${seen[base_repo]:-}" = "$REPOSITORY" ] && [ "${seen[tree]:-}" = "$tree" ]; then
    found=$run
    every_job=${seen[every_job]:-false}
    break
  fi
  unset seen
done
[ -n "$found" ] || verdict false "no run of Build and Test on ${head} recorded #${number} testing ${tree} on ${BEFORE}"

before_run=$(gh api "${RUNS}?head_sha=${BEFORE}&event=push&branch=main&per_page=1" \
  --jq '.workflow_runs[0].conclusion // "none"') || verdict false "Could not look up the run on ${BEFORE}"
[ "$before_run" = success ] || verdict false "main before the push, ${BEFORE}, did not pass Build and Test (${before_run})"

latest=$(gh api "${RUNS}?branch=main&status=completed&per_page=50" --jq ".workflow_runs[]
  | select(.head_repository.full_name == \"${REPOSITORY}\")
  | select(.event == \"push\" or .event == \"schedule\" or .event == \"workflow_dispatch\")
  | \"\(.id) \(.event) \(.conclusion)\"") || verdict false "Could not list the runs on main"
full_run=''
while read -r id event conclusion; do
  [ -n "$id" ] || continue
  if [ "$event" = push ]; then
    gh run download "$id" -R "$REPOSITORY" -n full -D "${folder}/full-${id}" > /dev/null 2>&1 || continue
    grep -qx 'full=true' "${folder}/full-${id}/full.txt" || continue
  fi
  full_run=$id
  full_conclusion=$conclusion
  break
done <<< "$latest"
[ -n "$full_run" ] || verdict false "no full run of Build and Test among the latest on main"
[ "$full_conclusion" = success ] \
  || verdict false "the latest full run of Build and Test on main, ${full_run}, did not pass (${full_conclusion})"

verdict true "run ${found} of #${number} passed Build and Test on ${tree}, which main now has, on ${BEFORE}, \
which passed, and the latest full run on main, ${full_run}, passed (every job: ${every_job})"
