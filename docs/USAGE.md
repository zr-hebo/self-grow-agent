<!-- generated-by: gsd-doc-writer -->
# 使用指南

本文说明如何配置直接 LLM 或 Pi Coding Agent、启动 Self-Growing Agent，并通过管理 API 让 Agent 自动创建和热更新业务 API。

## 前置要求与安装

需要：

- Python 3.12 或更高版本
- [uv](https://docs.astral.sh/uv/)
- `curl`（用于执行本文示例）

只有选择 Pi 后端时，才额外需要 Node.js 22.19 或更高版本和 Pi CLI。

在项目根目录安装 Python 3.12 和项目依赖：

```bash
uv python install 3.12
uv sync --python 3.12 --dev
uv run python --version
```

最后一条命令应显示 Python 3.12 或更高版本。

## 从 clone 到第一个功能

下面的流程从一个空目录开始，使用 DeepSeek 让 Agent 创建并立即运行第一个
`GET /quickstart/hello` 业务 API。示例默认使用直接 LLM 后端，不需要安装 Pi。

### 1. 获取代码并安装依赖

```bash
git clone https://github.com/zr-hebo/self-grow-agent.git
cd self-grow-agent
uv python install 3.12
uv sync --python 3.12 --dev
```

### 2. 在当前 shell 注入配置

先通过密码管理器、CI 密钥变量或其他安全方式设置真实的 DeepSeek 密钥。下面的命令只读取该变量；不会把密钥写入仓库、SQLite、日志或命令历史。

```bash
: "${DEEPSEEK_API_KEY:?请先安全注入 DEEPSEEK_API_KEY}"
export MANAGEMENT_API_KEY="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export LLM_API_KEY="$DEEPSEEK_API_KEY"
export LLM_BASE_URL='https://api.deepseek.com'
export LLM_MODEL='deepseek-v4-flash'
export GENERATION_BACKEND='direct'
```

`MANAGEMENT_API_KEY` 是本次本地运行的管理面密码。请保持这个终端开启，或在启动服务的 shell 中使用同一组环境变量。

### 3. 启动 Agent

```bash
uv run python main.py
```

服务默认运行在 `http://127.0.0.1:8000`。新开一个终端，进入同一仓库后通过同一个秘密管理器（或从当前 shell 新建的终端复用环境）注入相同的 `MANAGEMENT_API_KEY`；不要将实际值写进脚本。然后确认服务正常：

```bash
export AGENT_URL='http://127.0.0.1:8000'
curl -sS "$AGENT_URL/healthz"
```

预期返回：

```json
{"code":0,"message":"OK","data":{"status":"ok","event_time":"2026-09-03T20:34:56.789+08:00"}}
```

### 4. 让 Agent 添加第一个 API

```bash
curl -sS -X POST "$AGENT_URL/api/v1/manage/routes" \
  -H 'Content-Type: application/json' \
  -H "X-Management-Key: $MANAGEMENT_API_KEY" \
  -d '{
    "path": "/hello",
    "method": "GET",
    "project": "quickstart",
    "instruction": "创建一个同步处理器：始终返回 JSON 对象 {\"message\": \"hello\"}，不读取请求参数，并将 description 设置为 Say hello。"
  }'
```

成功时先返回 HTTP `202 Accepted`。`requirement_id` 标识可持续编辑的需求，`operation_id` 标识本次独立执行；`operation_url` 用于查询本次执行状态。LLM 的生成、校验和热加载在后台继续执行，因此管理请求不会一直等待模型完成：

```json
{
  "code":0,
  "message":"OK",
  "data":{
    "requirement_id":"<requirement-id>",
    "operation_id":"<operation-id>",
    "status":"accepted",
    "project":"quickstart",
    "path":"/quickstart/hello",
    "method":"GET",
    "operation_url":"/api/v1/manage/operations/<operation-id>"
  }
}
```

轮询 `data.operation_url`，直到 `data.status` 为 `finish`。若状态为 `failed`，查看 `data.last_error` 并调整需求后重试：

```bash
curl -sS "$AGENT_URL/api/v1/manage/operations/<operation-id>" \
  -H "X-Management-Key: $MANAGEMENT_API_KEY"
```

失败的 operation 可以直接重试，不需要重新提交整段需求内容。旧 operation 保持
`failed`，接口返回一个新的 `operation_id` 和 `operation_url`。对于 update/move，
服务会重新读取当前 route/version，避免继续使用失败任务中的过期版本：

```bash
curl -sS -X POST \
  "$AGENT_URL/api/v1/manage/operations/<failed-operation-id>/retry" \
  -H "X-Management-Key: $MANAGEMENT_API_KEY"
```

任务激活后即可访问刚刚创建的功能：

```bash
curl -sS "$AGENT_URL/quickstart/hello"
```

预期响应：

```json
{"code":0,"message":"OK","data":{"message":"hello"}}
```

也可以打开 [本地开发控制台](http://127.0.0.1:8000/console)，输入同一个管理密钥，通过图形界面完成创建、查看和后续迭代。

## 配置管理密钥和 LLM

`config.py` 直接读取当前进程的环境变量。仓库中的 `.env.example` 只是变量清单，应用**不会自动加载 `.env` 文件**。

本指南使用 [DeepSeek Responses API](https://api-docs.deepseek.com/guides/responses_api/) 作为可运行示例。
启动服务前，先通过秘密管理器把真实密钥注入 `DEEPSEEK_API_KEY`，再在同一个 shell 中导出以下变量。
管理密钥在运行时随机生成，两个密钥都不会写入项目文件：

```bash
export MANAGEMENT_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
: "${DEEPSEEK_API_KEY:?请先安全注入 DEEPSEEK_API_KEY}"
export LLM_API_KEY="$DEEPSEEK_API_KEY"
export LLM_BASE_URL='https://api.deepseek.com'
export LLM_MODEL='deepseek-v4-flash'
export LLM_TIMEOUT_SECONDS='30'
```

`deepseek-v4-flash` 适合作为低延迟示例；需要更强模型时，可把 `LLM_MODEL` 改为
`deepseek-v4-pro`。这里使用的是 DeepSeek 当前模型名和不带 `/v1` 的官方基础地址。
下表仍列出 `config.py` 在没有显式环境变量时的代码默认值。

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `MANAGEMENT_API_KEY` | 空 | 管理 API 的 `X-Management-Key`。非空时至少 16 个字符；为空时所有管理请求都会被拒绝。 |
| `LLM_API_KEY` | 空 | LLM 凭据。为空时已有动态路由仍能运行，但创建和更新请求返回 `503`。 |
| `LLM_BASE_URL` | `https://api.deepseek.com` | DeepSeek Responses API 的基础地址；可覆盖为其他兼容服务。 |
| `LLM_MODEL` | `deepseek-v4-flash` | 用于生成处理器的默认 DeepSeek 模型。 |
| `LLM_TIMEOUT_SECONDS` | `30` | 调用 LLM 的超时时间，单位为秒。 |
| `PLUGIN_WORKSPACE_ROOT` | 系统临时目录下的 `self-grow-agent-workspaces` | operation 级插件候选工作区；必须与生成制品目录分离。 |
| `PLUGIN_ARTIFACT_ROOT` | `generated/plugins` | 已验证的不可变插件版本。 |
| `PLUGIN_ALLOWED_DEPENDENCIES` | 空 | 可声明的精确依赖 pin，逗号分隔；依赖必须预装。 |
| `PLUGIN_PROJECT_ENV_ALLOWLIST` | 空 | `project:ENV_NAME` 项列表，只注入指定项目的业务插件进程。 |
| `PLUGIN_MAX_FILES` | `32` | 单个插件 bundle 的文件数上限。 |
| `PLUGIN_MAX_FILE_BYTES` | `262144` | 单个插件文件的 UTF-8 字节上限。 |
| `PLUGIN_MAX_TOTAL_BYTES` | `1048576` | 单个插件全部源码的总字节上限。 |
| `PLUGIN_KEEP_FAILED_WORKSPACES` | `true` | 是否保留失败候选工作区供本地诊断。生产环境通常设置为 `false`。 |

这些变量会在导入 `main.py` 时读取为当前进程的配置快照。修改 LLM、监听地址或运行限制后，需要重启 Agent；通过管理 API 发布动态处理器则不需要重启。

服务启动后会在运行日志中记录管理密钥是否已配置，以及不可逆 SHA-256 指纹和末四位掩码，便于核对客户端与服务端是否使用同一密钥。每次动态业务 API 调用会记录路由、查询参数和 JSON 请求体；异步路由任务还会输出 `route_task` 阶段日志：`accepted`、`generation_started`、`generation_completed`、`completed`，失败时会记录安全错误摘要和耗时。生成失败会区分安全类别，例如 `LLM provider authentication failed`、`LLM provider request timed out`、`LLM returned invalid generated-handler JSON`、`Pi executable was not found`、`Pi RPC emitted invalid JSON` 或 `Pi RPC stream ended before agent_settled`。后两者通常表示当前 `pi` 二进制与 RPC JSONL 协议不兼容或异常退出，应检查 Pi 版本和启动参数。参数和 `accepted` 指令最长记录 1024 个字符；`password`、`token`、`api_key`、`secret`、`密码`、`口令`、`密钥` 等字段的值会替换为 `<redacted>`。使用 `uv run python main.py` 启动时，这些日志显示在当前终端；若通过 systemd、Docker 或其他进程管理器启动，请查看该管理器采集的标准输出。日志绝不会输出完整 `MANAGEMENT_API_KEY`、LLM API Key、LLM 原始推理或生成源码。

## 使用 Pi Coding Agent 后端

第一阶段的 Pi 集成通过官方 RPC 模式运行，每次生成使用一个独立 Pi 进程。安装经过本项目验证的固定版本：

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.84.4
pi --version
```

Pi 0.84.4 要求 Node.js 22.19 或更高版本。然后在启动 Agent 的同一个 shell 中设置：

```bash
: "${DEEPSEEK_API_KEY:?请先安全注入 DEEPSEEK_API_KEY}"
export LLM_API_KEY="$DEEPSEEK_API_KEY"
export GENERATION_BACKEND='pi'
export PI_EXECUTABLE='pi'
export PI_PROVIDER='deepseek'
export PI_MODEL='deepseek-v4-pro'
export PI_TIMEOUT_SECONDS='600'
export PI_MAX_EVENT_STREAM_BYTES='67108864'
export PI_MAX_CONCURRENT_RUNS='1'
export PI_ADMISSION_TIMEOUT_SECONDS='1'
export PI_WORKSPACE_ROOT="$PWD/generated/pi-workspaces"
export PI_PROVIDER_ENV_NAME='DEEPSEEK_API_KEY'
```

`LLM_API_KEY` 由 Python 配置加载后，只通过 `PI_PROVIDER_ENV_NAME` 指定的环境变量传给 Pi，不会出现在 Pi 命令行、SQLite、HTTP 响应或日志中。该变量名必须以 `API_KEY` 或 `TOKEN` 结尾；`PI_PROVIDER` 和 `PI_MODEL` 会作为明确的 RPC 启动参数，避免使用开发者个人 Pi 默认模型。

Pi 进程以以下受控方式运行：

- 使用 `pi --mode rpc --no-session` 和严格 LF 分隔的 JSONL；
- 禁用所有 Pi 工具、extensions、skills、prompt templates、项目 context files 和项目自动信任；
- 每次运行创建独立临时工作目录和独立 `PI_CODING_AGENT_DIR`，避免读取个人 `auth.json`；
- 限制同时运行的 Pi 进程数；槽位满时仅短暂等待，超限返回 `429`；运行超时或请求取消时执行 abort 和进程清理，POSIX 系统会回收整个独立进程组；
- 只有收到 `agent_settled` 且最终 assistant 消息正常结束，才接受生成结果。

Pi 的 `message_update` 等高频流式增量会被逐条校验和计数，但不会保留在内存结果中。`PI_MAX_EVENT_STREAM_BYTES` 限制单次 RPC 原始 JSONL 的累计传输量，默认 64 MiB；单条事件仍限制为 1 MiB，关键非流式事件仍最多保留 10,000 条。若模型确实需要更长输出，可在评估运行时间和带宽后提高该配置并重启服务。

Pi 根据请求的 `execution_mode` 返回两类结果：默认 `restricted` 返回单文件受限 `def handle(request)`；`plugin` 返回完整、多文件、带依赖声明和测试的 JSON bundle。Pi 本身仍不编辑主仓库。插件候选由 Agent 写入外部 operation 工作区，经过策略和测试门禁后发布到不可变制品目录。数据库 schema 迁移、平台依赖升级和管理面源码等核心工程变更不属于动态插件能力，仍需常规评审、部署和重启。

Pi 官方明确说明其本身不是安全沙箱。当前阶段使用 `--no-tools`，只接受模型最终返回的文本；即使如此，公网、多租户或后续允许 Pi 操作源码的生产场景，仍必须在容器、微虚拟机或等价的外部沙箱中运行 Pi。参阅 [Pi RPC 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)、[Provider 配置](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md)和[安全说明](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md)。

需求开发元数据默认保存在 `generated/runtime-metadata.sqlite3`，也可以在启动前覆盖：

```bash
export METADATA_DB_PATH="$PWD/generated/runtime-metadata.sqlite3"
```

数据库使用 SQLite WAL 模式，保存需求、状态、关联路由版本和追加式事件。它不会保存管理密钥、LLM API Key 或生成提示中的外部凭据。动态处理器源码和 `routes.json` 仍由 `GENERATED_DIR` 管理，两种存储职责相互独立。

如果需要使用 `.env` 文件，可以自行把它加载到 shell；这一步不是应用完成的：

```bash
cp .env.example .env
# 编辑 .env，替换其中的占位符，然后执行：
set -a
source .env
set +a
```

## 启动服务与健康检查

启动 Agent：

```bash
uv run python main.py
```

默认监听 `127.0.0.1:8000`。可以通过 `HOST` 和 `PORT` 修改监听地址和端口。

在另一个终端检查服务：

```bash
curl -sS 'http://127.0.0.1:8000/healthz'
```

预期响应：

```json
{"code":0,"message":"OK","data":{"status":"ok","event_time":"2026-09-03T20:34:56.789+08:00"}}
```

## 使用本地开发控制台（推荐）

浏览器打开 [http://127.0.0.1:8000/console](http://127.0.0.1:8000/console)。控制台提供三个工作区：

- **需求开发**：创建和选择持久化需求；
- **Feature Composer**：编辑自然语言需求，保存草稿，或让配置的生成后端生成并热加载功能；
- **Live Runtime**：查看动态路由、基于当前版本继续开发，并直接调用业务 API。

页面连接时输入启动服务所用的 `MANAGEMENT_API_KEY`。密钥只保存在当前页面内存中，不会写入
SQLite、浏览器存储或日志；刷新页面后需要重新输入。所有需求和实现接口仍由
`X-Management-Key` 保护，因此 `/console` 本身可打开并不代表管理数据可匿名读取。

典型流程是：输入需求名称、项目分组、HTTP 方法、业务路径和实现描述，点击“保存草稿”，再点击“生成并热加载”。控制台通过 `revise-and-implement` 快速取得独立 `operation_id`，随后轮询 `operation_url` 并定期刷新 SQLite 时间线，不会让一次 HTTP 管理请求等待完整 LLM/Pi 生成过程。对外状态会依次变为 `draft`、`implementing`、`finish`；`finish` 表示生成、校验和热加载已完成，已发布路由可继续调用。失败时显示 operation 的安全错误摘要并允许编辑后重试。已发布路由上的“继续开发”会创建关联当前版本和项目的需求，成功后版本自动递增。

如果同一路由后来通过其他需求或管理 API 升级，控制台会同时显示“当前版本”和“需求基线”，并出现“同步最新版本”按钮。这个 rebase 必须由用户显式确认，避免静默覆盖其他更新；同步后再生成会基于最新源码继续开发。

## 管理面与业务面

Agent 把 API 分为两个平面：

| 平面 | 路径 | 作用 | 鉴权 |
|---|---|---|---|
| 管理面 | `GET /api/v1/manage/routes?project={project}` | 查看动态路由，可按项目筛选 | 必须提供 `X-Management-Key` |
| 管理面 | `POST /api/v1/manage/routes` | 创建后台 LLM 路由任务，返回 `202` 回执 | 必须提供 `X-Management-Key` |
| 管理面 | `PUT /api/v1/manage/routes/{route_id}` | 让 LLM 替换现有路由逻辑 | 必须提供 `X-Management-Key` |
| 管理面 | `POST /api/v1/manage/routes/{route_id}/move` | 异步重新生成并迁移现有路由 | 必须提供 `X-Management-Key` |
| 管理面 | `POST /api/v1/manage/routes/{route_id}/rollback` | 将插件旧版本重新发布为新版本 | 必须提供 `X-Management-Key` |
| 管理面 | `GET /api/v1/manage/operations?requirement_id={id}` | 查看需求的执行历史 | 必须提供 `X-Management-Key` |
| 管理面 | `GET /api/v1/manage/operations/{id}` | 查询一次异步执行状态 | 必须提供 `X-Management-Key` |
| 管理面 | `POST /api/v1/manage/operations/{id}/retry` | 用失败记录创建新的异步重试任务 | 必须提供 `X-Management-Key` |
| 管理面 | `GET/POST /api/v1/manage/requirements?project={project}` | 列出或保存开发需求，可按项目筛选 | 必须提供 `X-Management-Key` |
| 管理面 | `GET /api/v1/manage/requirements/{id}` | 查询稳定的需求定义及最新状态 | 必须提供 `X-Management-Key` |
| 管理面 | `PATCH /api/v1/manage/requirements/{id}` | 编辑需求内容 | 必须提供 `X-Management-Key` |
| 管理面 | `POST /api/v1/manage/requirements/{id}/revise-and-implement` | 保存修改并异步生成，返回 `202` 回执 | 必须提供 `X-Management-Key` |
| 管理面 | `POST /api/v1/manage/requirements/{id}/implement` | 实现需求并关联路由版本 | 必须提供 `X-Management-Key` |
| 管理面 | `POST /api/v1/manage/requirements/{id}/rebase` | 显式同步关联路由的最新版本 | 必须提供 `X-Management-Key` |
| 管理面 | `GET /api/v1/manage/requirements/{id}/events` | 查看追加式实现时间线 | 必须提供 `X-Management-Key` |
| 业务面 | 例如 `GET /default/hello` | 执行已经发布的动态处理器 | 当前实现不要求管理密钥 |

管理面接收自然语言指令并发布代码；业务面只运行已经通过校验并激活的处理器。动态业务路由支持 `GET`、`POST`、`PUT`、`PATCH` 和 `DELETE`，并按 HTTP 方法和标准化后的完整路径精确匹配。

## 项目分组

创建动态 API 或控制台需求时应填写 `project`，例如 `customer-portal` 或 `billing`。项目名会标准化为小写，必须以字母开头，只能包含小写字母、数字和连字符，最长 63 个字符。创建成功的管理响应会返回规范化后的 `project`。运行时、SQLite 需求记录和控制台会按该字段归类；`GET /api/v1/manage/routes?project=billing` 和 `GET /api/v1/manage/requirements?project=billing` 可只查询一个项目的数据。

项目同时是逻辑分组和 URL 命名空间：项目 `billing` 的相对路径 `/orders` 发布为 `/billing/orders`，默认项目的 `/hello` 发布为 `/default/hello`。已有根路由可通过 `/api/v1/manage/routes/{route_id}/move` 异步重新生成并迁移。

所有 JSON API 的成功响应由 Agent 统一封装；业务处理器 `handle(request)` 或管理 API 的实际返回值始终放在 `data` 中：

```json
{"code":0,"message":"OK","data":null}
```

成功时 `code` 固定为 `0`、`message` 固定为 `OK`，`data` 可以是对象、数组、字符串、数字、布尔值或 `null`。`/healthz` 也使用该成功信封，并在 `data.event_time` 中返回每次调用时生成的 RFC 3339 北京时间（`+08:00`）时间戳。失败时 HTTP 状态码保持语义不变，响应仍是同一结构：`code` 等于 HTTP 状态码、`message` 为安全错误摘要、`data` 为 `null`。控制台默认以北京时间展示需求和事件时间。

## 并发访问业务 API

动态业务请求通过异步分发器并发准入，同步处理器在后台线程和独立子进程中执行，因此不会阻塞服务的事件循环。单个服务进程中的所有动态路由共享以下两个参数：

| 参数 | 默认值 | 并发语义 |
|---|---:|---|
| `MAX_CONCURRENT_HANDLERS` | `4` | 同时执行或已经提交到后台的动态处理器总数上限。 |
| `HANDLER_ADMISSION_TIMEOUT_SECONDS` | `0.1` | 没有空闲槽位时，请求等待准入的最长秒数。 |

等待超时会返回 HTTP `429`、`{"code":429,"message":"dynamic handler capacity is full","data":null}` 和响应头
`Retry-After: 1`。客户端取消请求不会提前释放仍在后台运行的处理器槽位；处理器完成、失败或超时后，槽位才会重新可用。

并发请求在进入动态分发器时取得不可变路由版本快照。某个请求执行期间即使管理 API 发布了新版本，该在途请求仍安全完成旧版本；发布完成后到达的新请求使用新版本，不需要重启服务。

`HANDLER_CPU_LIMIT_SECONDS` 限制生成处理器自身可使用的 CPU 时间，不会把 multiprocessing 子进程启动和服务模块导入消耗算进该预算。达到软限制时会返回 `dynamic handler failed: generated handler exceeded CPU limit`；子进程仍受额外的硬限制与 `HANDLER_TIMEOUT_SECONDS` 墙钟超时保护。

## 示例：让 Agent 自动添加 `GET /demo/hello`

以下命令假定第二个终端已经通过秘密管理器或其他安全方式注入了启动服务时使用的同一个 `MANAGEMENT_API_KEY`；不要在命令或文件中写死该值：

```bash
export AGENT_URL='http://127.0.0.1:8000'

curl -sS -X POST "$AGENT_URL/api/v1/manage/routes" \
  -H 'Content-Type: application/json' \
  -H "X-Management-Key: $MANAGEMENT_API_KEY" \
  -d '{
    "path": "/hello",
    "method": "GET",
    "project": "demo",
    "instruction": "创建一个同步处理器：始终返回 JSON 对象 {\"message\": \"hello\"}，不读取请求参数，并将 description 设置为 Say hello。"
  }'
```

创建成功立即返回 HTTP `202 Accepted`。外层保持统一响应结构，`data` 中包含任务字段；不会返回生成的源码或等待 LLM 完成：

```json
{
  "code": 0,
  "message": "OK",
  "data": {
    "requirement_id": "<requirement-id>",
    "operation_id": "<operation-id>",
    "status": "accepted",
    "project": "demo",
    "path": "/demo/hello",
    "method": "GET",
    "execution_mode": "restricted",
    "operation_url": "/api/v1/manage/operations/<operation-id>"
  }
}
```

`data.requirement_id` 在后续多次修改中保持稳定；每次创建、迁移或 `revise-and-implement` 都产生新的 `data.operation_id`。`data.status=accepted` 只表示服务已接收本次任务。通过 `data.operation_url` 轮询，直到状态变为 `finish`；若变为 `failed`，可读取安全的 `last_error` 摘要。

例如，使用响应中的实际任务 ID 查询：

```bash
curl -sS "$AGENT_URL/api/v1/manage/operations/<operation-id>" \
  -H "X-Management-Key: $MANAGEMENT_API_KEY"
```

任务状态为 `finish` 后，路由已经生效，可以调用：

```bash
curl -sS "$AGENT_URL/demo/hello"
```

预期业务响应：

```json
{"code":0,"message":"OK","data":{"message":"hello"}}
```

## 一次修改需求并重新生成

已发布需求的修改通常需要先 `PATCH`，再调用 `/implement`。如果希望服务端一次完成这两个步骤，可以调用 `revise-and-implement`：它先保存新的标题和指令（将内部状态重置为 `draft`），再异步开始生成；不会等待 LLM 完成。

```bash
curl -sS -X POST \
  "$AGENT_URL/api/v1/manage/requirements/1fc30b79b777468d8ec669d9282acc6b/revise-and-implement" \
  -H 'Content-Type: application/json' \
  -H "X-Management-Key: $MANAGEMENT_API_KEY" \
  -d '{
    "title": "binlog-server: POST /rebuild_replication",
    "instruction": "优化接口：校验请求体中的实例标识，返回清晰的执行计划和参数校验错误。不要连接数据库、不要执行 SQL；MySQL 操作必须调用已实现的受控后端能力。"
  }'
```

响应为 HTTP `202 Accepted`，稳定的 `requirement_id` 不变，本次调用返回新的 `operation_id` 和对应查询地址。服务自动读取并绑定当前路由版本，无需手动 rebase；如果生成期间路由再次变化，本次 operation 会以真实版本冲突失败。存在 `accepted` 或 `implementing` operation 时再次修改会返回 `409`。

## 查看路由并热更新处理逻辑

先查看当前路由和版本：

```bash
curl -sS "$AGENT_URL/api/v1/manage/routes" \
  -H "X-Management-Key: $MANAGEMENT_API_KEY"
```

创建示例路由后，响应的 `data` 是一个数组：

```json
{
  "code": 0,
  "message": "OK",
  "data": [{
    "route_id": "<route-id>",
    "path": "/demo/hello",
    "method": "GET",
    "project": "demo",
    "version": 1,
    "description": "Say hello",
    "execution_mode": "restricted",
    "artifact_digest": null
  }]
}
```

更新时必须传入列表中看到的当前 `version` 作为 `expected_version`：

```bash
curl -sS -X PUT "$AGENT_URL/api/v1/manage/routes/<route-id>" \
  -H 'Content-Type: application/json' \
  -H "X-Management-Key: $MANAGEMENT_API_KEY" \
  -d '{
    "instruction": "完整替换处理器：读取 query 中的 name，缺省值为 world，返回 JSON 对象 {\"message\": \"hello <name>\"}，并将 description 设置为 Greet by name。",
    "expected_version": 1
  }'
```

成功返回 HTTP `200`，同一路由的版本递增：

```json
{
  "code": 0,
  "message": "OK",
  "data": {
    "route_id": "<route-id>",
    "path": "/demo/hello",
    "method": "GET",
    "project": "demo",
    "version": 2,
    "description": "Greet by name",
    "execution_mode": "restricted",
    "artifact_digest": null
  }
}
```

无需停止或重启 Agent，新逻辑会立即处理后续请求：

```bash
curl -sS "$AGENT_URL/demo/hello?name=Tom"
curl -sS "$AGENT_URL/demo/hello"
```

预期分别返回：

```json
{"code":0,"message":"OK","data":{"message":"hello Tom"}}
```

```json
{"code":0,"message":"OK","data":{"message":"hello world"}}
```

如果其他管理请求已经抢先更新了该路由，旧的 `expected_version` 会收到 HTTP `409`；重新查询路由列表，使用最新版本决定是否重试，避免无意覆盖他人的更新。

## 完整 API 插件模式

默认的 `restricted` 模式适合小型 JSON 数据转换。需要普通 Python import、多文件模块、第三方依赖声明和生成物测试时，使用 Pi 后端并显式选择 `plugin`：

```bash
export GENERATION_BACKEND=pi
export PLUGIN_WORKSPACE_ROOT=/var/lib/self-grow-agent/workspaces
export PLUGIN_ARTIFACT_ROOT="$PWD/generated/plugins"
export PLUGIN_ALLOWED_DEPENDENCIES='mysql-connector-python==26.7.0'

# 依赖由部署环境预装；生成过程不会访问包仓库。
uv add 'mysql-connector-python==26.7.0'

curl -sS -X POST "$AGENT_URL/api/v1/manage/routes" \
  -H 'Content-Type: application/json' \
  -H "X-Management-Key: $MANAGEMENT_API_KEY" \
  -d '{
    "path":"/rebuild_replication",
    "method":"POST",
    "project":"binlog-server",
    "execution_mode":"plugin",
    "instruction":"从 JSON body.raw-message 提取 Instance ip:port；使用官方 mysql-connector-python（import mysql.connector）连接并依次执行 stop slave 和 start slave；失败最多重试 2 次；返回每一步的安全结果并提供单元测试。凭据只能读取 request.runtime.environment 中的 MYSQL_USER 和 MYSQL_PASSWORD，不能返回凭据值。"
  }'
```

候选 bundle 先写入 `PLUGIN_WORKSPACE_ROOT/<operation_id>`，再经过路径/依赖/AST 策略检查和生成测试。只有全部通过，才会发布到 `PLUGIN_ARTIFACT_ROOT/<project>/<route-id>/vN` 并原子切换活动路由。失败候选不会替换在线版本；`PLUGIN_KEEP_FAILED_WORKSPACES=false` 会自动清理失败工作区。

业务插件默认拿不到任何父进程环境变量。确需数据库凭据时，由部署系统安全注入变量，并逐项允许给指定项目：

```bash
export MYSQL_USER='visit_user'
: "${MYSQL_PASSWORD:?请由秘密管理器注入 MYSQL_PASSWORD}"
export PLUGIN_PROJECT_ENV_ALLOWLIST='binlog-server:MYSQL_USER,binlog-server:MYSQL_PASSWORD'
```

`MANAGEMENT_API_KEY`、`LLM_API_KEY`、`DEEPSEEK_API_KEY`、Python loader 变量等不能加入该白名单；允许值会出现在该项目插件的 `request["runtime"]["environment"]` 中，也会注入该插件子进程环境，但不会进入生成 prompt、测试进程或 HTTP 请求日志。插件必须避免返回或自行记录这些值。可声明的第三方依赖必须同时满足：精确 `name==version`、位于 `PLUGIN_ALLOWED_DEPENDENCIES`、已经安装在运行环境中。

查看路由返回的 `artifact_digest` 可核对当前不可变制品。将插件 v1 内容回滚并重新发布为 v3（版本始终单调递增）：

```bash
curl -sS -X POST "$AGENT_URL/api/v1/manage/routes/<route-id>/rollback" \
  -H 'Content-Type: application/json' \
  -H "X-Management-Key: $MANAGEMENT_API_KEY" \
  -d '{"target_version":1,"expected_version":2}'
```

控制台也可选择“完整插件”、查看模式/制品摘要，并对插件执行“回滚上一版”。核心平台自身（FastAPI 管理路由、策略和执行器）的修改仍需正常代码发布与服务重启，动态插件只能扩展业务 API。

插件需要记录连接、校验、重试或 SQL 步骤时，应使用标准库 `logging`，不要使用 `print`。worker 会把最多 64 条、每条最多 1024 字符的 `INFO` 及以上日志通过受控协议交给父服务；服务统一记录 `plugin_handler event`，并附带 project、route ID、制品版本和日志级别。项目白名单环境值以及请求中 `password`、`token`、`api_key`、`secret` 等字段值会在父进程再次脱敏。插件仍不得主动记录或返回密码；超出数量的日志会丢弃，过大的协议输出会使本次业务调用安全失败。

## Agent 如何自动生成并发布功能

这里的“自动添加功能”是指：管理员发起 `POST` 或 `PUT` 管理请求后，Agent 自动完成生成、校验、持久化和热加载。Agent 不会在后台自行修改项目源码，也不会在没有管理请求的情况下主动添加功能。

一次创建或更新请求会经过以下步骤：

1. 管理 API 校验 `X-Management-Key`、请求字段、HTTP 方法和路径；更新操作还会先检查 `expected_version`。
2. Agent 把自然语言指令、目标方法和路径发送给配置的直接 Responses API 或 Pi RPC 后端。更新时还会附带当前处理器的完整源码，要求模型返回完整替代版本。
3. `restricted` 模式要求 `source`/`description`，并执行严格 AST 白名单；`plugin` 模式要求完整多文件 bundle，依次执行依赖、源码策略和生成测试门禁。
4. 校验通过后，Agent 先把目标路由版本和源码 SHA-256 作为发布回执写入 SQLite，再把源码版本文件和运行时路由清单写入 `GENERATED_DIR`（默认是 `generated`），最后替换内存中的活动路由。
5. 后续业务请求会解析到最新路由版本，并在短生命周期子进程中重新加载和执行处理器，因此不需要重启 Uvicorn。
6. 如果生成、校验、编译或持久化失败，候选版本不会成为活动版本；更新前的处理器会继续提供服务。若路由已发布但 SQLite 最终状态写入被打断，启动恢复或重复实现会用版本与源码哈希对账，补齐状态而不重复调用 LLM。进程重启时，Agent 会从 `GENERATED_DIR` 中的路由清单恢复最后成功发布的版本。

当前运行时按单个服务进程设计。不要通过多个 Uvicorn worker 运行它，否则各 worker 会持有不同的内存路由快照。

## 编写有效指令与安全限制

把指令写成小而确定的纯数据转换，并明确输入位置、缺省值和预期 JSON 结构。例如：

> 从 `query` 读取 `name`，缺省为 `world`，返回 `{"message": "hello <name>"}`。该对象会被 Agent 自动放入业务响应的 `data` 字段。

默认 `restricted` 处理器必须满足以下限制：

- 源码只能包含一个顶层同步函数，签名必须精确为 `def handle(request)`。
- 不允许导入、装饰器、属性访问、循环、推导式、异步代码、生成器、异常、类、lambda、嵌套函数或私有标识符。
- 只能调用 `get`、`str`、`int`、`float`、`bool`、`len`、`min`、`max`、`sum`、`abs` 和 `round`。
- 不允许访问文件、网络或进程；因此不要要求处理器调用数据库、第三方 API、操作文件或运行命令。
- `request` 是普通 JSON 对象，包含 `method`、`path`、`query`、`headers` 和 `body`。动态 API 的请求体统一为 JSON：无请求体时 `body` 为 `null`，否则为已解析的 JSON 值。POST、PUT、PATCH 的参数默认从 `body` 读取，例如 `get(get(request, "body", {}), "name", "world")`。推荐客户端发送 `Content-Type: application/json`；为兼容 `curl -d '{"name":"OK"}'`，有效 JSON 即使未声明该请求头也会被解析。非 JSON 请求体返回 `422`。读取映射时应使用 `get(mapping, key, default)`。
- 处理器只能返回 JSON 兼容值。输入体、输出结果、执行时间和并发数都受配置限制；内存和 CPU 限制会在操作系统支持时生效。
- 只有显式白名单中的业务请求头会传给处理器；`Authorization`、Cookie、API Key 和管理密钥不会传入生成代码。

LLM 输出始终应视为不可信输入。当前实现使用 AST 白名单和隔离子进程降低风险，但子进程仍使用服务进程的操作系统用户，不能替代容器、虚拟机或 seccomp 等强隔离。管理 API 应只放在可信网络或额外身份认证之后，并使用强密钥。

`plugin` 模式放宽普通 import 和多文件限制，但不会放开 `os`、`subprocess`、`socket`、动态 import、任意文件访问、`eval`/`exec` 或硬编码凭据。它在本地仍是同用户 subprocess 隔离；生产环境应把 `PluginProcessExecutor` 替换为低权限容器/微虚拟机执行器，限制只读根文件系统、网络出口、CPU、内存和墙钟时间。

## 常见错误与排查

| HTTP 状态 | 典型 `message` | 原因与处理 |
|---|---|---|
| `401` | `invalid management key` | 缺少或错误的 `X-Management-Key`；确认客户端和服务启动环境中的值一致。 |
| `404` | `managed route not found` | 更新使用了不存在的 `route_id`；先调用路由列表接口。 |
| `404` | `dynamic route not found` | 业务方法或路径没有精确匹配活动路由。 |
| `409` | 路由已存在、需求执行中或版本不一致 | 不要重复创建同一方法和路径；更新前重新查询版本。关联需求落后时，在控制台确认“同步最新版本”或调用 rebase 接口后再实现。 |
| `413` | `request body is too large` | 请求体超过 `MAX_REQUEST_BODY_BYTES`。 |
| `422` | `dynamic request body must be valid JSON` | 动态 API 的非空请求体不是有效 JSON。 |
| `422` | 具体的路径、方法或代码校验错误 | 路径无效、路径被保留、方法不支持，或者 LLM 生成了不符合安全规则的源码；缩小并明确指令后重试。 |
| `429` | `dynamic handler capacity is full` | 同时运行的处理器达到 `MAX_CONCURRENT_HANDLERS`，且等待超过 `HANDLER_ADMISSION_TIMEOUT_SECONDS`；稍后重试。 |
| `429` | `generation capacity is full` | Pi 进程达到 `PI_MAX_CONCURRENT_RUNS`，且等待超过 `PI_ADMISSION_TIMEOUT_SECONDS`；稍后重试。 |
| `500` | `dynamic handler failed: generated handler raised ZeroDivisionError` 等安全摘要 | 处理器运行失败或返回值不符合 JSON 契约；会返回异常类别但不包含请求值、源码或堆栈。对于数据库、网络和子进程等受限能力，请实现受控后端能力，而不是放进动态处理器。 |
| `500` | `dynamic handler failed: generated handler exceeded CPU limit` | 生成处理器耗尽 `HANDLER_CPU_LIMIT_SECONDS` 的 CPU 预算；简化处理逻辑，或在评估资源风险后提高该配置。 |
| `502` | `LLM provider authentication failed`、`LLM provider request timed out`、`LLM returned invalid generated-handler JSON` 等 | 生成失败会返回不含密钥或上游正文的安全分类；据此检查 API Key、Base URL、模型、网络或模型的结构化输出能力。 |
| `502` | `Pi RPC event stream is too large` | Pi 的累计 JSONL 输出超过 `PI_MAX_EVENT_STREAM_BYTES`；确认没有异常输出循环后，可提高该配置并重启服务，再重试失败 operation。 |
| `503` | `LLM is not configured` | `LLM_API_KEY` 为空，或者 `.env` 没有被导入当前 shell。 |
| `503` | `route publication failed` | `GENERATED_DIR` 无法写入或持久化失败；检查目录权限和磁盘状态。 |
| `504` | `dynamic handler timed out` | 处理器执行超过 `HANDLER_TIMEOUT_SECONDS`；简化生成逻辑或调整限制。 |

其他常见检查：

- `MANAGEMENT_API_KEY` 非空时必须至少 16 个字符，否则服务会在加载配置时失败。
- 使用第三方 LLM 服务时，确认 `LLM_BASE_URL` 实现了 OpenAI Responses API，并支持当前请求使用的严格 JSON Schema 输出格式。
- 动态路由重启后未恢复时，检查启动进程使用的 `GENERATED_DIR` 是否与创建路由时相同，以及其中的路由清单和对应版本源码是否完整。

## 运行测试

运行单元测试：

```bash
make test
```

运行独立 CI/CD 集成测试（会启动真实 Agent 服务进程以及本地 Responses API、Pi RPC stub，不使用真实 LLM 凭据，并包含并发业务请求、Pi 后端生成、控制台需求实现及 SQLite 重启恢复 case）：

```bash
make cicd
```

只运行 Pi coding-agent 集成组：

```bash
uv run python -m cicd_case.run_tests coding_agent

# 完整插件创建、失败保留旧版、重试、重启恢复和回滚
uv run python -m cicd_case.run_tests plugin
```
