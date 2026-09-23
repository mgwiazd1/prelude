.PHONY: demo run test

demo:
	python3 -m prelude demo

run:
	python3 -m prelude snapshot

test:
	python3 -m pytest tests/ -q
