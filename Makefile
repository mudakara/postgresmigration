.PHONY: help init up down logs sandbox test migrate revision ps

COMPOSE := docker compose

help:  ## Show available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

init:  ## Create .env with freshly generated secrets (no-op if .env exists)
	@if [ -f .env ]; then echo ".env already exists, leaving it alone"; else \
	  key=$$(openssl rand -base64 32 | tr '+/' '-_'); \
	  sed -e "s|^CREDENTIALS_KEY=.*|CREDENTIALS_KEY=$$key|" \
	      -e "s|^APP_DB_PASSWORD=.*|APP_DB_PASSWORD=$$(openssl rand -hex 16)|" \
	      -e "s|^SCRATCH_DB_PASSWORD=.*|SCRATCH_DB_PASSWORD=$$(openssl rand -hex 16)|" \
	      -e "s|^ORACLE_SANDBOX_SYS_PASSWORD=.*|ORACLE_SANDBOX_SYS_PASSWORD=Sys_$$(openssl rand -hex 8)|" \
	      .env.example > .env; \
	  chmod 600 .env; echo "Created .env"; fi

up: init  ## Build and start the stack
	$(COMPOSE) up -d --build

sandbox: init  ## Start the stack plus the local Oracle sandbox
	$(COMPOSE) --profile sandbox up -d --build

down:  ## Stop everything (data volumes are kept)
	$(COMPOSE) --profile sandbox down

logs:  ## Follow logs
	$(COMPOSE) logs -f --tail=100

ps:  ## Show service status
	$(COMPOSE) --profile sandbox ps

test:  ## Run the backend test suite inside the api container
	$(COMPOSE) exec -T app-db psql -U studio -d studio -tc "SELECT 1 FROM pg_database WHERE datname = 'studio_test'" | grep -q 1 \
	  || $(COMPOSE) exec -T app-db createdb -U studio studio_test
	$(COMPOSE) exec -T api pytest -q $(ARGS)

migrate:  ## Apply database migrations
	$(COMPOSE) exec api alembic upgrade head

revision:  ## Autogenerate a migration: make revision m="add scans"
	$(COMPOSE) exec api alembic revision --autogenerate -m "$(m)"
