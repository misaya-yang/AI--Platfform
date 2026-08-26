# =============================================================================
# AI Gateway - Operations Makefile
# =============================================================================
# 运维只需要记住 make 命令:
#
#   make doctor          环境体检 (工具/内存/端口/compose 归属，只读不改动)
#   make quickstart      首次一键启动 (生成 .env + 拉镜像 + 启动 + 迁移 + 校验)
#   make harness-check   校验 harness.yml 契约与文档预算
#   make agent-runtime-source-contract 校验 Agent Runtime 源码/Schema/SBOM/OCI 锁
#
#   make deploy          部署全部服务 (启动+迁移+健康检查，不默认重建镜像)
#   make deploy-build    部署并强制重新构建镜像 (需要 AI_PLATFORM_AGENT_RUNTIME_SOURCE)
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
#   make hot-update      热更新本地部署容器源码 (不 pip、不重建镜像)
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
BUILD_COMPOSE := $(COMPOSE) -f docker-compose.yml -f docker-compose.build.yml
DEV_COMPOSE := $(BUILD_COMPOSE) -f docker-compose.dev.yml
COMPOSE_PARALLEL_LIMIT ?= 1
export COMPOSE_PARALLEL_LIMIT

.DEFAULT_GOAL := help

# -- Quick Start --------------------------------------------------------------

.PHONY: doctor harness-check runtime-dependency-gate agent-runtime-source-build-local agent-runtime-source-contract agent-runtime-build-local agent-runtime-contract agent-runtime-smoke agent-runtime-text-gate agent-runtime-single-kernel-gate agent-runtime-readonly-gate agent-runtime-write-gate agent-thread-store-contract agent-capability-worker-build-local agent-capability-worker-smoke agent-runtime-release-gate agent-runtime-rollback-rehearsal sdk-sse-contract snapshot-gateway-openapi quickstart quickstart-build validate-config validate-example-config validate seed-demo seed-demo-apply

doctor:                     ## 环境体检: 工具/Docker/内存/端口/compose 归属 (只读)
	@ENV_FILE="$(ENV_FILE)" bash $(SCRIPTS)/doctor.sh --env "$(ENV_FILE)"

harness-check:              ## 校验 harness.yml 契约: 命令存在、必备文档、指令文件行数预算
	@python3 scripts/harness/check_harness.py

runtime-dependency-gate:    ## 检查已退役 Python 执行面/docgen/OpenAPI 依赖未回流
	@python3 scripts/harness/runtime_dependency_gate.py

agent-runtime-source-contract: ## 校验 Agent Runtime 不可变源码、Schema、SBOM、许可证和 OCI 身份
	@python3 scripts/harness/agent_runtime_supply_chain.py validate \
		--repo-root . \
		--lock deploy/agent-runtime-source/lock.json \
		--require-artifact agent_runtime
	@uv run --all-packages --extra test pytest -q --no-cov \
		tests/harness/test_agent_runtime_supply_chain.py

agent-runtime-contract:     ## 校验 Rust Agent Runtime 已锁到独立、可运行的 OCI 制品
	@python3 scripts/harness/agent_runtime_supply_chain.py validate \
		--repo-root . \
		--lock deploy/agent-runtime-source/lock.json \
		--require-artifact agent_runtime

agent-runtime-build-local:  ## 从干净受控 fork 构建 Rust Agent Runtime 镜像
	@bash scripts/harness/build_agent_runtime_image.sh

agent-runtime-smoke:        ## 在隔离 PostgreSQL/Docker 网络验证 Runtime 健康、Thread 和重启恢复
	@bash scripts/harness/smoke_agent_runtime_image.sh

agent-runtime-text-gate:    ## 真实 Qwen Responses：简单、长输出与重启后多轮恢复
	@uv run --all-packages --extra test python \
		scripts/harness/agent_runtime_text_gate.py \
		--env-file "$(ENV_FILE)" \
		--runtime-image "$(AI_PLATFORM_AGENT_RUNTIME_IMAGE)"

agent-runtime-single-kernel-gate: ## 验证唯一 Rust Agent 内核、V1/V2 投影和迁移合同
	@python3 scripts/harness/agent_runtime_single_kernel_gate.py
	@ENV_FILE="$(ENV_FILE)" uv run --all-packages --extra test pytest -q --no-cov \
		tests/services/assistant/test_runtime_assignment.py \
		tests/services/agent_runtime/test_thread_store.py \
		tests/api/test_agent_v2.py \
		tests/database/test_agent_runtime_thread_store_migration.py \
		tests/contract/test_openapi_schema_compat.py

agent-runtime-readonly-gate: ## Context/Knowledge/Tool/MCP/Artifact 只读桥合同
	@uv run --all-packages --extra test python scripts/harness/agent_runtime_readonly_gate.py
	@uv run --all-packages --extra test pytest -q --no-cov \
		tests/services/agent_runtime/test_readonly_capabilities.py \
		tests/harness/test_agent_runtime_readonly_gate.py

agent-runtime-write-gate:   ## 校验工具审批、dispatch fence、幂等和中断恢复闭环
	@uv run --all-packages --extra test python \
		scripts/harness/agent_runtime_write_gate.py \
		--fork "$(AI_PLATFORM_AGENT_RUNTIME_SOURCE)"

agent-thread-store-contract: ## 在真实 PostgreSQL 验证 Agent ThreadStore 与预授权根线程闭环
	@ENV_FILE="$(ENV_FILE)" AI_PLATFORM_AGENT_RUNTIME_SOURCE="$(AI_PLATFORM_AGENT_RUNTIME_SOURCE)" \
		bash scripts/harness/agent_thread_store_contract.sh

agent-capability-worker-build-local: ## 单 job 构建固定源码的 Rust capability worker 镜像
	@AI_PLATFORM_AGENT_RUNTIME_SOURCE="$(AI_PLATFORM_AGENT_RUNTIME_SOURCE)" \
		bash scripts/harness/build_agent_capability_worker_image.sh

agent-capability-worker-smoke: ## 隔离 PostgreSQL 验证 capability worker 的租约、幂等、事件、取消和恢复
	@bash scripts/harness/smoke_agent_capability_worker_image.sh

agent-runtime-release-gate:   ## 串行运行单内核发布合同（不构建镜像、不调用 provider）
	@$(MAKE) agent-runtime-source-contract
	@$(MAKE) runtime-dependency-gate
	@$(MAKE) agent-runtime-single-kernel-gate
	@$(MAKE) agent-runtime-readonly-gate
	@$(MAKE) verify-assistant-runtime-dev
	@$(MAKE) agent-eval-core-gate
	@$(MAKE) test-isolation
	@$(MAKE) rag-eval-regression-gate
	@$(MAKE) sdk-sse-contract
	@$(MAKE) harness-check

agent-runtime-rollback-rehearsal: ## 用冻结 release 执行 new→old→new 回滚并验证会话/执行账本
	@ENV_FILE="$(ENV_FILE)" bash scripts/harness/agent_runtime_rollback_rehearsal.sh

agent-runtime-source-build-local: ## 低内存构建本地 Agent Runtime 源码镜像并验证源码/Schema 标签与 initialize
	@bash scripts/harness/build_agent_runtime_source_image.sh

sdk-sse-contract:           ## 验证四端 SSE 合同；发布 CI 要求 Maven/Dart 必须存在
	@uv run --all-packages --extra test pytest -q --no-cov \
		sdk/python/tests/test_sse_inner_envelopes.py \
		tests/contract/test_sdk_sse_fixture_contract.py
	@npm --prefix sdk/cli run typecheck
	@npm --prefix sdk/cli test
	@if command -v mvn >/dev/null 2>&1; then \
		mvn -q -f sdk/java/pom.xml test; \
	elif [ "$${SDK_SSE_CONTRACT_REQUIRE_ALL:-0}" = "1" ]; then \
		echo "ERROR Java SDK SSE contract requires mvn" >&2; exit 1; \
	else \
		echo "SKIP Java SDK SSE contract: mvn not installed"; \
	fi
	@if command -v dart >/dev/null 2>&1; then \
		cd sdk/dart/ai_gateway_sdk && dart pub get && dart test; \
	elif [ "$${SDK_SSE_CONTRACT_REQUIRE_ALL:-0}" = "1" ]; then \
		echo "ERROR Dart SDK SSE contract requires dart" >&2; exit 1; \
	else \
		echo "SKIP Dart SDK SSE contract: dart not installed"; \
	fi

snapshot-gateway-openapi:  ## 从实际 FastAPI app 生成 Gateway OpenAPI 快照
	@uv run --all-packages --extra test python scripts/snapshot_gateway_openapi.py

quickstart:                 ## 拉取版本化多架构镜像并一键部署 (仅需模型配置)
	@bash $(SCRIPTS)/init-env.sh --env "$(ENV_FILE)" --if-missing
	@bash $(SCRIPTS)/deploy.sh --env "$(ENV_FILE)" --pull

quickstart-build:           ## 维护者从当前源码串行构建并部署
	@bash $(SCRIPTS)/init-env.sh --env "$(ENV_FILE)" --if-missing
	@bash $(SCRIPTS)/deploy.sh --env "$(ENV_FILE)" --build

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

.PHONY: deploy deploy-build deploy-cn deploy-infra deploy-app stop logs restart status hot-update

deploy:                     ## 部署全部服务 (启动+迁移+健康检查，不默认重建镜像)
	@bash $(SCRIPTS)/deploy.sh --env "$(ENV_FILE)" $(ARGS)

deploy-build:               ## 部署并重新构建镜像
	@bash $(SCRIPTS)/deploy.sh --env "$(ENV_FILE)" --build $(ARGS)

deploy-cn:                  ## 使用国内镜像构建部署
	@bash $(SCRIPTS)/deploy.sh --env "$(ENV_FILE)" --cn $(ARGS)

deploy-infra:               ## 仅部署基础设施 (postgres/redis/qdrant)
	@bash $(SCRIPTS)/deploy.sh --env "$(ENV_FILE)" --infra $(ARGS)

deploy-app:                 ## 仅部署应用服务 (gateway/frontend/knowledge/Agent Runtime/Worker)
	@bash $(SCRIPTS)/deploy.sh --env "$(ENV_FILE)" --app $(ARGS)

stop:                       ## 停止所有服务
	@cd "$(shell pwd)" && $(COMPOSE) --env-file "$(ENV_FILE)" stop

restart:                    ## 重启所有服务
	@cd "$(shell pwd)" && $(COMPOSE) --env-file "$(ENV_FILE)" restart

logs:                       ## 查看实时日志
	@cd "$(shell pwd)" && $(COMPOSE) --env-file "$(ENV_FILE)" logs -f

status:                     ## 查看所有服务状态和健康检查
	@ENV_FILE="$(ENV_FILE)" bash $(SCRIPTS)/status.sh

hot-update:                 ## 热更新本地部署容器源码 (不 pip、不重建镜像)
	@bash $(SCRIPTS)/hot-update.sh --env "$(ENV_FILE)" $(ARGS)

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

dev-compose:                ## 从源码构建并挂载后端服务，启用热重载
	@bash $(SCRIPTS)/validate-env.sh --env "$(ENV_FILE)" --config-only
	@$(DEV_COMPOSE) --env-file "$(ENV_FILE)" up -d --build --remove-orphans gateway knowledge-service knowledge-worker agent-runtime agent-capability-worker frontend
	@echo "Development compose is running with backend source mounts and uvicorn reload."

dev-compose-logs:           ## 查看源码挂载开发服务日志
	@$(DEV_COMPOSE) --env-file "$(ENV_FILE)" logs -f gateway knowledge-service knowledge-worker agent-runtime agent-capability-worker frontend

# -- Agent Trace / Eval Development Gates ------------------------------------

.PHONY: verify-agent-studio verify-eval-dev agent-eval-core-gate agent-runtime-eval-contract-gate eval-e1-gate eval-e1-unit-gate eval-regression-gate rag-eval-regression-gate verify-assistant-runtime-dev test-isolation

EVAL_REGRESSION_REPORT_DIR ?= tmp/eval-regression
EVAL_E1_ARTIFACT_DIR ?= tmp/eval-e1
EVAL_UV_RUN ?= uv run --all-packages --extra test
EVAL_RUFF_RUN ?= uv run --all-packages --extra dev

verify-agent-studio:        ## 运行 AS-00~AS-08 版本化 Agent Studio 整体回归门禁
	@uv run python scripts/agent_studio_regression.py $(ARGS)

verify-assistant-runtime-dev: ## 运行 Assistant Runtime 离线回归门禁 (AHR-01~AHR-04)
	@$(EVAL_UV_RUN) python scripts/assistant_runtime_regression.py gate --no-write

agent-runtime-eval-contract-gate: ## 验证 V2 Agent Runtime 的离线 Thread/Turn/Item 质量合同
	@$(EVAL_UV_RUN) pytest -q --no-cov \
		tests/services/eval/test_agent_runtime_eval_contract.py \
		tests/services/eval/test_eval_candidate_client.py

agent-eval-core-gate:       ## 运行 Agent Runtime 候选执行、统计、评估器与多代理核心门禁
	@$(MAKE) agent-runtime-eval-contract-gate
	@$(EVAL_UV_RUN) pytest -q --no-cov \
		tests/services/eval/test_candidate_runner.py \
		tests/services/eval/test_experiment_statistics.py \
		tests/services/eval/test_eval_candidate_client.py \
		tests/services/eval/test_evaluator_executor.py
	@echo "Agent Runtime live-provider candidate is not run by this offline gate; use the explicit V2 live gate with AGENT_EVAL_AUTH_TOKEN or AGENT_EVAL_API_KEY."

verify-eval-dev:            ## 运行 Agent Trace/Eval dev 分支验证门禁
	@$(EVAL_RUFF_RUN) ruff check \
		src/api/v1/eval.py \
		src/api/schemas/eval.py \
		src/api/eval_export.py \
		packages/ai-gateway-core/src/ai_gateway_core/eval \
		packages/ai-gateway-core/src/ai_gateway_core/persistence/repositories/agent_trace_repository.py \
		tests/api/test_eval_traces.py \
		tests/api/test_eval_api_trace_tree.py \
		tests/services/eval \
		tests/services/eval/test_agent_runtime_eval_contract.py
	@$(EVAL_UV_RUN) pytest -q --no-cov \
		tests/api/test_eval_traces.py \
		tests/api/test_eval_api_trace_tree.py \
		tests/services/eval/test_eval_permissions.py
	@$(EVAL_UV_RUN) pytest -q --no-cov \
		tests/services/eval/test_evaluator_executor.py \
		tests/services/eval/test_outbox_worker.py \
		tests/services/eval/test_online_sampling.py \
		tests/services/eval/test_eval_llm_client.py \
		tests/services/eval/test_golden_regression_gate.py \
		tests/services/eval/test_trace_retention_scheduler.py \
		tests/services/eval/test_search_indexes.py \
		tests/services/eval/test_drive_shipped_entrypoints.py
	@$(MAKE) eval-e1-gate
	@$(EVAL_UV_RUN) pytest -q --no-cov \
		tests/services/eval/test_agent_runtime_eval_contract.py
	@$(EVAL_UV_RUN) pytest -q --no-cov \
		tests/services/eval/test_ingest_roundtrip.py \
		tests/services/eval/test_trace_capture_helpers.py
	@corepack pnpm@10.33.0 -C web lint
	@corepack pnpm@10.33.0 -C web type-check

eval-regression-gate:       ## 运行离线 Assistant Eval golden regression gate
	@$(EVAL_UV_RUN) python scripts/eval_golden.py validate tests/fixtures/eval/golden/assistant_regression_v1.jsonl
	@$(EVAL_UV_RUN) python scripts/eval_golden.py gate tests/fixtures/eval/golden/assistant_regression_v1.jsonl \
		--observations tests/fixtures/eval/observations/assistant_regression_v1.jsonl \
		--output $(EVAL_REGRESSION_REPORT_DIR)/latest.json \
		--markdown $(EVAL_REGRESSION_REPORT_DIR)/latest.md

rag-eval-regression-gate:   ## 验证离线双轨 RAG Eval fixture contract（不调用 provider）
	@$(EVAL_UV_RUN) python scripts/eval_rag.py validate tests/fixtures/eval/rag/golden/rag_regression_v1.jsonl \
		--observations tests/fixtures/eval/rag/observations/rag_regression_v1.jsonl
	@$(EVAL_UV_RUN) python scripts/eval_rag.py gate tests/fixtures/eval/rag/golden/rag_regression_v1.jsonl \
		--observations tests/fixtures/eval/rag/observations/rag_regression_v1.jsonl \
		--output $(EVAL_E1_ARTIFACT_DIR)/rag-latest.json

eval-e1-unit-gate:          ## 运行 Agent stateful 与 RAG Eval E1 单元门禁
	@$(EVAL_UV_RUN) pytest -q --no-cov \
		tests/services/eval/test_agent_observation_adapter.py \
		tests/services/eval/test_golden_regression_gate.py \
		tests/services/eval/test_stateful_agent_eval.py \
		tests/services/eval/test_rag_regression_gate.py \
		tests/services/eval/test_retrieval_metrics.py

eval-e1-gate:               ## 运行 Eval E1 离线 fixture-contract 门禁（Agent + RAG）
	@$(MAKE) eval-e1-unit-gate
	@$(MAKE) eval-regression-gate
	@$(MAKE) rag-eval-regression-gate

# -- Agent Runtime isolation gate --------------------------------------------

test-isolation:             ## 运行 Agent Runtime 与 Gateway 隔离契约测试
	@uv run pytest -q --no-cov \
		tests/integration/test_gateway_openapi_contract.py \
		tests/integration/test_gateway_boot.py \
		tests/api/test_assistant_control_plane_routes.py

# -- Help ---------------------------------------------------------------------

.PHONY: help

help:                       ## 显示此帮助信息
	@echo ""
	@echo "AI Gateway 运维命令:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
