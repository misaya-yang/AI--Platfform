#!/usr/bin/env python
"""Run database migrations for KBMS enhancements - step by step."""

import asyncio
import asyncpg


async def run_migration():
    dsn = "postgresql://postgres:111111@localhost:5433/gateway"
    
    print("Connecting to database...")
    conn = await asyncpg.connect(dsn)
    
    try:
        # Migration steps - each executed separately
        steps = [
            # 1. Create dataset_process_rules table
            (
                "Create dataset_process_rules table",
                """
                CREATE TABLE IF NOT EXISTS dataset_process_rules (
                    id VARCHAR(255) PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    dataset_id VARCHAR(255) NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
                    mode VARCHAR(50) NOT NULL DEFAULT 'automatic',
                    rules JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_by VARCHAR(255),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            ),
            (
                "Create index on dataset_process_rules",
                "CREATE INDEX IF NOT EXISTS idx_process_rules_dataset_id ON dataset_process_rules(dataset_id)"
            ),
            
            # 2. Enhance datasets table
            ("Add indexing_technique to datasets", "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS indexing_technique VARCHAR(50) DEFAULT 'high_quality'"),
            ("Add document_count to datasets", "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS document_count INTEGER NOT NULL DEFAULT 0"),
            ("Add segment_count to datasets", "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS segment_count INTEGER NOT NULL DEFAULT 0"),
            ("Add word_count to datasets", "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS word_count BIGINT NOT NULL DEFAULT 0"),
            ("Add icon_info to datasets", "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS icon_info JSONB DEFAULT '{}'::jsonb"),
            
            # 3. Enhance documents table
            ("Add enabled to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE"),
            ("Add disabled_at to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ"),
            ("Add disabled_by to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS disabled_by VARCHAR(255)"),
            ("Add archived to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE"),
            ("Add archived_reason to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS archived_reason VARCHAR(255)"),
            ("Add archived_by to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS archived_by VARCHAR(255)"),
            ("Add archived_at to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ"),
            ("Add process_rule_id to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS process_rule_id VARCHAR(255)"),
            ("Add word_count to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS word_count INTEGER DEFAULT 0"),
            ("Add segment_count to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS segment_count INTEGER DEFAULT 0"),
            ("Add tokens to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS tokens INTEGER DEFAULT 0"),
            ("Add batch to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS batch VARCHAR(255)"),
            ("Add doc_type to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_type VARCHAR(50)"),
            ("Add doc_form to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_form VARCHAR(50) DEFAULT 'text_model'"),
            ("Add doc_language to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_language VARCHAR(50)"),
            ("Add created_by to documents", "ALTER TABLE documents ADD COLUMN IF NOT EXISTS created_by VARCHAR(255)"),
            ("Create index idx_documents_enabled", "CREATE INDEX IF NOT EXISTS idx_documents_enabled ON documents(enabled)"),
            ("Create index idx_documents_archived", "CREATE INDEX IF NOT EXISTS idx_documents_archived ON documents(archived)"),
            ("Create index idx_documents_batch", "CREATE INDEX IF NOT EXISTS idx_documents_batch ON documents(batch)"),
            
            # 4. Enhance segments table
            ("Add enabled to segments", "ALTER TABLE segments ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE"),
            ("Add disabled_at to segments", "ALTER TABLE segments ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ"),
            ("Add disabled_by to segments", "ALTER TABLE segments ADD COLUMN IF NOT EXISTS disabled_by VARCHAR(255)"),
            ("Add status to segments", "ALTER TABLE segments ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'completed'"),
            ("Add hit_count to segments", "ALTER TABLE segments ADD COLUMN IF NOT EXISTS hit_count INTEGER NOT NULL DEFAULT 0"),
            ("Add word_count to segments", "ALTER TABLE segments ADD COLUMN IF NOT EXISTS word_count INTEGER DEFAULT 0"),
            ("Add keywords to segments", "ALTER TABLE segments ADD COLUMN IF NOT EXISTS keywords JSONB DEFAULT '[]'::jsonb"),
            ("Add answer to segments", "ALTER TABLE segments ADD COLUMN IF NOT EXISTS answer TEXT"),
            ("Add index_node_id to segments", "ALTER TABLE segments ADD COLUMN IF NOT EXISTS index_node_id VARCHAR(255)"),
            ("Add index_node_hash to segments", "ALTER TABLE segments ADD COLUMN IF NOT EXISTS index_node_hash VARCHAR(255)"),
            ("Add created_by to segments", "ALTER TABLE segments ADD COLUMN IF NOT EXISTS created_by VARCHAR(255)"),
            ("Create index idx_segments_enabled", "CREATE INDEX IF NOT EXISTS idx_segments_enabled ON segments(enabled)"),
            ("Create index idx_segments_status", "CREATE INDEX IF NOT EXISTS idx_segments_status ON segments(status)"),
            ("Create index idx_segments_index_node_id", "CREATE INDEX IF NOT EXISTS idx_segments_index_node_id ON segments(index_node_id)"),
            
            # 5. Child chunks table
            (
                "Create child_chunks table",
                """
                CREATE TABLE IF NOT EXISTS child_chunks (
                    id VARCHAR(255) PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    tenant_id VARCHAR(255) NOT NULL DEFAULT '',
                    dataset_id VARCHAR(255) NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
                    document_id VARCHAR(255) NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                    segment_id VARCHAR(255) NOT NULL REFERENCES segments(segment_id) ON DELETE CASCADE,
                    position INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL,
                    word_count INTEGER NOT NULL DEFAULT 0,
                    index_node_id VARCHAR(255),
                    index_node_hash VARCHAR(255),
                    type VARCHAR(50) NOT NULL DEFAULT 'automatic',
                    created_by VARCHAR(255),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    indexing_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ,
                    error TEXT
                )
                """
            ),
            ("Create index idx_child_chunks_segment_id", "CREATE INDEX IF NOT EXISTS idx_child_chunks_segment_id ON child_chunks(segment_id)"),
            ("Create index idx_child_chunks_dataset_id", "CREATE INDEX IF NOT EXISTS idx_child_chunks_dataset_id ON child_chunks(dataset_id)"),
            ("Create index idx_child_chunks_document_id", "CREATE INDEX IF NOT EXISTS idx_child_chunks_document_id ON child_chunks(document_id)"),
            
            # 6. Keyword table
            (
                "Create dataset_keyword_tables",
                """
                CREATE TABLE IF NOT EXISTS dataset_keyword_tables (
                    id VARCHAR(255) PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    dataset_id VARCHAR(255) NOT NULL UNIQUE REFERENCES datasets(dataset_id) ON DELETE CASCADE,
                    keyword_table TEXT NOT NULL DEFAULT '{}',
                    data_source_type VARCHAR(50) NOT NULL DEFAULT 'database',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            ),
            
            # 7. Query history table
            (
                "Create dataset_queries table",
                """
                CREATE TABLE IF NOT EXISTS dataset_queries (
                    id VARCHAR(255) PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    dataset_id VARCHAR(255) NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    source VARCHAR(50) NOT NULL DEFAULT 'api',
                    source_app_id VARCHAR(255),
                    created_by_role VARCHAR(50),
                    created_by VARCHAR(255),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            ),
            ("Create index idx_dataset_queries_dataset_id", "CREATE INDEX IF NOT EXISTS idx_dataset_queries_dataset_id ON dataset_queries(dataset_id)"),
            ("Create index idx_dataset_queries_created_at", "CREATE INDEX IF NOT EXISTS idx_dataset_queries_created_at ON dataset_queries(created_at DESC)"),
        ]
        
        success_count = 0
        skip_count = 0
        
        for name, sql in steps:
            try:
                await conn.execute(sql)
                print(f"✅ {name}")
                success_count += 1
            except asyncpg.exceptions.DuplicateTableError:
                print(f"⏭️  {name} (already exists)")
                skip_count += 1
            except asyncpg.exceptions.DuplicateColumnError:
                print(f"⏭️  {name} (already exists)")
                skip_count += 1
            except asyncpg.exceptions.DuplicateObjectError:
                print(f"⏭️  {name} (already exists)")
                skip_count += 1
            except Exception as e:
                print(f"❌ {name}: {e}")
        
        print(f"\n✅ Migration completed: {success_count} applied, {skip_count} skipped")
        
    finally:
        await conn.close()
        print("Database connection closed.")


if __name__ == "__main__":
    asyncio.run(run_migration())

