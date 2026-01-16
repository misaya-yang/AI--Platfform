# Model Management Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement dynamic provider and model management replacing hardcoded DEFAULT_MODELS

**Architecture:** Database tables + REST API + React UI with tabs

**Tech Stack:** PostgreSQL, FastAPI, React, Ant Design, TanStack Query

---

## Phase 1: Database Schema

### Task 1: Create Migration Script

**Files:**
- Create: `database/migrations/022_llm_providers_models.sql`

**Step 1: Write migration SQL**

```sql
-- Migration: 022_llm_providers_models.sql
-- Description: Add tables for dynamic LLM provider and model management

-- Provider table
CREATE TABLE IF NOT EXISTS llm_providers (
    provider_id VARCHAR(50) NOT NULL,
    tenant_id VARCHAR(100) NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    api_type VARCHAR(20) DEFAULT 'openai',
    base_url VARCHAR(500),
    api_key_encrypted TEXT,
    is_enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (tenant_id, provider_id)
);

-- Model table
CREATE TABLE IF NOT EXISTS llm_models (
    model_id VARCHAR(100) NOT NULL,
    tenant_id VARCHAR(100) NOT NULL,
    provider_id VARCHAR(50) NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    context_window INT DEFAULT 128000,
    max_output_tokens INT DEFAULT 4096,
    supports_vision BOOLEAN DEFAULT false,
    supports_tools BOOLEAN DEFAULT true,
    input_price_per_1k DECIMAL(10,6) DEFAULT 0,
    output_price_per_1k DECIMAL(10,6) DEFAULT 0,
    access_level VARCHAR(20) DEFAULT 'public',
    is_enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (tenant_id, model_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_llm_providers_tenant ON llm_providers(tenant_id);
CREATE INDEX IF NOT EXISTS idx_llm_models_tenant ON llm_models(tenant_id);
CREATE INDEX IF NOT EXISTS idx_llm_models_provider ON llm_models(tenant_id, provider_id);
```

**Step 2: Run migration**

```bash
psql -h localhost -p 5433 -U postgres -d ai_gateway -f database/migrations/022_llm_providers_models.sql
```

---

## Phase 2: Backend API

### Task 2: Create Provider Schemas

**Files:**
- Create: `src/api/schemas/providers.py`

Create Pydantic models for Provider CRUD operations.

### Task 3: Create Model Schemas

**Files:**
- Create: `src/api/schemas/models.py`

Create Pydantic models for Model CRUD operations.

### Task 4: Create Provider Service

**Files:**
- Create: `src/services/llm/provider_service.py`

Implement database CRUD operations for providers with encryption for API keys.

### Task 5: Create Model Service

**Files:**
- Create: `src/services/llm/model_service.py`

Implement database CRUD operations for models.

### Task 6: Create Provider API Router

**Files:**
- Create: `src/api/v1/providers.py`

Implement REST endpoints for provider management.

### Task 7: Create Model API Router

**Files:**
- Create: `src/api/v1/models.py`

Implement REST endpoints for model management.

### Task 8: Register API Routers

**Files:**
- Modify: `src/api/router.py`

Add provider and model routers to the API.

### Task 9: Update ModelRegistry to Load from Database

**Files:**
- Modify: `src/services/assistant/model_registry.py`

Add method to load models from database, fallback to DEFAULT_MODELS if empty.

### Task 10: Update Assistant API to Use Database Models

**Files:**
- Modify: `src/api/v1/assistant.py`

Update `/api/v1/assistant/models` to fetch from database.

---

## Phase 3: Frontend

### Task 11: Create Provider API Client

**Files:**
- Create: `web/src/api/providers.ts`

TypeScript API client for provider CRUD.

### Task 12: Create Model API Client

**Files:**
- Create: `web/src/api/models.ts`

TypeScript API client for model CRUD.

### Task 13: Create ProviderCard Component

**Files:**
- Create: `web/src/components/ProviderCard.tsx`

Card component for displaying a provider.

### Task 14: Create ProviderForm Component

**Files:**
- Create: `web/src/components/ProviderForm.tsx`

Modal form for creating/editing a provider.

### Task 15: Create ModelTable Component

**Files:**
- Create: `web/src/components/ModelTable.tsx`

Table component for displaying models with actions.

### Task 16: Create ModelForm Component

**Files:**
- Create: `web/src/components/ModelForm.tsx`

Modal form for creating/editing a model.

### Task 17: Update Services Page with Tabs

**Files:**
- Modify: `web/src/pages/Services.tsx`

Add Tabs component with three tabs: Services, Providers, Models.

---

## Phase 4: Data Migration

### Task 18: Create Data Seed Script

**Files:**
- Create: `scripts/seed_default_models.py`

Script to seed default providers and models from DEFAULT_MODELS.

### Task 19: Build and Test

Run TypeScript check and test the full flow:
1. Create a provider
2. Create a model
3. Verify model appears in AI Assistant selector
