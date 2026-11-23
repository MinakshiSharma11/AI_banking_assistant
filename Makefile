COMPOSE := docker compose --env-file .env -f infra/docker-compose.yml
UV := uv run --python 3.12

.PHONY: ollama-model ask test-live api web up-all up down seed decode sanity refresh-live test test-unit cov lint fmt psql eval

COUNT ?= 0

up:
	$(COMPOSE) up -d --wait db

# Full stack in Docker (db + api + web): http://localhost:5180
up-all:
	$(COMPOSE) up -d --build

# Gateway on the host (port 8200) and the UI dev server (port 5180, proxies /api)
api:
	$(UV) uvicorn aiplatform.gateway.app:app --host 127.0.0.1 --port 8200

web:
	cd web/staff && npm install --no-audit --no-fund && npm run dev

down:
	$(COMPOSE) down

seed:
	$(UV) python -m mocks.seed.run
	$(UV) python -m assistants.data.semantic.atum_decoder

# Atumverse payloads -> atum.wf_instance_flat (incremental; malformed -> atum.decode_error)
decode:
	$(UV) python -m assistants.data.semantic.atum_decoder

sanity:
	$(UV) python -m mocks.seed.sanity

# Re-stamp the "active right now" OBDX sessions (they age out 15 min after seeding)
refresh-live:
	$(UV) python -m mocks.seed.refresh_live --count $(COUNT)

test:
	$(UV) pytest -q

# Real Ollama calls (slow): planner/answerer behaviour on spec scenarios
test-live:
	$(UV) pytest -q -m live_llm -p no:cacheprovider

test-unit:
	$(UV) pytest -q -m "not db"

# Spec bar: >= 85% line coverage on the semantic package
cov:
	$(UV) pytest -q --cov=assistants/data/semantic --cov-report=term-missing --cov-fail-under=85

lint:
	$(UV) ruff check .
	$(UV) ruff format --check .
	$(UV) mypy

fmt:
	$(UV) ruff format .
	$(UV) ruff check --fix .

psql:
	$(COMPOSE) exec db psql -U bcai -d bankdata

# Golden-set evaluation: make eval ASSISTANT=data PROVIDER=ollama|anthropic|gemini|cohere|all [IDS=E01,A14]
ASSISTANT ?= data
PROVIDER ?= ollama
eval:
	$(UV) python -m aiplatform.eval.run --assistant $(ASSISTANT) --provider $(PROVIDER) $(if $(IDS),--ids $(IDS),)

# Local LLM: qwen3:8b + 12k context window, and the embedding model for example retrieval
ollama-model:
	ollama pull qwen3:8b
	ollama pull qwen3-embedding:0.6b
	ollama create bcai-qwen3-8b -f infra/ollama/Modelfile

# Ask the data agent from the terminal: make ask Q="accounts opened by branch last quarter" USER=ho_analyst
USER ?= ho_analyst
ask:
	$(UV) python -m assistants.data.cli "$(Q)" --user $(USER)
