# =============================================================================
# AI Gateway - Operations Makefile
# =============================================================================
# 运维只需要记住 make 命令:
#
#   make deploy          部署全部服务 (启动+迁移+健康检查，不默认重建镜像)
#   make deploy-build    部署并强制重新构建镜像
#   make deploy-cn       使用国内镜像构建部署
#   make validate-config 校验 .env 和 Compose 配置
#   make validate-example-config 校验开源示例配置和 Compose 渲染
#   make validate        校验 .env、Compose 配置和运行时依赖
#   make seed-demo       预览本地 demo 数据
#   make seed-demo-apply 写入本地 demo 数据
#   make status          查看所有服务状态
#   make logs            查看实时日志
#   make stop            停止所有服务
#   make restart         重启所有服务
#
#   make migrate         运行数据库迁移 (自动跳过已执行的)
#   make migrate-init    首次初始化数据库 schema
#   make migrate-status  查看迁移状态
#
#   make backup          创建数据库备份
#   make restore         从最新备份恢复
#   make backup-list     列出所有备份
#
#   make dev-setup       一键搭建本地开发环境
#   make dev-start       启动开发容器
#   make dev-stop        停止开发容器
#   make dev-reset       重置开发环境
#   make dev-status      查看开发环境状态
#   make dev-compose     使用源码挂载启动应用服务 (无需反复构建)
#
#   make help            显示此帮助
# =============================================================================

SHELL := /bin/bash
SCRIPTS := scripts/new
ENV_FILE ?= .env
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")
DEV_COMPOSE := $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml

.DEFAULT_GOAL := help

# -- Quick Start --------------------------------------------------------------

.PHONY: quickstart validate-config validate-example-config validate seed-demo seed-demo-apply

quickstart:                 ## 零配置一键部署 (首次使用: 启动+迁移+校验)
	@bash $(SCRIPTS)/validate-env.sh --env "$(ENV_FILE)" --config-only
	@$(COMPOSE) --env-file "$(ENV_FILE)" up -d --build --remove-orphans
	@ENV_FILE="$(ENV_FILE)" bash -c 'source "$(SCRIPTS)/common.sh"; load_env; wait_for_healthy "PostgreSQL" "check_postgres_health" 30'
	@bash $(SCRIPTS)/migrate.sh --env "$(ENV_FILE)" --auto
	@bash $(SCRIPTS)/validate-env.sh --env "$(ENV_FILE)" --runtime
	@echo ""
	@echo "AI Gateway is starting..."
	@ENV_FILE="$(ENV_FILE)" bash -c 'source "$(SCRIPTS)/common.sh"; load_env; echo "  Gateway:  http://localhost:$${GATEWAY_PORT:-8080}"; echo "  Frontend: http://localhost:$${FRONTEND_PORT:-8081}"'
	@echo "  Run 'make status' to check health."

validate:                   ## 校验 .env、Compose 配置和运行时依赖
	@bash $(SCRIPTS)/validate-env.sh --env "$(ENV_FILE)" --runtime

validate-config:            ## 仅校验 .env 和 Compose 配置
	@bash $(SCRIPTS)/validate-env.sh --env "$(ENV_FILE)" --config-only

validate-example-config:    ## 校验提交的 .env.example 和 Compose 示例配置
	@bash $(SCRIPTS)/validate-env.sh --env ".env.example" --example

seed-demo:                  ## 预览本地 demo 数据 SQL 和路由 (不写数据库)
	@bash $(SCRIPTS)/seed-demo-data.sh --env "$(ENV_FILE)" --dry-run

seed-demo-apply:            ## 写入本地 demo 数据 (仅用于开发/本地演示)
	@bash $(SCRIPTS)/seed-demo-data.sh --env "$(ENV_FILE)" --apply

# -- Deployment ---------------------------------------------------------------

.PHONY: deploy deploy-build deploy-cn deploy-infra deploy-app stop logs restart status

deploy:                     ## 部署全部服务 (启动+迁移+健康检查，不默认重建镜像)
	@bash $(SCRIPTS)/deploy.sh --env "$(ENV_FILE)" $(ARGS)

deploy-build:               ## 部署并重新构建镜像
	@bash $(SCRIPTS)/deploy.sh --env "$(ENV_FILE)" --build $(ARGS)

deploy-cn:                  ## 使用国内镜像构建部署
	@bash $(SCRIPTS)/deploy.sh --env "$(ENV_FILE)" --cn $(ARGS)

deploy-infra:               ## 仅部署基础设施 (postgres/redis/qdrant)
	@bash $(SCRIPTS)/deploy.sh --env "$(ENV_FILE)" --infra $(ARGS)

deploy-app:                 ## 仅部署应用服务 (gateway/frontend/assistant/knowledge/docgen)
	@bash $(SCRIPTS)/deploy.sh --env "$(ENV_FILE)" --app $(ARGS)

stop:                       ## 停止所有服务
	@cd "$(shell pwd)" && $(COMPOSE) --env-file "$(ENV_FILE)" stop

restart:                    ## 重启所有服务
	@cd "$(shell pwd)" && $(COMPOSE) --env-file "$(ENV_FILE)" restart

logs:                       ## 查看实时日志
	@cd "$(shell pwd)" && $(COMPOSE) --env-file "$(ENV_FILE)" logs -f

status:                     ## 查看所有服务状态和健康检查
	@ENV_FILE="$(ENV_FILE)" bash $(SCRIPTS)/status.sh

# -- Database Migrations ------------------------------------------------------

.PHONY: migrate migrate-init migrate-status

migrate:                    ## 运行数据库迁移 (自动跳过已执行的)
	@bash $(SCRIPTS)/migrate.sh --env "$(ENV_FILE)"

migrate-init:               ## 首次初始化数据库 schema
	@bash $(SCRIPTS)/migrate.sh --env "$(ENV_FILE)" --init

migrate-status:             ## 查看迁移状态 (已执行/待执行)
	@bash $(SCRIPTS)/migrate.sh --env "$(ENV_FILE)" --status

# -- Backup & Restore --------------------------------------------------------

.PHONY: backup restore backup-list

backup:                     ## 创建数据库备份
	@ENV_FILE="$(ENV_FILE)" bash $(SCRIPTS)/backup.sh

restore:                    ## 从最新备份恢复
	@ENV_FILE="$(ENV_FILE)" bash $(SCRIPTS)/backup.sh --restore

backup-list:                ## 列出所有备份
	@ENV_FILE="$(ENV_FILE)" bash $(SCRIPTS)/backup.sh --list

# -- Development Environment --------------------------------------------------

.PHONY: dev-setup dev-start dev-stop dev-reset dev-status dev-compose dev-compose-logs

dev-setup:                  ## 一键搭建本地开发环境 (容器+数据库+迁移)
	@ENV_FILE="$(ENV_FILE)" bash $(SCRIPTS)/setup-dev.sh

dev-start:                  ## 启动开发容器
	@ENV_FILE="$(ENV_FILE)" bash $(SCRIPTS)/setup-dev.sh --start

dev-stop:                   ## 停止开发容器
	@ENV_FILE="$(ENV_FILE)" bash $(SCRIPTS)/setup-dev.sh --stop

dev-reset:                  ## 重置开发环境 (销毁并重建)
	@ENV_FILE="$(ENV_FILE)" bash $(SCRIPTS)/setup-dev.sh --reset

dev-status:                 ## 查看开发环境状态
	@ENV_FILE="$(ENV_FILE)" bash $(SCRIPTS)/setup-dev.sh --status

dev-compose:                ## 源码挂载启动应用服务 (首次 quickstart/build 后使用)
	@bash $(SCRIPTS)/validate-env.sh --env "$(ENV_FILE)" --config-only
	@$(DEV_COMPOSE) --env-file "$(ENV_FILE)" up -d --remove-orphans gateway assistant-service knowledge-service frontend
	@echo "Development compose is running with backend source mounts and uvicorn reload."

dev-compose-logs:           ## 查看源码挂载开发服务日志
	@$(DEV_COMPOSE) --env-file "$(ENV_FILE)" logs -f gateway assistant-service knowledge-service frontend

# -- Agent Trace / Eval Development Gates ------------------------------------

.PHONY: verify-eval-dev test-isolation snapshot-assistant-openapi

verify-eval-dev:            ## 运行 Agent Trace/Eval dev 分支验证门禁
	@uv run ruff check \
		src/api/v1/eval.py \
		src/api/schemas/eval.py \
		src/api/eval_export.py \
		packages/ai-gateway-core/src/ai_gateway_core/eval \
		packages/ai-gateway-core/src/ai_gateway_core/persistence/repositories/agent_trace_repository.py \
		apps/assistant-service/src/assistant_service/core/assistant_service.py \
		apps/assistant-service/src/assistant_service/core/trace_writer.py \
		apps/assistant-service/src/assistant_service/core/trace_payloads.py \
		tests/api/test_eval_traces.py \
		tests/api/test_eval_api_trace_tree.py \
		tests/services/eval \
		tests/services/assistant/test_agent_trace_capture.py
	@uv run pytest -q --no-cov \
		tests/api/test_eval_traces.py \
		tests/api/test_eval_api_trace_tree.py \
		tests/services/eval/test_eval_permissions.py
	@uv run pytest -q --no-cov \
		tests/services/eval/test_evaluator_executor.py \
		tests/services/eval/test_outbox_worker.py \
		tests/services/eval/test_trace_retention_scheduler.py \
		tests/services/eval/test_search_indexes.py \
		tests/services/eval/test_drive_shipped_entrypoints.py
	@uv run --package assistant-service pytest -q --no-cov \
		tests/services/assistant/test_agent_trace_capture.py
	@uv run --package assistant-service pytest -q --no-cov \
		tests/services/eval/test_ingest_roundtrip.py \
		tests/services/eval/test_trace_capture_helpers.py
	@corepack pnpm@10.33.0 -C web lint
	@corepack pnpm@10.33.0 -C web type-check

# -- Assistant Service Isolation Gate (Phase 0 safety net) -------------------

test-isolation:             ## 运行 Assistant Service 隔离契约测试 (Phase 0 + Phase 4 gates)
	@uv run pytest -q --no-cov \
		tests/integration/test_assistant_isolation_contract.py \
		tests/integration/test_assistant_openapi_contract.py \
		tests/integration/test_assistant_core_isolation.py \
		tests/integration/test_gateway_boot.py

snapshot-assistant-openapi: ## 重新生成 assistant-service OpenAPI 基线快照
	@uv run python scripts/snapshot_assistant_openapi.py

# -- Help ---------------------------------------------------------------------

.PHONY: help

help:                       ## 显示此帮助信息
	@echo ""
	@echo "AI Gateway 运维命令:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
