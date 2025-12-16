# AI Service Gateway v2

基于 `ai-gateway-architecture-v2.md` 的通用 AI 服务网关实现。

## Quick start

1. 安装依赖

```powershell
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
```

2. 启动

```powershell
uvicorn src.main:app --reload --host 0.0.0.0 --port 8080
```

默认会从根目录下的 `services/` 读取服务 YAML 并自动注册。

## API

基础前缀：`/api/v1`

- `POST /invoke` 同步调用
- `POST /stream` SSE 流式调用
- `POST /submit` 异步提交，返回 `task_id`
- `GET /tasks/{task_id}` 查询任务
- `GET /tasks/{task_id}/result` 获取任务结果
- `DELETE /tasks/{task_id}` 取消任务
- `GET /services` 列出服务
- `GET /services/{service_id}` 获取服务详情
- `GET /services/{service_id}/schema` 获取服务 schema
- `GET /health` 网关健康
- `GET /health/services` 所有服务健康

鉴权、限流、熔断、适配器、连接器等细节见文档与 `src/` 实现。
