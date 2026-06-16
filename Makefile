# =============================================================================
# AI Gateway - Operations Makefile
# =============================================================================
# 运维只需要记住 make 命令:
#
#   make deploy          部署全部服务 (启动+迁移+健康检查，不默认重建镜像)
#   make deploy-build    部署并强制重新构建镜像
#   make deploy-cn       使用国内镜像构建部署
#   make validate-config 校验 .env 和 Compose 配置
#   make validate        校验 .env、Compose 配置和运行时依赖
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
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")
DEV_COMPOSE := $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml

.DEFAULT_GOAL := help

# -- Quick Start --------------------------------------------------------------

.PHONY: quickstart validate-config validate

quickstart:                 ## 零配置一键部署 (首次使用)
	@bash $(SCRIPTS)/validate-env.sh --config-only
	@$(COMPOSE) --env-file .env up -d --build --remove-orphans
	@bash $(SCRIPTS)/validate-env.sh --runtime
	@echo ""
	@echo "AI Gateway is starting..."
	@bash -c 'source "$(SCRIPTS)/common.sh"; load_env; echo "  Gateway:  http://localhost:$${GATEWAY_PORT:-8080}"; echo "  Frontend: http://localhost:$${FRONTEND_PORT:-8081}"'
	@echo "  Run 'make status' to check health."

validate:                   ## 校验 .env、Compose 配置和运行时依赖
	@bash $(SCRIPTS)/validate-env.sh --runtime

validate-config:            ## 仅校验 .env 和 Compose 配置
	@bash $(SCRIPTS)/validate-env.sh --config-only

# -- Deployment ---------------------------------------------------------------

.PHONY: deploy deploy-build deploy-cn deploy-infra deploy-app stop logs restart status

deploy:                     ## 部署全部服务 (启动+迁移+健康检查，不默认重建镜像)
	@bash $(SCRIPTS)/deploy.sh

deploy-build:               ## 部署并重新构建镜像
	@bash $(SCRIPTS)/deploy.sh --build

deploy-cn:                  ## 使用国内镜像构建部署
	@bash $(SCRIPTS)/deploy.sh --cn

deploy-infra:               ## 仅部署基础设施 (postgres/redis/qdrant)
	@bash $(SCRIPTS)/deploy.sh --infra

deploy-app:                 ## 仅部署应用 (gateway/frontend)
	@bash $(SCRIPTS)/deploy.sh --app

stop:                       ## 停止所有服务
	@cd "$(shell pwd)" && $(COMPOSE) stop

restart:                    ## 重启所有服务
	@cd "$(shell pwd)" && $(COMPOSE) restart

logs:                       ## 查看实时日志
	@cd "$(shell pwd)" && $(COMPOSE) logs -f

status:                     ## 查看所有服务状态和健康检查
	@bash $(SCRIPTS)/status.sh

# -- Database Migrations ------------------------------------------------------

.PHONY: migrate migrate-init migrate-status

migrate:                    ## 运行数据库迁移 (自动跳过已执行的)
	@bash $(SCRIPTS)/migrate.sh

migrate-init:               ## 首次初始化数据库 schema
	@bash $(SCRIPTS)/migrate.sh --init

migrate-status:             ## 查看迁移状态 (已执行/待执行)
	@bash $(SCRIPTS)/migrate.sh --status

# -- Backup & Restore --------------------------------------------------------

.PHONY: backup restore backup-list

backup:                     ## 创建数据库备份
	@bash $(SCRIPTS)/backup.sh

restore:                    ## 从最新备份恢复
	@bash $(SCRIPTS)/backup.sh --restore

backup-list:                ## 列出所有备份
	@bash $(SCRIPTS)/backup.sh --list

# -- Development Environment --------------------------------------------------

.PHONY: dev-setup dev-start dev-stop dev-reset dev-status dev-compose dev-compose-logs

dev-setup:                  ## 一键搭建本地开发环境 (容器+数据库+迁移)
	@bash $(SCRIPTS)/setup-dev.sh

dev-start:                  ## 启动开发容器
	@bash $(SCRIPTS)/setup-dev.sh --start

dev-stop:                   ## 停止开发容器
	@bash $(SCRIPTS)/setup-dev.sh --stop

dev-reset:                  ## 重置开发环境 (销毁并重建)
	@bash $(SCRIPTS)/setup-dev.sh --reset

dev-status:                 ## 查看开发环境状态
	@bash $(SCRIPTS)/setup-dev.sh --status

dev-compose:                ## 源码挂载启动应用服务 (首次 quickstart/build 后使用)
	@bash $(SCRIPTS)/validate-env.sh --config-only
	@$(DEV_COMPOSE) --env-file .env up -d --remove-orphans gateway assistant-service knowledge-service frontend
	@echo "Development compose is running with backend source mounts and uvicorn reload."

dev-compose-logs:           ## 查看源码挂载开发服务日志
	@$(DEV_COMPOSE) --env-file .env logs -f gateway assistant-service knowledge-service frontend

# -- Assistant Service Isolation Gate (Phase 0 safety net) -------------------

.PHONY: test-isolation snapshot-assistant-openapi

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
