UV ?= uv
UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
UV_RUN = UV_CACHE_DIR="$(UV_CACHE_DIR)" $(UV) run --frozen --offline
PACKAGES ?=
CORES ?=
UPSTREAM_REF ?=
DEST ?=
KEEP_WORKDIR ?=
TEST_JOBS ?= 4
TEST_TIMEOUT ?= 300
TEST_GLOBAL_TIMEOUT ?= 360

PACKAGE_ARGS = $(foreach package,$(PACKAGES),--package $(package))
CORE_ARGS = $(foreach core,$(CORES),--core $(core))
KEEP_WORKDIR_ARG = $(if $(strip $(KEEP_WORKDIR)),--keep-workdir,)
DEST_ARG = $(if $(strip $(DEST)),--dest "$(DEST)",)
TEST_RUN = TEST_JOBS="$(TEST_JOBS)" TEST_TIMEOUT="$(TEST_TIMEOUT)" \
	TEST_GLOBAL_TIMEOUT="$(TEST_GLOBAL_TIMEOUT)" \
	$(UV_RUN) python scripts/run_tests.py

.PHONY: lock-check bootstrap doctor refresh fetch harvest build render \
	lint style-report test deterministic-check scan check publish validate \
	validate-core validate-packages validate-plugin-compile \
	validate-plugin-runtime all

lock-check:
	UV_CACHE_DIR="$(UV_CACHE_DIR)" $(UV) lock --check --offline

bootstrap:
	UV_CACHE_DIR="$(UV_CACHE_DIR)" $(UV) sync --frozen
	UV_CACHE_DIR="$(UV_CACHE_DIR)" $(UV) lock --check --offline

doctor: lock-check
	$(UV_RUN) python scripts/doctor.py

refresh:
	@test -n "$(UPSTREAM_REF)" || \
		(echo "ERROR: UPSTREAM_REF must be an exact 40-character commit"; exit 2)
	$(UV_RUN) python scripts/fetch_upstream.py --upstream-ref "$(UPSTREAM_REF)"

fetch: refresh

harvest:
	$(UV_RUN) python scripts/harvest_stata_help.py

build: lock-check
	$(UV_RUN) python scripts/scan_repository.py
	$(UV_RUN) python scripts/lint_skill_pack.py --no-generated-check
	$(UV_RUN) python scripts/render_skills.py

render: build

lint: build
	$(UV_RUN) python scripts/lint_skill_pack.py

style-report: build
	$(UV_RUN) python scripts/lint_skill_pack.py --style-report

test:
	$(TEST_RUN)

deterministic-check:
	$(UV_RUN) python scripts/check_determinism.py

scan:
	$(UV_RUN) python scripts/scan_repository.py

check: build
	$(UV_RUN) python scripts/lint_skill_pack.py
	$(TEST_RUN)
	$(UV_RUN) python scripts/check_determinism.py
	$(UV_RUN) python scripts/scan_repository.py

publish:
	$(UV_RUN) python scripts/publish_local.py $(DEST_ARG)

validate:
	@test ! -L "$(CURDIR)/build" || \
		(echo "ERROR: build must not be a symlink"; exit 2)
	$(UV_RUN) python scripts/validate_skill_pack.py --invalidate-receipt
	$(MAKE) check
	$(UV_RUN) python scripts/validate_skill_pack.py \
		--suite default --write-receipt $(KEEP_WORKDIR_ARG)

validate-core: check
	$(UV_RUN) python scripts/validate_skill_pack.py \
		--suite core $(CORE_ARGS) $(KEEP_WORKDIR_ARG)

validate-packages: check
	$(UV_RUN) python scripts/validate_skill_pack.py \
		--suite packages $(PACKAGE_ARGS) $(KEEP_WORKDIR_ARG)

validate-plugin-compile: check
	$(UV_RUN) python scripts/validate_skill_pack.py \
		--suite plugin-compile $(KEEP_WORKDIR_ARG)

validate-plugin-runtime: check
	$(UV_RUN) python scripts/validate_skill_pack.py \
		--suite plugin-runtime $(KEEP_WORKDIR_ARG)

all: check
