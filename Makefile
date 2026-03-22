PYTHON ?= /opt/anaconda3/bin/python3

.PHONY: fetch harvest scaffold render lint publish validate all

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

publish:
	$(PYTHON) scripts/publish_local.py

validate:
	$(PYTHON) scripts/validate_skill_pack.py

all: fetch harvest scaffold render lint
