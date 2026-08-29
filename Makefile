# pporlock — build and gate targets.
#
# The sprint close gates in docs/implementation-plan.md §2.3 are expressed here.
# `make gate` is what must pass before any sprint branch merges into master.

SHELL := /bin/bash
.DEFAULT_GOAL := help

UV      := uv
NPM     := npm
DAEMON  := daemon
MCP     := mcp
WEB     := web
EXT     := extension
CONTR   := contracts

# A component is "in play" for the coverage gate once it has product source.
# Components with no source yet are exempt (implementation-plan.md §2.3, G2).
DAEMON_SRC := $(shell find $(DAEMON)/src/pporlock -name '*.py' ! -name '__init__.py' 2>/dev/null)
MCP_SRC    := $(shell find $(MCP)/src/pporlock_mcp -name '*.py' ! -name '__init__.py' 2>/dev/null)
WEB_SRC    := $(shell find $(WEB)/src -name '*.ts' -o -name '*.tsx' 2>/dev/null | grep -v '\.test\.' )
EXT_SRC    := $(shell find $(EXT)/src -name '*.ts' -o -name '*.tsx' 2>/dev/null | grep -v '\.test\.' )

.PHONY: help
help:
	@echo "Setup"
	@echo "  setup        install all toolchains, generate contracts, install hooks"
	@echo ""
	@echo "Build"
	@echo "  contracts    validate schemas, regenerate contracts/generated/types.ts"
	@echo "  daemon web extension mcp"
	@echo "  all          contracts -> daemon, web, extension"
	@echo ""
	@echo "Gates (docs/implementation-plan.md §2.3)"
	@echo "  test         G3  ALL tests, all components"
	@echo "  coverage     G2  per-component thresholds"
	@echo "  lint         G5  ruff, mypy, eslint, tsc"
	@echo "  security     G6  bandit, pip-audit, npm audit, gitleaks"
	@echo "  gate         G2 + G3 + G5 + G6"
	@echo ""
	@echo "Other"
	@echo "  e2e          Playwright suites (e2e-web / e2e-extension)"
	@echo "  fixtures     fixture origin server, standalone"
	@echo "  bench        PRF-001/002 harness"
	@echo "  clean"

# ------------------------------------------------------------------ setup ---
.PHONY: setup
setup:
	@echo "==> python toolchains"
	cd $(DAEMON) && $(UV) sync --group dev
	cd $(MCP)    && $(UV) sync --group dev
	@echo "==> node toolchains"
	cd $(CONTR) && $(NPM) install
	cd $(WEB)   && $(NPM) install
	cd $(EXT)   && $(NPM) install
	@$(MAKE) contracts
	@echo "==> git hooks"
	@./scripts/install-hooks.sh
	@echo "setup complete"

# ------------------------------------------------------------------ build ---
.PHONY: contracts
contracts:
	cd $(CONTR) && $(NPM) run --silent validate
	cd $(CONTR) && $(NPM) run --silent generate
	cd $(CONTR) && $(NPM) run --silent docs

.PHONY: docs
docs:
	@# REQ DOC-004. The API reference and the rule schema reference are
	@# rendered from contracts/, not maintained beside it — a hand-written
	@# copy of a machine-readable contract is a copy that will disagree.
	cd $(CONTR) && $(NPM) run --silent docs

.PHONY: daemon
daemon:
	cd $(DAEMON) && $(UV) build

.PHONY: mcp
mcp:
	cd $(MCP) && $(UV) build

.PHONY: web
web: contracts
	cd $(WEB) && $(NPM) run --silent build

.PHONY: extension
extension: contracts
	cd $(EXT) && $(NPM) run --silent build

.PHONY: all
all: contracts daemon web extension

# ------------------------------------------------------------- G3: tests ---
.PHONY: test
test:
	@echo "==> G3 daemon"
	cd $(DAEMON) && $(UV) run pytest
	@echo "==> G3 mcp"
	cd $(MCP) && $(UV) run pytest
	@echo "==> G3 web"
	cd $(WEB) && $(NPM) run --silent test
	@echo "==> G3 extension"
	cd $(EXT) && $(NPM) run --silent test
	@echo "G3 PASS — all tests, all components"

# ---------------------------------------------------------- G2: coverage ---
# Per-component thresholds. daemon >= 80, engine >= 90, mcp >= 80,
# web >= 80, extension >= 80. No source yet -> exempt, and it says so.
.PHONY: coverage
coverage:
	@echo "==> G2 daemon"
ifeq ($(strip $(DAEMON_SRC)),)
	@echo "    SKIP daemon — no product source yet (exempt, implementation-plan §2.3 G2)"
else
	cd $(DAEMON) && $(UV) run pytest --cov --cov-report=term-missing --cov-fail-under=80
	@echo "==> G2 daemon/engine (>= 90%, REQ TST-002)"
	cd $(DAEMON) && $(UV) run pytest --cov=src/pporlock/engine --cov-report=term-missing --cov-fail-under=90
endif
	@echo "==> G2 mcp"
ifeq ($(strip $(MCP_SRC)),)
	@echo "    SKIP mcp — no product source yet (exempt)"
else
	cd $(MCP) && $(UV) run pytest --cov --cov-report=term-missing --cov-fail-under=80
endif
	@echo "==> G2 web"
ifeq ($(strip $(WEB_SRC)),)
	@echo "    SKIP web — no product source yet (exempt)"
else
	cd $(WEB) && $(NPM) run --silent coverage
endif
	@echo "==> G2 extension"
ifeq ($(strip $(EXT_SRC)),)
	@echo "    SKIP extension — no product source yet (exempt)"
else
	cd $(EXT) && $(NPM) run --silent coverage
endif
	@echo "G2 PASS — per-component coverage thresholds met"

# -------------------------------------------------------------- G5: lint ---
.PHONY: lint
lint:
	@echo "==> G5 ruff"
	cd $(DAEMON) && $(UV) run ruff format --check . && $(UV) run ruff check .
	cd $(MCP)    && $(UV) run ruff format --check . && $(UV) run ruff check .
	@echo "==> G5 ruff (shared test fixtures)"
	cd $(DAEMON) && $(UV) run ruff format --check ../testfixtures && $(UV) run ruff check ../testfixtures
	@echo "==> G5 mypy"
	cd $(DAEMON) && $(UV) run mypy src
	cd $(MCP)    && $(UV) run mypy src
	@echo "==> G5 tsc + eslint + prettier"
	cd $(WEB) && $(NPM) run --silent typecheck && $(NPM) run --silent lint && $(NPM) run --silent format:check
	cd $(EXT) && $(NPM) run --silent typecheck && $(NPM) run --silent lint && $(NPM) run --silent format:check
	@echo "==> G5 generated documentation is current (REQ DOC-004)"
	cd $(CONTR) && $(NPM) run --silent docs:check
	@echo "G5 PASS — lint clean"

.PHONY: format
format:
	cd $(DAEMON) && $(UV) run ruff format . && $(UV) run ruff check --fix .
	cd $(MCP)    && $(UV) run ruff format . && $(UV) run ruff check --fix .
	cd $(WEB) && $(NPM) run --silent format
	cd $(EXT) && $(NPM) run --silent format

# ---------------------------------------------------------- G6: security ---
# Scanners only. The hand-reviewed checklist in implementation-plan.md §2.5 is
# the other half of G6 and is not automatable.
.PHONY: security
security:
	@echo "==> G6 bandit"
	cd $(DAEMON) && $(UV) run bandit -q -c pyproject.toml -r src
	cd $(MCP)    && $(UV) run bandit -q -c pyproject.toml -r src
	@echo "==> G6 pip-audit"
	@# No '|| true' here. An advisory that does not fail the gate is an advisory
	@# nobody reads. Transitive findings we have accepted live in .pip-audit-ignore
	@# with a written justification and are passed as explicit --ignore-vuln flags.
	cd $(DAEMON) && $(UV) run pip-audit --skip-editable $$(sed -n 's/^ignore: *//p' ../.pip-audit-ignore | sed 's/^/--ignore-vuln /' | tr '\n' ' ')
	cd $(MCP)    && $(UV) run pip-audit --skip-editable
	@echo "==> G6 npm audit (high+)"
	cd $(WEB) && $(NPM) audit --audit-level=high
	cd $(EXT) && $(NPM) audit --audit-level=high
	cd $(CONTR) && $(NPM) audit --audit-level=high
	@echo "==> G6 gitleaks"
	@./scripts/gitleaks.sh detect
	@echo "G6 PASS — scanners clean. Now walk §2.5 for the areas this sprint touched."

# -------------------------------------------------------------- the gate ---
.PHONY: gate
gate: lint test coverage security
	@echo ""
	@echo "======================================================================"
	@echo " GATE PASS — G2, G3, G5, G6 automated checks clean."
	@echo ""
	@echo " Still required by hand before merging:"
	@echo "   G1  sprint requirement IDs demonstrated, exit demo run"
	@echo "   G4  removed tests reviewed — no coverage laundering"
	@echo "   G6  security checklist §2.5 walked for touched areas"
	@echo "   G7  git merge --no-ff  (never squash)"
	@echo "======================================================================"

# -------------------------------------------------------------- the rest ---
.PHONY: e2e
e2e: web extension
	cd $(WEB) && npx playwright test

.PHONY: e2e-web
e2e-web: web
	cd $(WEB) && npx playwright test --project=web

# Headed by necessity — MV3 extensions do not load headless.
.PHONY: e2e-extension
e2e-extension: extension
	cd $(WEB) && npx playwright test --project=extension

.PHONY: examples
examples:
	@# Copies the example modules into the state directory, still disabled.
	@# Never overwrites: a module you have edited is yours, and an install
	@# step that silently replaces your work is not a convenience.
	@mkdir -p $(HOME)/.pporlock/modules
	@for d in examples/modules/*/; do \
		name=$$(basename $$d); \
		if [ -e "$(HOME)/.pporlock/modules/$$name" ]; then \
			echo "  skip    $$name (already installed)"; \
		else \
			cp -R "$$d" "$(HOME)/.pporlock/modules/$$name"; \
			echo "  install $$name"; \
		fi; \
	done
	@echo ""
	@echo "Installed disabled. Read them, then enable the ones you want."
	@echo "  examples/README.md      what each one shows"
	@echo "  docs/module-cookbook.md the reference they draw on"

.PHONY: fixtures
fixtures:
	cd $(DAEMON) && $(UV) run python ../testfixtures/origin/server.py --port 8099 --verbose

.PHONY: bench
bench:
	cd $(DAEMON) && $(UV) run python -m bench.run

.PHONY: clean
clean:
	rm -rf $(WEB)/dist $(EXT)/dist $(CONTR)/generated
	rm -rf $(DAEMON)/dist $(MCP)/dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find . -name '.pytest_cache' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(DAEMON)/.coverage $(WEB)/coverage $(EXT)/coverage
