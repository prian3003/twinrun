.PHONY: all test check sandbox regressions demo install clean

# One command. Runs the self-check, then points twinrun at its own last commit.
all: test sandbox check

test:
	python3 test_twinrun.py

# Dogfood: verify this repo's most recent commit with the tool itself.
check:
	python3 -m twinrun.cli . --base HEAD~1 --head HEAD || true

# A repo whose answers are known: half the commits change behaviour, half don't.
# Pass a path to keep it around: make sandbox DIR=/tmp/shop
DIR ?=
sandbox:
	python3 sandbox.py $(DIR)

# Regressions a real repository already admitted to. A commit someone later ran
# `git revert` on is a pair whose label is the maintainers' own verdict.
# make regressions REPO=~/src/click  (add ARGS="--blame --since 2019" for more)
ARGS ?=
regressions:
	python3 regressions.py $(REPO) $(ARGS)

# make demo REPO=~/some/repo BASE=main HEAD=my-branch
REPO ?= .
BASE ?= HEAD~1
HEAD ?= HEAD
demo:
	python3 -m twinrun.cli $(REPO) --base $(BASE) --head $(HEAD)

install:
	python3 -m pip install -e .

clean:
	rm -rf __pycache__ */__pycache__ *.egg-info build dist
