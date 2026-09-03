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
`GET /hello` 业务 API。示例默认使用直接 LLM 后端，不需要安装 Pi。

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

成功时返回 HTTP `201`；Agent 会校验生成的处理器、写入本地运行数据并热加载路由，无需重启服务。随后即可访问刚刚创建的功能：

```bash
curl -sS "$AGENT_URL/hello"
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

这些变量会在导入 `main.py` 时读取为当前进程的配置快照。修改 LLM、监听地址或运行限制后，需要重启 Agent；通过管理 API 发布动态处理器则不需要重启。

服务启动后会在运行日志中记录管理密钥是否已配置，以及不可逆 SHA-256 指纹和末四位掩码，便于核对客户端与服务端是否使用同一密钥。日志绝不会输出完整 `MANAGEMENT_API_KEY`、LLM API Key 或其他凭据。

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

当前阶段有意保持原来的安全发布边界：Pi 最终仍必须返回一个受限的 `def handle(request)`，随后继续经过 JSON 解析、AST 白名单、试编译、SQLite 发布回执和原子热加载。它不会修改主仓库，也不能借此实现数据库迁移、依赖变更或跨文件 API。此类工程级变更需要后续的隔离代码快照、diff 审批和重启部署通道。

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

典型流程是：输入需求名称、项目分组、HTTP 方法、业务路径和实现描述，点击“保存草稿”，再点击“生成并热加载”。状态会依次变为 `draft`、`implementing`、`active`；失败时记录安全的错误摘要并允许编辑后重试。已发布路由上的“继续开发”会创建关联当前版本和项目的需求，成功后版本自动递增。

如果同一路由后来通过其他需求或管理 API 升级，控制台会同时显示“当前版本”和“需求基线”，并出现“同步最新版本”按钮。这个 rebase 必须由用户显式确认，避免静默覆盖其他更新；同步后再生成会基于最新源码继续开发。

## 管理面与业务面

Agent 把 API 分为两个平面：

| 平面 | 路径 | 作用 | 鉴权 |
|---|---|---|---|
| 管理面 | `GET /api/v1/manage/routes?project={project}` | 查看动态路由，可按项目筛选 | 必须提供 `X-Management-Key` |
| 管理面 | `POST /api/v1/manage/routes` | 让 LLM 创建动态路由 | 必须提供 `X-Management-Key` |
| 管理面 | `PUT /api/v1/manage/routes/{route_id}` | 让 LLM 替换现有路由逻辑 | 必须提供 `X-Management-Key` |
| 管理面 | `GET/POST /api/v1/manage/requirements?project={project}` | 列出或保存开发需求，可按项目筛选 | 必须提供 `X-Management-Key` |
| 管理面 | `PATCH /api/v1/manage/requirements/{id}` | 编辑需求内容 | 必须提供 `X-Management-Key` |
| 管理面 | `POST /api/v1/manage/requirements/{id}/implement` | 实现需求并关联路由版本 | 必须提供 `X-Management-Key` |
| 管理面 | `POST /api/v1/manage/requirements/{id}/rebase` | 显式同步关联路由的最新版本 | 必须提供 `X-Management-Key` |
| 管理面 | `GET /api/v1/manage/requirements/{id}/events` | 查看追加式实现时间线 | 必须提供 `X-Management-Key` |
| 业务面 | 例如 `GET /hello` | 执行已经发布的动态处理器 | 当前实现不要求管理密钥 |

管理面接收自然语言指令并发布代码；业务面只运行已经通过校验并激活的处理器。动态业务路由支持 `GET`、`POST`、`PUT`、`PATCH` 和 `DELETE`，并按 HTTP 方法和标准化后的完整路径精确匹配。

## 项目分组

创建动态 API 或控制台需求时应填写 `project`，例如 `customer-portal` 或 `billing`。项目名会标准化为小写，必须以字母开头，只能包含小写字母、数字和连字符，最长 63 个字符。创建成功的管理响应会返回规范化后的 `project`。运行时、SQLite 需求记录和控制台会按该字段归类；`GET /api/v1/manage/routes?project=billing` 和 `GET /api/v1/manage/requirements?project=billing` 可只查询一个项目的数据。

项目是逻辑分组，不是 URL 命名空间：不同项目仍不能发布相同的 HTTP 方法和路径。升级前已存在的路由和需求会自动归入 `default` 项目，保持可恢复性。

所有业务 API 的成功响应由 Agent 统一封装；生成的 `handle(request)` 返回值始终放在 `data` 中：

```json
{"code":0,"message":"OK","data":null}
```

其中 `code` 固定为 `0`，`message` 固定为 `OK`，`data` 可以是对象、数组、字符串、数字、布尔值或 `null`。`/healthz` 也使用该成功信封，并在 `data.event_time` 中返回每次调用时生成的 RFC 3339 北京时间（`+08:00`）时间戳。控制台默认以北京时间展示需求和事件时间。管理 API 保持既有的响应契约；业务失败仍通过对应的 HTTP 状态码和 `detail` 返回。

## 并发访问业务 API

动态业务请求通过异步分发器并发准入，同步处理器在后台线程和独立子进程中执行，因此不会阻塞服务的事件循环。单个服务进程中的所有动态路由共享以下两个参数：

| 参数 | 默认值 | 并发语义 |
|---|---:|---|
| `MAX_CONCURRENT_HANDLERS` | `4` | 同时执行或已经提交到后台的动态处理器总数上限。 |
| `HANDLER_ADMISSION_TIMEOUT_SECONDS` | `0.1` | 没有空闲槽位时，请求等待准入的最长秒数。 |

等待超时会返回 HTTP `429`、`{"detail":"dynamic handler capacity is full"}` 和响应头
`Retry-After: 1`。客户端取消请求不会提前释放仍在后台运行的处理器槽位；处理器完成、失败或超时后，槽位才会重新可用。

并发请求在进入动态分发器时取得不可变路由版本快照。某个请求执行期间即使管理 API 发布了新版本，该在途请求仍安全完成旧版本；发布完成后到达的新请求使用新版本，不需要重启服务。

## 示例：让 Agent 自动添加 `GET /hello`

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

创建成功返回 HTTP `201`。响应只包含以下六个字段，不会返回生成的源码：

```json
{
  "route_id": "get-hello",
  "path": "/hello",
  "method": "GET",
  "project": "demo",
  "version": 1,
  "description": "Say hello"
}
```

`route_id`、路径、方法和版本由运行时确定；`description` 来自 LLM，因此实际措辞可能与示例略有不同。

创建响应返回后，路由已经生效，可以立即调用：

```bash
curl -sS "$AGENT_URL/hello"
```

预期业务响应：

```json
{"code":0,"message":"OK","data":{"message":"hello"}}
```

## 查看路由并热更新处理逻辑

先查看当前路由和版本：

```bash
curl -sS "$AGENT_URL/api/v1/manage/routes" \
  -H "X-Management-Key: $MANAGEMENT_API_KEY"
```

创建示例路由后，响应是一个数组：

```json
[
  {
    "route_id": "get-hello",
    "path": "/hello",
    "method": "GET",
    "project": "demo",
    "version": 1,
    "description": "Say hello"
  }
]
```

更新时必须传入列表中看到的当前 `version` 作为 `expected_version`：

```bash
curl -sS -X PUT "$AGENT_URL/api/v1/manage/routes/get-hello" \
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
  "route_id": "get-hello",
  "path": "/hello",
  "method": "GET",
  "project": "demo",
  "version": 2,
  "description": "Greet by name"
}
```

无需停止或重启 Agent，新逻辑会立即处理后续请求：

```bash
curl -sS "$AGENT_URL/hello?name=Tom"
curl -sS "$AGENT_URL/hello"
```

预期分别返回：

```json
{"code":0,"message":"OK","data":{"message":"hello Tom"}}
```

```json
{"code":0,"message":"OK","data":{"message":"hello world"}}
```

如果其他管理请求已经抢先更新了该路由，旧的 `expected_version` 会收到 HTTP `409`；重新查询路由列表，使用最新版本决定是否重试，避免无意覆盖他人的更新。

## Agent 如何自动生成并发布功能

这里的“自动添加功能”是指：管理员发起 `POST` 或 `PUT` 管理请求后，Agent 自动完成生成、校验、持久化和热加载。Agent 不会在后台自行修改项目源码，也不会在没有管理请求的情况下主动添加功能。

一次创建或更新请求会经过以下步骤：

1. 管理 API 校验 `X-Management-Key`、请求字段、HTTP 方法和路径；更新操作还会先检查 `expected_version`。
2. Agent 把自然语言指令、目标方法和路径发送给配置的直接 Responses API 或 Pi RPC 后端。更新时还会附带当前处理器的完整源码，要求模型返回完整替代版本。
3. LLM 必须按严格 JSON Schema 返回 `source` 和 `description`。Agent 解析响应后，对源码执行 AST 白名单校验并试编译。
4. 校验通过后，Agent 先把目标路由版本和源码 SHA-256 作为发布回执写入 SQLite，再把源码版本文件和运行时路由清单写入 `GENERATED_DIR`（默认是 `generated`），最后替换内存中的活动路由。
5. 后续业务请求会解析到最新路由版本，并在短生命周期子进程中重新加载和执行处理器，因此不需要重启 Uvicorn。
6. 如果生成、校验、编译或持久化失败，候选版本不会成为活动版本；更新前的处理器会继续提供服务。若路由已发布但 SQLite 最终状态写入被打断，启动恢复或重复实现会用版本与源码哈希对账，补齐状态而不重复调用 LLM。进程重启时，Agent 会从 `GENERATED_DIR` 中的路由清单恢复最后成功发布的版本。

当前运行时按单个服务进程设计。不要通过多个 Uvicorn worker 运行它，否则各 worker 会持有不同的内存路由快照。

## 编写有效指令与安全限制

把指令写成小而确定的纯数据转换，并明确输入位置、缺省值和预期 JSON 结构。例如：

> 从 `query` 读取 `name`，缺省为 `world`，返回 `{"message": "hello <name>"}`。该对象会被 Agent 自动放入业务响应的 `data` 字段。

生成的处理器必须满足以下限制：

- 源码只能包含一个顶层同步函数，签名必须精确为 `def handle(request)`。
- 不允许导入、装饰器、属性访问、循环、推导式、异步代码、生成器、异常、类、lambda、嵌套函数或私有标识符。
- 只能调用 `get`、`str`、`int`、`float`、`bool`、`len`、`min`、`max`、`sum`、`abs` 和 `round`。
- 不允许访问文件、网络或进程；因此不要要求处理器调用数据库、第三方 API、操作文件或运行命令。
- `request` 是普通 JSON 对象，包含 `method`、`path`、`query`、`headers` 和 `body`。读取映射时应使用 `get(mapping, key, default)`。
- 处理器只能返回 JSON 兼容值。输入体、输出结果、执行时间和并发数都受配置限制；内存和 CPU 限制会在操作系统支持时生效。
- 只有显式白名单中的业务请求头会传给处理器；`Authorization`、Cookie、API Key 和管理密钥不会传入生成代码。

LLM 输出始终应视为不可信输入。当前实现使用 AST 白名单和隔离子进程降低风险，但子进程仍使用服务进程的操作系统用户，不能替代容器、虚拟机或 seccomp 等强隔离。管理 API 应只放在可信网络或额外身份认证之后，并使用强密钥。

## 常见错误与排查

| HTTP 状态 | 典型 `detail` | 原因与处理 |
|---|---|---|
| `401` | `invalid management key` | 缺少或错误的 `X-Management-Key`；确认客户端和服务启动环境中的值一致。 |
| `404` | `managed route not found` | 更新使用了不存在的 `route_id`；先调用路由列表接口。 |
| `404` | `dynamic route not found` | 业务方法或路径没有精确匹配活动路由。 |
| `409` | 路由已存在、需求执行中或版本不一致 | 不要重复创建同一方法和路径；更新前重新查询版本。关联需求落后时，在控制台确认“同步最新版本”或调用 rebase 接口后再实现。 |
| `413` | `request body is too large` | 请求体超过 `MAX_REQUEST_BODY_BYTES`。 |
| `422` | 具体的路径、方法或代码校验错误 | 路径无效、路径被保留、方法不支持，或者 LLM 生成了不符合安全规则的源码；缩小并明确指令后重试。 |
| `429` | `dynamic handler capacity is full` | 同时运行的处理器达到 `MAX_CONCURRENT_HANDLERS`，且等待超过 `HANDLER_ADMISSION_TIMEOUT_SECONDS`；稍后重试。 |
| `429` | `generation capacity is full` | Pi 进程达到 `PI_MAX_CONCURRENT_RUNS`，且等待超过 `PI_ADMISSION_TIMEOUT_SECONDS`；稍后重试。 |
| `500` | `dynamic handler failed` 或 `dynamic handler returned an invalid response` | 处理器运行失败或返回值不符合 JSON 契约；通过管理 API 生成更简单的完整替代版本。 |
| `502` | `LLM generation failed` | LLM 请求、严格结构化输出解析或上游服务失败；检查 API Key、Base URL、模型及 LLM 服务日志。 |
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
```
