# Self-Growing Agent

一个用 Python 实现的可自扩展 Agent。处理器可以由直接 LLM 调用或
[Pi Coding Agent](https://github.com/earendil-works/pi) RPC 后端生成。它有两个平面：

- **业务面**：用户调用带项目命名空间的动态 API，例如 `GET /default/hello`。
- **管理面**：管理员用自然语言要求 LLM 新增或修改 API；生成物验证通过后，无需重启进程即可生效。
- **开发控制台**：在 `/console` 保存需求、触发实现、查看 SQLite 时间线和调用已发布 API。

所有 JSON API（业务面、管理面和健康检查）的成功响应统一为 `{"code": 0, "message": "OK", "data": ...}`；实际返回值位于 `data`。失败响应也使用这三个字段，`code` 为 HTTP 状态码、`data` 为 `null`。`/healthz` 在 `data.event_time` 返回调用时的北京时间（`+08:00`）。

每个动态 API 都使用 `project` 作为 URL 命名空间。创建 API 或控制台需求时传入例如 `customer-portal` 的项目名和相对路径 `/orders`，公开地址即为 `/customer-portal/orders`；默认项目也显式使用 `/default` 前缀。控制台按项目显示路由，管理接口支持 `GET /api/v1/manage/routes?project=customer-portal` 和 `GET /api/v1/manage/requirements?project=customer-portal` 筛选。项目名会标准化为小写，必须以字母开头，且只能包含小写字母、数字和连字符（最长 63 个字符）。

管理面是稳定的 FastAPI 路由，业务面由末尾的动态分发器处理。直接创建路由会先在 SQLite 保存一个需求任务并返回 `202 Accepted`，LLM 在后台生成、校验和热加载；可轮询任务状态，避免管理请求长时间占用连接。正在执行的请求继续使用旧处理器，后续请求使用新处理器；失败的更新不会影响旧版本。每次业务调用会在短生命周期子进程中重新加载当前版本，并施加超时、响应大小和并发上限；操作系统支持时还会限制内存与 CPU。

完整的 LLM 配置、启动步骤、自动创建和热更新示例，请参阅 [使用指南](docs/USAGE.md)。

## 快速开始

要求 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。以下示例显式使用
[DeepSeek Responses API](https://api-docs.deepseek.com/guides/responses_api/)，真实密钥应先由秘密管理器注入
`DEEPSEEK_API_KEY`，不要写入仓库或命令历史。

```bash
uv sync --dev

export MANAGEMENT_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
: "${DEEPSEEK_API_KEY:?请先安全注入 DEEPSEEK_API_KEY}"
export LLM_API_KEY="$DEEPSEEK_API_KEY"
export LLM_MODEL='deepseek-v4-flash'
export LLM_BASE_URL='https://api.deepseek.com'

uv run python main.py
```

若要改用 Pi RPC 后端，先安装 `@earendil-works/pi-coding-agent@0.84.4`（Node.js 22.19+），再设置 `GENERATION_BACKEND=pi`、`PI_PROVIDER=deepseek`、`PI_MODEL=deepseek-v4-pro`。第一阶段以 `--no-tools` 运行 Pi，只接收受约束的 handler 文本，不允许它编辑主仓库；完整配置和安全边界见[使用指南](docs/USAGE.md#使用-pi-coding-agent-后端)。

服务默认监听 `127.0.0.1:8000`，入口会从 `config.py` 加载 `HOST` 和 `PORT`。动态业务处理器不依赖 Uvicorn 重启。
启动后打开 [http://127.0.0.1:8000/console](http://127.0.0.1:8000/console)，输入本次启动使用的
`MANAGEMENT_API_KEY`，即可通过图形界面完成需求开发和实现管理。密钥只保存在当前页面内存中。

当前运行时是**单进程模型**：请保持一个 Uvicorn worker。线程内更新由锁和版本比较保护；多个 worker 会各自持有路由快照，不能保证一次管理请求同时更新所有进程。需要横向扩展时，应把路由清单、发布锁和变更通知迁移到共享存储/协调服务。

动态业务 API 支持并发访问。单个服务进程中的所有动态路由共享
`MAX_CONCURRENT_HANDLERS` 个执行槽位（默认 `4`）；请求最多等待
`HANDLER_ADMISSION_TIMEOUT_SECONDS`（默认 `0.1` 秒），超时返回 `429` 和
`Retry-After: 1`。客户端中途断开时，槽位会保留到已经提交的后台处理器真正结束，避免通过取消请求绕过并发上限。

## 添加 hello API

管理请求必须携带 `X-Management-Key`：

```bash
curl -X POST 'http://127.0.0.1:8000/api/v1/manage/routes' \
  -H 'Content-Type: application/json' \
  -H "X-Management-Key: $MANAGEMENT_API_KEY" \
  -d '{
    "path": "/hello",
    "method": "GET",
    "project": "quickstart",
    "instruction": "返回 JSON {message: hello}"
  }'
```

成功响应为 HTTP `202 Accepted`，表示后台任务已保存并开始执行（不是路由已经激活）：

```json
{
  "code": 0,
  "message": "OK",
  "data": {
    "requirement_id": "<requirement-id>",
    "operation_id": "<operation-id>",
    "status": "accepted",
    "project": "quickstart",
    "path": "/quickstart/hello",
    "method": "GET",
    "operation_url": "/api/v1/manage/operations/<operation-id>"
  }
}
```

轮询 `data.operation_url`，直到 `data.status` 为 `finish`；若为 `failed`，请查看 `data.last_error` 并修改后重试：

```bash
curl "http://127.0.0.1:8000/api/v1/manage/operations/<operation-id>" \
  -H "X-Management-Key: $MANAGEMENT_API_KEY"
```

如果 operation 已经是 `failed`，可以直接按原指令重试。服务会创建新的
`operation_id`；更新或迁移任务会自动绑定重试时的当前路由版本：

```bash
curl -sS -X POST \
  "http://127.0.0.1:8000/api/v1/manage/operations/<failed-operation-id>/retry" \
  -H "X-Management-Key: $MANAGEMENT_API_KEY"
```

任务激活后调用业务 API：

```bash
curl 'http://127.0.0.1:8000/quickstart/hello'
```

业务响应会统一封装生成处理器的结果：

```json
{"code":0,"message":"OK","data":{"message":"hello"}}
```

## 修改处理逻辑

更新使用 `expected_version` 做并发比较；版本已经变化时返回 `409`，避免覆盖其他管理请求的结果。

如果修改的是一个已有需求，可将编辑和生成合并为一次异步请求。每次调用都会生成新的 `operation_id`，但 `requirement_id` 保持不变。服务在接收任务时自动绑定当前路由版本；只有生成期间路由又被其他请求更新，发布时才会报告版本冲突。它返回 `202 Accepted`，随后轮询 `data.operation_url`：

```bash
curl -sS -X POST \
  "$AGENT_URL/api/v1/manage/requirements/<requirement-id>/revise-and-implement" \
  -H 'Content-Type: application/json' \
  -H "X-Management-Key: $MANAGEMENT_API_KEY" \
  -d '{
    "title": "binlog-server: POST /rebuild_replication",
    "instruction": "校验实例标识，返回执行计划和参数校验错误；不要直接连接数据库或执行 SQL。"
  }'
```

```bash
curl -X PUT 'http://127.0.0.1:8000/api/v1/manage/routes/<route-id>' \
  -H 'Content-Type: application/json' \
  -H "X-Management-Key: $MANAGEMENT_API_KEY" \
  -d '{
    "instruction": "读取 name 查询参数，返回 JSON {message: hello <name>}；缺省名称为 world",
    "expected_version": 1
  }'

curl 'http://127.0.0.1:8000/quickstart/hello?name=Tom'
```

查看当前动态路由：

```bash
curl 'http://127.0.0.1:8000/api/v1/manage/routes?project=quickstart' \
  -H "X-Management-Key: $MANAGEMENT_API_KEY"
```

## 固定 API

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/healthz` | 健康检查 |
| `GET` | `/console` | 本地需求开发控制台 |
| `GET` | `/api/v1/manage/routes?project={project}` | 列出动态路由，可按项目筛选 |
| `POST` | `/api/v1/manage/routes` | 创建后台 LLM 路由任务，返回 `202` 回执 |
| `PUT` | `/api/v1/manage/routes/{route_id}` | 通过 LLM 更新动态路由 |
| `POST` | `/api/v1/manage/routes/{route_id}/move` | 异步重新生成并迁移路由 |
| `GET` | `/api/v1/manage/operations?requirement_id={id}` | 查看执行记录 |
| `GET` | `/api/v1/manage/operations/{id}` | 查询一次异步执行的状态 |
| `POST` | `/api/v1/manage/operations/{id}/retry` | 用失败记录创建新的异步重试任务 |
| `GET/POST` | `/api/v1/manage/requirements?project={project}` | 列出或保存 SQLite 需求元数据，可按项目筛选 |
| `GET` | `/api/v1/manage/requirements/{id}` | 查询稳定的需求定义及最新状态 |
| `PATCH` | `/api/v1/manage/requirements/{id}` | 编辑需求草稿 |
| `POST` | `/api/v1/manage/requirements/{id}/revise-and-implement` | 保存修改并异步生成，返回 `202` 回执 |
| `POST` | `/api/v1/manage/requirements/{id}/implement` | 生成、校验并发布需求 |
| `POST` | `/api/v1/manage/requirements/{id}/rebase` | 显式同步关联路由的最新版本 |
| `GET` | `/api/v1/manage/requirements/{id}/events` | 查看实现状态时间线 |

业务处理器可以读取一个普通字典：

```python
{
    "method": "GET",
    "path": "/quickstart/hello",
    "query": {"name": "Tom"},
    "headers": {"accept": "*/*"},
    "body": None,
}
```

它必须定义同步函数 `def handle(request)` 并返回可 JSON 序列化的值。动态 API 的请求体统一使用 JSON：有请求体时会作为解析后的 JSON 值传入 `request["body"]`，无请求体时为 `null`。POST 参数默认从该对象读取；推荐客户端发送 `Content-Type: application/json`，而 `curl -d '{"name":"OK"}'` 的有效 JSON 也会被兼容解析。非 JSON 请求体返回 `422`。当前版本支持纯数据转换，刻意不支持导入模块、属性访问、循环、网络、文件或子进程操作。

## 配置

所有参数都在 [`config.py`](config.py) 中集中加载，并可由环境变量覆盖：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` | `127.0.0.1` | 服务监听地址 |
| `PORT` | `8000` | 服务端口 |
| `MANAGEMENT_API_KEY` | 空 | 至少 16 字符；为空时所有管理请求均拒绝 |
| `LLM_API_KEY` | 空 | LLM 密钥；为空时业务路由仍可恢复和运行，但生成请求返回 `503` |
| `LLM_BASE_URL` | `https://api.deepseek.com` | DeepSeek Responses API 地址；可覆盖为其他兼容地址 |
| `LLM_MODEL` | `deepseek-v4-flash` | 默认生成处理器所用的 DeepSeek 模型 |
| `LLM_TIMEOUT_SECONDS` | `30` | LLM 请求超时秒数 |
| `GENERATION_BACKEND` | `direct` | 处理器生成后端：`direct` 或 `pi` |
| `PI_EXECUTABLE` | `pi` | Pi CLI 可执行文件路径 |
| `PI_PROVIDER` | `deepseek` | Pi 使用的模型提供方 |
| `PI_MODEL` | `deepseek-v4-pro` | Pi 使用的模型 |
| `PI_TIMEOUT_SECONDS` | `600` | 单次 Pi RPC 运行超时秒数 |
| `PI_MAX_EVENT_STREAM_BYTES` | `67108864` | 单次 Pi RPC JSONL 事件流累计传输上限（64 MiB） |
| `PI_MAX_CONCURRENT_RUNS` | `1` | 同时运行的 Pi 进程上限 |
| `PI_ADMISSION_TIMEOUT_SECONDS` | `1` | Pi 槽位已满时，生成请求允许等待的秒数 |
| `PI_WORKSPACE_ROOT` | `generated/pi-workspaces` | Pi 临时工作目录父路径 |
| `PI_PROVIDER_ENV_NAME` | `DEEPSEEK_API_KEY` | 在 Pi 子进程中承载 `LLM_API_KEY` 的环境变量名；须以 `API_KEY` 或 `TOKEN` 结尾 |
| `GENERATED_DIR` | `generated` | 版本化处理器和清单目录 |
| `METADATA_DB_PATH` | `generated/runtime-metadata.sqlite3` | 本地需求和实现事件 SQLite 数据库 |
| `MAX_REQUEST_BODY_BYTES` | `1048576` | 动态业务请求体上限 |
| `MAX_HANDLER_RESULT_BYTES` | `1048576` | 动态处理器 JSON 响应上限 |
| `HANDLER_TIMEOUT_SECONDS` | `2` | 生成处理器的墙钟执行上限 |
| `HANDLER_MEMORY_LIMIT_MB` | `256` | 子进程内存上限（平台支持时） |
| `HANDLER_CPU_LIMIT_SECONDS` | `1` | 生成处理器可使用的 CPU 时间预算，不包含子进程启动和模块导入（平台支持时） |
| `MAX_CONCURRENT_HANDLERS` | `4` | 单服务进程同时执行的动态处理器上限 |
| `HANDLER_ADMISSION_TIMEOUT_SECONDS` | `0.1` | 执行槽位满时允许排队的最长时间 |

仓库提供 `.env.example` 作为变量清单，但应用不会自动读取 `.env`；请通过进程管理器、容器配置或 shell 导出这些变量。

服务启动日志会显示管理密钥是否已配置，以及用于核对的不可逆 SHA-256 指纹和掩码尾部；每次动态业务 API 调用还会记录路由、查询参数和 JSON 请求体。`password`、`token`、`api_key`、`secret` 等字段值会自动脱敏，完整 `MANAGEMENT_API_KEY` 与 LLM API Key 不会写入日志。

## 热加载与恢复

每次成功发布会在 `GENERATED_DIR` 中写入：

```text
generated/
├── get-hello.v1.py
├── get-hello.v2.py
├── routes.json
└── runtime-metadata.sqlite3
```

源码文件和 `routes.json` 都使用临时文件 + `os.replace()` 发布。进程重启时会从清单加载最后一个成功版本。生成目录默认被 Git 忽略；部署时若需要跨容器重启恢复，请挂载持久卷。

本地控制台的需求、当前实现状态、关联路由版本和追加事件保存在
`METADATA_DB_PATH`。SQLite 使用 WAL 模式，服务重启后仍可继续编辑或迭代需求；若服务在实现过程中退出，尚未发布的需求会在下次启动时标记为失败并允许重试。SQLite 只保存运行元数据，不保存管理密钥或 LLM API Key。

Agent 会在发布路由前把目标版本和源码 SHA-256 写入 SQLite 作为发布回执。若路由已经发布、但最终状态写入被进程退出或短暂锁冲突打断，下一次启动或重复实现请求会校验回执并补齐对外可见的 `finish` 状态，不会再次调用 LLM 或重复升版。仅打开数据库做查询不会触发中断恢复。

## CI/CD 集成测试

项目按 DTS 的 `cicd_case` 模式提供独立黑盒测试入口：默认先执行 health 门禁，门禁失败会立即阻断后续 case；测试可按组或单个 case 运行，并把终端输出同步写入时间戳日志。

```bash
# 单元测试
make test

# health 门禁 + 全部 lifecycle cases
make cicd

# 只运行一个测试组
uv run python -m cicd_case.run_tests health
uv run python -m cicd_case.run_tests lifecycle
uv run python -m cicd_case.run_tests coding_agent

# 查看和复现单个 case
uv run python -m cicd_case.run_tests --list-cases
uv run python -m cicd_case.run_tests \
  --case lifecycle:test_hot_reload_update

# 指定 CI 制品日志路径
uv run python -m cicd_case.run_tests \
  --log-file cicd-logs/run_tests.log
```

这些 case 会启动真实的 `main.py` 服务进程，并通过真实 TCP 请求验证管理鉴权、动态创建 `hello`、并发业务访问、控制台需求实现、SQLite 元数据重启恢复、无重启热更新、失败更新回滚、路由重启恢复，以及 Pi RPC 后端生成和发布。Responses API 和 Pi RPC 均由本地确定性 stub 提供，Agent 使用运行时随机凭据，因此本地和 CI 都不需要真实 LLM 凭据，也不会向外部 LLM 发请求。

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) 在 pull request、`main` 分支 push 和手动触发时运行：先执行 Ruff、组件测试和编译检查，再执行独立 CI/CD 集成阶段；集成阶段无论成功或失败都会上传 `cicd-integration-logs` 制品。

## 安全边界

LLM 输出始终是不可信输入。本项目会做 AST 白名单校验、限制内置函数、禁止私有标识符，只向处理器传递 JSON 风格数据；请求头采用显式白名单，代理令牌、`Authorization`、Cookie 和 API Key 不会进入生成代码。业务请求体和处理器结果都有大小上限；生成代码在短生命周期子进程执行，父进程会终止超时任务。Unix 平台会尽力设置内存和 CPU 资源限制。

这些措施可以约束常见误生成和资源滥用，但子进程仍与服务使用同一操作系统用户，**不是容器、虚拟机或 seccomp 级的强安全沙箱**。

用于公网或多租户生产环境前，还应：

- 把执行子进程进一步放到独立低权限容器或微虚拟机中，并配置 seccomp、CPU、内存和墙钟上限；
- 禁止执行环境访问宿主文件系统、云元数据和不必要的网络出口；
- 将管理 API 放在内网或强身份认证之后，定期轮换密钥并记录审计日志；
- 对每次生成物运行组织自己的测试、审批或策略扫描后再发布。

## 验证

```bash
uv run ruff check .
make test
make cicd
uv run python -m compileall -q config.py main.py self_grow_agent cicd_case tests
uv run python -c 'from main import app; print(app.title)'
```
