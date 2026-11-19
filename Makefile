.PHONY: test run deb clean

test:
	python3 -I -m unittest discover -s tests -v

run:
	python3 -I run.py

deb: test
	packaging/build-deb.sh

clean:
	rm -rf dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
