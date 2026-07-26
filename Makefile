PYTHON ?= /opt/anaconda3/bin/python3
PACKAGES ?=
PACKAGE_ARGS = $(foreach package,$(PACKAGES),--package $(package))

.PHONY: fetch harvest scaffold render lint test check publish validate \
	validate-core validate-packages validate-plugin-compile \
	validate-plugin-runtime all

fetch:
	$(PYTHON) scripts/fetch_upstream.py

harvest:
	$(PYTHON) scripts/harvest_stata_help.py

scaffold:
	$(PYTHON) scripts/scaffold_content.py

render:
	$(PYTHON) scripts/render_skills.py

lint:
	$(PYTHON) scripts/lint_skill_pack.py

test:
	$(PYTHON) -m unittest discover -s tests -v

check: lint test

publish:
	$(PYTHON) scripts/publish_local.py

validate:
	$(PYTHON) scripts/validate_skill_pack.py --suite default

validate-core:
	$(PYTHON) scripts/validate_skill_pack.py --suite core

validate-packages:
	$(PYTHON) scripts/validate_skill_pack.py --suite packages $(PACKAGE_ARGS)

validate-plugin-compile:
	$(PYTHON) scripts/validate_skill_pack.py --suite plugin-compile

validate-plugin-runtime:
	$(PYTHON) scripts/validate_skill_pack.py --suite plugin-runtime

all: fetch harvest scaffold render lint
