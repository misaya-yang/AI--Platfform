# =============================================================================
# AI Gateway - Operations Makefile
# =============================================================================
# 运维只需要记住 make 命令:
#
#   make deploy          部署全部服务 (构建+启动+迁移+健康检查)
#   make deploy-build    部署并强制重新构建镜像
#   make deploy-cn       使用国内镜像构建部署
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
#
#   make help            显示此帮助
# =============================================================================

SHELL := /bin/bash
SCRIPTS := scripts/new
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")

.DEFAULT_GOAL := help

# -- Deployment ---------------------------------------------------------------

.PHONY: deploy deploy-build deploy-cn deploy-infra deploy-app stop logs restart status

deploy:                     ## 部署全部服务 (启动+迁移+健康检查)
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
	@cd "$(shell pwd)" && $(COMPOSE) down

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

.PHONY: dev-setup dev-start dev-stop dev-reset dev-status

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

# -- Help ---------------------------------------------------------------------

.PHONY: help

help:                       ## 显示此帮助信息
	@echo ""
	@echo "AI Gateway 运维命令:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
