PYTHON ?= python

.PHONY: demo day test guard check

demo:
	$(PYTHON) -m demo

day:
	$(PYTHON) -m simulation.run_day --seed 7 --date 2026-11-24

test:
	$(PYTHON) -m pytest -q

guard:
	$(PYTHON) tools/privacy_guard.py

check: guard test demo
