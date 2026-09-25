.PHONY: demo run test site

demo:
	python3 -m prelude demo

run:
	python3 -m prelude snapshot

test:
	python3 -m pytest tests/ -q

site:
	python3 -m prelude site
