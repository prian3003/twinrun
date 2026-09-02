.PHONY: all test check demo install clean

# One command. Runs the self-check, then points twinrun at its own last commit.
all: test check

test:
	python3 test_twinrun.py

# Dogfood: verify this repo's most recent commit with the tool itself.
check:
	python3 -m twinrun.cli . --base HEAD~1 --head HEAD || true

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
