# skyll-trades-validator — read-only fills→trades→candle integrity dashboard.
# Targets that touch prod data run through `secretctl run skyll-mwaa` (keychain must be unlocked).
PORT ?= 8799
URL  := http://127.0.0.1:$(PORT)

.PHONY: help up down logs restart backend frontend smoke report report-md build install

help:
	@echo "make up        - build UI + start backend detached + open browser (frees your terminal)"
	@echo "make down      - stop the detached backend"
	@echo "make restart   - down then up"
	@echo "make logs      - tail the detached backend log"
	@echo "make backend   - run backend in the FOREGROUND (blocks; for debugging)"
	@echo "make frontend  - Vite dev server with live reload (proxies /api -> :$(PORT))"
	@echo "make smoke     - engine text summary of current state, no server"
	@echo "make report    - AGENT-READABLE findings JSON (offline). ARGS='--category mismatch,skipped --min-net 5'"
	@echo "make report-md - AGENT-READABLE findings as a markdown digest (offline)"
	@echo "make build     - production-build the UI into frontend/dist"
	@echo "make install   - set up backend venv + frontend node_modules"

# One command: start the whole thing in the background and open it.
up:
	@-lsof -nP -iTCP:$(PORT) -sTCP:LISTEN -t | xargs kill 2>/dev/null || true
	@sleep 0.5
	@echo "building UI…"
	@-cd frontend && yarn build >/dev/null 2>&1 || echo "  (skipped UI rebuild — serving existing frontend/dist)"
	@echo "starting backend on :$(PORT) (detached)…"
	@cd backend && nohup secretctl run skyll-mwaa -- ./venv/bin/uvicorn app.api:app --host 127.0.0.1 --port $(PORT) > ../validator.log 2>&1 &
	@for i in $$(seq 1 40); do curl -fs $(URL)/api/health >/dev/null 2>&1 && break || sleep 0.5; done
	@curl -fs $(URL)/api/health >/dev/null 2>&1 && open $(URL) && echo "up → $(URL)  (stop: make down · logs: make logs)" \
		|| echo "backend did not come up — check: make logs  (is the keychain unlocked? secretctl unlock)"

down:
	@-lsof -nP -iTCP:$(PORT) -sTCP:LISTEN -t | xargs kill 2>/dev/null || true
	@echo "stopped backend on :$(PORT)"

restart: down up

logs:
	@tail -f validator.log

backend:
	cd backend && secretctl run skyll-mwaa -- ./venv/bin/uvicorn app.api:app --host 127.0.0.1 --port $(PORT)

frontend:
	cd frontend && yarn dev

smoke:
	cd backend && secretctl run skyll-mwaa -- ./venv/bin/python -m app.engine

# Agent-readable findings (no server needed). Pass filters via ARGS, e.g.
#   make report ARGS="--category mismatch,skipped --min-net 5 --group Axia --limit 40"
report:
	@cd backend && secretctl run skyll-mwaa -- ./venv/bin/python -m app.report $(ARGS)

report-md:
	@cd backend && secretctl run skyll-mwaa -- ./venv/bin/python -m app.report --md $(ARGS)

build:
	cd frontend && yarn build

install:
	cd backend && python3 -m venv venv && ./venv/bin/pip install -q -r requirements.txt
	cd frontend && yarn install
