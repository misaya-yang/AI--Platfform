use codex_protocol::ThreadId;
use codex_thread_store::AppendThreadItemsParams;
use codex_thread_store::ArchiveThreadParams;
use codex_thread_store::CreateThreadParams;
use codex_thread_store::DeleteThreadParams;
use codex_thread_store::ListThreadsParams;
use codex_thread_store::LoadThreadHistoryParams;
use codex_thread_store::PersistContext;
use codex_thread_store::ReadThreadByRolloutPathParams;
use codex_thread_store::ReadThreadParams;
use codex_thread_store::ResumeThreadParams;
use codex_thread_store::StoredModelContext;
use codex_thread_store::StoredThread;
use codex_thread_store::StoredThreadHistory;
use codex_thread_store::ThreadPage;
use codex_thread_store::ThreadStore;
use codex_thread_store::ThreadStoreError;
use codex_thread_store::ThreadStoreFuture;
use codex_thread_store::UpdateThreadMetadataParams;

use super::PostgresThreadStore;
use super::store_error;
use super::thread_uuid;

impl ThreadStore for PostgresThreadStore {
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }

    fn create_thread(&self, params: CreateThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(PostgresThreadStore::create_thread(self, params))
    }

    fn resume_thread(&self, params: ResumeThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            self.ensure_visible(params.thread_id, params.include_archived)
                .await
        })
    }

    fn append_items(&self, params: AppendThreadItemsParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(PostgresThreadStore::append_items(self, params))
    }

    fn persist_thread(
        &self,
        _thread_id: ThreadId,
        _context: PersistContext,
    ) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }

    fn flush_thread(&self, _thread_id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }

    fn shutdown_thread(&self, _thread_id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }

    fn discard_thread(&self, _thread_id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }

    fn load_history(
        &self,
        params: LoadThreadHistoryParams,
    ) -> ThreadStoreFuture<'_, StoredThreadHistory> {
        Box::pin(PostgresThreadStore::load_history(self, params))
    }

    fn load_latest_model_context(
        &self,
        params: LoadThreadHistoryParams,
    ) -> ThreadStoreFuture<'_, StoredModelContext> {
        Box::pin(async move {
            let history = self.load_history(params).await?;
            Ok(StoredModelContext {
                thread_id: history.thread_id,
                items: history.items,
            })
        })
    }

    fn read_thread(&self, params: ReadThreadParams) -> ThreadStoreFuture<'_, StoredThread> {
        Box::pin(PostgresThreadStore::read_thread(self, params))
    }

    fn read_thread_by_rollout_path(
        &self,
        _params: ReadThreadByRolloutPathParams,
    ) -> ThreadStoreFuture<'_, StoredThread> {
        Box::pin(async {
            Err(ThreadStoreError::Unsupported {
                operation: "read_thread_by_rollout_path",
            })
        })
    }

    fn list_threads(&self, _params: ListThreadsParams) -> ThreadStoreFuture<'_, ThreadPage> {
        Box::pin(async {
            Err(ThreadStoreError::Unsupported {
                operation: "thread/list_requires_platform_scope",
            })
        })
    }

    fn update_thread_metadata(
        &self,
        params: UpdateThreadMetadataParams,
    ) -> ThreadStoreFuture<'_, Option<StoredThread>> {
        Box::pin(PostgresThreadStore::update_thread_metadata(self, params))
    }

    fn archive_thread(&self, params: ArchiveThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(self.set_archived(params.thread_id, true))
    }

    fn unarchive_thread(&self, params: ArchiveThreadParams) -> ThreadStoreFuture<'_, StoredThread> {
        Box::pin(async move {
            self.set_archived(params.thread_id, false).await?;
            self.read_thread(ReadThreadParams {
                thread_id: params.thread_id,
                include_archived: true,
                include_history: false,
            })
            .await
        })
    }

    fn delete_thread(&self, params: DeleteThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let kernel_thread_id = thread_uuid(params.thread_id)?;
            let result = sqlx::query(
                "UPDATE assistant_runtime_thread_projections SET deleted_at = NOW() WHERE kernel_thread_id = $1 AND deleted_at IS NULL",
            )
            .bind(kernel_thread_id)
            .execute(&self.pool)
            .await
            .map_err(store_error)?;
            if result.rows_affected() == 0 {
                return Err(ThreadStoreError::ThreadNotFound {
                    thread_id: params.thread_id,
                });
            }
            sqlx::query(
                "UPDATE assistant_runtime_threads SET deleted_at = NOW() WHERE runtime_thread_id = $1",
            )
            .bind(kernel_thread_id)
            .execute(&self.pool)
            .await
            .map_err(store_error)?;
            Ok(())
        })
    }
}
