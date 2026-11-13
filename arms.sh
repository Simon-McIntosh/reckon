#!/bin/bash
# Measure the node's gate tree by tree: the base arm runs the tests that
# import the changed modules as the base revision holds them, the head arm
# runs the same list at the node's head, and the negative control runs the
# node's own test over the base revision's code.
set -u
export TMPDIR=/tmp
R=/home/ITER/mcintos/.config/reckon/crew/runs/r-20261112T231336380925-a-landed-reading-follows-the-primary-branch
W=/home/ITER/mcintos/Code/.reckon-worktrees/reckon-c8f839407e49/1fb615a0-2801-4418-bbb4-c2914719e520/a-landed-reading-follows-the-primary-branch
PY=/home/ITER/mcintos/Code/reckon/.venv/bin/python
BASE=3886d8d45c827b58f638ee2a94cca71d601231b3
NODE_TEST=tests/test_a_landed_reading_follows_the_primary_branch.py
D=$(mktemp -d /tmp/"$USER"-arms-XXXX)
cleanup() { rm -rf "$D"; }
trap cleanup EXIT
git -C "$W" archive "$BASE" | tar -x -C "$D"
HEAD_SHA=$(git -C "$W" rev-parse HEAD)

BASE_TESTS=$(cd "$D" && grep -rl "recovery_classification" tests/ --include='test_*.py' | sort)
{
  echo "# revision: $BASE (tree: $D, base arm)"
  echo "# module: $(cd "$D" && PYTHONPATH=. "$PY" -c 'import reckon.crew.recovery_classification as m; print(m.__file__)')"
  echo "# cwd: $D"
  echo "# command: cd $D && PYTHONPATH=. $PY -m pytest -p no:cacheprovider -q -n 8 $BASE_TESTS"
  cd "$D" && PYTHONPATH=. "$PY" -m pytest -p no:cacheprovider -q -n 8 $BASE_TESTS 2>&1
  echo "EXIT=$?"
} > "$R/base-arm.log"

cp "$W/$NODE_TEST" "$D/$NODE_TEST"
{
  echo "# revision: $BASE (tree: $D, negative control: the node's test over the base code)"
  echo "# module: $(cd "$D" && PYTHONPATH=. "$PY" -c 'import reckon.crew.recovery_classification as m; print(m.__file__)')"
  echo "# cwd: $D"
  echo "# command: cd $D && PYTHONPATH=. $PY -m pytest -p no:cacheprovider -q $NODE_TEST"
  cd "$D" && PYTHONPATH=. "$PY" -m pytest -p no:cacheprovider -q "$NODE_TEST" 2>&1
  echo "EXIT=$?"
} > "$R/negative-control.log"

HEAD_TESTS=$(cd "$W" && grep -rl "recovery_classification" tests/ --include='test_*.py' | sort)
{
  echo "# revision: $HEAD_SHA (tree: $W, head arm)"
  echo "# module: $(cd "$W" && PYTHONPATH=. "$PY" -c 'import reckon.crew.recovery_classification as m; print(m.__file__)')"
  echo "# cwd: $W"
  echo "# command: cd $W && PYTHONPATH=. $PY -m pytest -p no:cacheprovider -q -n 8 $HEAD_TESTS"
  cd "$W" && PYTHONPATH=. "$PY" -m pytest -p no:cacheprovider -q -n 8 $HEAD_TESTS 2>&1
  echo "EXIT=$?"
} > "$R/head-arm.log"