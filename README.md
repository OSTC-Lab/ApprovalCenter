# ApprovalCenter

ApprovalCenter 是一个自托管的轻量审批服务。
接入方通过 HTTP 创建审批单，审批人员在 Discord 中同意或拒绝，接入方轮询结果后执行自己的业务逻辑。

## 功能概述

- 单步审批，支持多个审批人员。
- 创建、查询和取消审批单，自动处理审批超时。
- 按接入方隔离数据，支持管理员接入方跨接入方操作。
- 按业务关联标识筛选审批记录。
- 每张审批单附带自定义字节数据，支持完整覆盖和可选的版本校验。
- 使用 SQLite 持久化，支持配置保留期限和服务健康检查。

## 架构与基本概念

HTTP API、Discord Bot 和定期维护在同一个进程中运行，通过审批服务访问 SQLite。
Discord 卡片异步发布和更新，未完成的同步会在连接恢复或服务重启后继续处理。
截止时间和决定（取消）时间显示在业务字段后的「审批时间」区域，单号、状态和审批人显示在其下方。业务字段已满 25 个时，审批信息显示在紧随其后的附加卡片中。

```mermaid
flowchart LR
    client["接入方"]
    reviewers["审批人员"]
    platform["Discord"]
    database[("SQLite")]

    subgraph center["ApprovalCenter（单进程）"]
        api["HTTP API"]
        service["审批服务"]
        bot["Discord Bot"]
        maintenance["定期维护"]
    end

    client <-->|创建、查询、取消、覆盖数据| api
    api <-->|处理审批单| service
    service <-->|保存和读取| database
    reviewers -->|同意或拒绝| platform
    platform <-->|卡片与按钮交互| bot
    bot -->|提交审批决定| service
    maintenance -->|处理超时、清理过期数据| service
    maintenance -->|同步卡片| bot
```

### 接入方与审批人员

接入方以 `client_id` 标识，通过 `client_secret` 鉴权，两者均在 TOML 配置中定义。
审批单归创建它的接入方所有，修改密钥或展示名称不改变归属。
管理员接入方可以操作其他接入方的单据，便于调试和管理。

审批人员使用 Discord 身份，匹配配置中的用户白名单或角色白名单即可审批。
接入方的管理员属性仅影响 HTTP 权限，Discord 管理员身份也不会自动获得审批资格。

### 审批单

每张审批单有一个自增整数 `approval_id`，清理后的单号不会再次使用。
接入方提交标题、说明和展示字段，中心负责保存与展示，不解释其中的业务含义。

可选字段 `reference_key` 用于关联接入方的业务对象，例如某个服务器上的玩家。
创建后不可修改，同一个标识可以关联多张单据。标识格式和归属范围由接入方定义。

所有接口时间均使用整数 Unix 时间戳，单位为秒。

| 状态        | 含义                             |
|-------------|----------------------------------|
| `pending`   | 等待审批                         |
| `approved`  | 已同意                           |
| `rejected`  | 已拒绝                           |
| `timed_out` | 截止前未形成有效决定，已超时     |
| `cancelled` | 已由所属接入方或管理员接入方取消 |

除 `pending` 外，其余状态均为终态，常规审批和取消操作不能再变更。
管理员接入方可通过状态调整接口修改终态。
`expires_at` 限定审批和取消的时间窗口，已同意的单据过了截止时间仍保持 `approved`。
接入方负责定义授权有效期、执行次数和具体操作。

### 自定义数据与保留期限

每张审批单附带一份自定义字节数据，HTTP 中使用标准 Base64 编码。
中心不解释这份数据，也不将其展示给审批人员。

数据版本从 `0` 开始，每次成功覆盖后递增，同时更新审批单的 `updated_at`。
覆盖时可以提供预期版本，防止覆盖其他调用方刚写入的数据。
终态单据在保留期内仍可覆盖自定义数据，覆盖不会修改审批内容或延长保留期限。

单据保留至 `expires_at + retention_seconds`，超过保留期限后无法通过接口访问，并由定期维护清理。
审批和取消均在操作时检查截止时间，维护间隔不会延长审批窗口。

## 运行与部署

### 环境要求

- 源码部署：Python 3.13 或更高版本，以及 [uv](https://docs.astral.sh/uv/)。
- 容器部署：Docker；使用 Compose 模板时需要 Docker Compose v2。
- Discord Bot：能够访问配置中的服务器和审批频道。

Bot 需要 View Channel、Send Messages、Embed Links、Read Message History 权限。
仅使用 Guilds intent，无需开启 Message Content 或 Server Members 特权 intent。

### 源码部署

在仓库根目录安装依赖并准备配置：

```bash
uv sync --locked --no-dev --python 3.13
cp config.example.toml config.toml
```

编辑 `config.toml`，填写 Bot token、Discord ID、审批白名单和接入方密钥，然后启动：

```bash
.venv/bin/python src/main.py --config config.toml
```

默认配置路径为 `config.toml`，也可以通过 `--config` 指定。修改配置后重启生效。

### Docker 部署

镜像：`ghcr.io/ostc-lab/approvalcenter:master`，支持 `linux/amd64`。

以 [config.docker.toml](config.docker.toml) 为模板填写配置，挂载至容器内的 `/config/config.toml`，
并持久化 `/data`。
仓库提供一个可复制使用的[最简 Compose 模板](docker/docker-compose.yml)。

### 运行管理与开发

服务应以单实例运行。日志输出到控制台，不记录凭据和自定义数据。
Discord 不可用时 HTTP 仍可提供服务，健康接口报告降级状态。
接口文档位于 `/docs`，OpenAPI 描述位于 `/openapi.json`。

管理页面位于 `/admin`，使用启用且 `is_admin=true` 的接入方凭据登录。
页面支持筛选审批单、查看详情与自定义数据、调整审批状态。刷新页面后需重新登录。
对外提供管理页面时应通过 HTTPS 访问。

安装开发依赖并运行检查：

```bash
uv sync --locked --python 3.13
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m mypy src sdk/approval_center_sdk.py
```

## 配置明细

完整配置参考 [config.example.toml](config.example.toml)，容器环境参考 [config.docker.toml](config.docker.toml)。
配置中未定义的字段会被拒绝，Discord ID 必须写成十进制字符串。
至少配置一个接入方，并设置审批用户或审批角色白名单。

### 服务：`[service]`

| 字段       | 类型   | 默认值                      | 说明                             |
|------------|--------|-----------------------------|----------------------------------|
| `host`     | 字符串 | `"127.0.0.1"`               | 监听地址，容器内使用 `"0.0.0.0"` |
| `port`     | 整数   | `8731`                      | 监听端口，范围 1–65535           |
| `database` | 字符串 | `"approval_center.sqlite3"` | SQLite 文件路径                  |

数据库相对路径以配置文件所在目录为基准。容器部署使用 `/data/approval_center.sqlite3`。

### Discord：`[discord]`

| 字段                | 类型       | 默认值 | 说明                      |
|---------------------|------------|--------|---------------------------|
| `token`             | 字符串     | 必填   | Bot token，不可为空       |
| `proxy_url`         | 字符串     | 不设置 | 可选的 `http://` 代理地址 |
| `guild_id`          | 字符串     | 必填   | Discord 服务器 ID         |
| `channel_id`        | 字符串     | 必填   | 审批文字频道或线程 ID     |
| `reviewer_ids`      | 字符串数组 | `[]`   | 可审批的用户 ID           |
| `reviewer_role_ids` | 字符串数组 | `[]`   | 可审批的角色 ID           |

两项白名单至少有一项非空。审批时检查用户的当前身份和角色。
修改频道配置只影响新建单据，已有单据保留原频道。

不设置 `proxy_url` 时直接连接。代理示例：`proxy_url = "http://127.0.0.1:7890"`。
需要认证时使用 `http://username:password@host:port`，凭据中的特殊字符需做百分号编码。
代理地址必须包含主机名，可指定端口；路径只允许为空或 `/`，不能包含查询参数或片段。
空值和非 `http://` 协议会被拒绝。

代理必须支持 CONNECT，用于 Discord HTTPS 和 Gateway WebSocket 连接。
配置后所有 Bot 连接均使用该代理，代理失败时不会改用直连。系统代理环境变量不生效。

### 接入方：`[[clients]]`

每个配置项定义一个接入方。

| 字段            | 类型   | 默认值  | 说明                                                    |
|-----------------|--------|---------|---------------------------------------------------------|
| `client_id`     | 字符串 | 必填    | 唯一、稳定的接入方标识                                  |
| `client_secret` | 字符串 | 必填    | HTTP Basic 密钥，不可为空                               |
| `display_name`  | 字符串 | 必填    | 卡片展示名称，不可全为空白，最多 256 个 UTF-16 代码单元 |
| `enabled`       | 布尔值 | `true`  | 是否允许该接入方鉴权                                    |
| `is_admin`      | 布尔值 | `false` | 是否允许访问其他接入方的审批单和数据                    |

`client_id` 长度为 1–128，只允许 `A-Z`、`a-z`、`0-9`、`_`、`.`、`-`。
禁用接入方不会删除其单据。示例中的管理员接入方默认禁用，启用前需填写密钥。

### 策略：`[policy]`

| 字段                   | 类型 | 默认值     | 说明                                   |
|------------------------|------|------------|----------------------------------------|
| `maintenance_interval` | 数值 | `5`        | 维护间隔，单位为秒                     |
| `max_approval_seconds` | 整数 | `604800`   | 创建至截止时间的最大间隔，默认 7 天    |
| `retention_seconds`    | 整数 | `15552000` | 截止后保留时长，默认 180 天            |
| `max_data_bytes`       | 整数 | `1048576`  | 自定义数据解码后的大小上限，默认 1 MiB |

以上数值必须为正数。修改保留时长会影响已有单据。

### 日志：`[logging]`

| 字段    | 类型   | 默认值   | 说明                              |
|---------|--------|----------|-----------------------------------|
| `level` | 字符串 | `"INFO"` | Python 标准日志级别，不区分大小写 |

支持 `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`、`NOTSET`。

## API 用法与接口

### 通用格式与鉴权

业务接口使用 HTTP Basic：用户名为 `client_id`，密码为 `client_secret`。
将 `client_id:client_secret` 做标准 Base64 编码，放入 `Authorization` 请求头：

```http
Authorization: Basic <base64(client_id:client_secret)>
Content-Type: application/json
```

有 JSON body 的接口需要 `Content-Type: application/json`。
GET 接口和取消接口没有 JSON body，健康接口无需鉴权。

| 操作                             | 普通接入方 | 管理员接入方     |
|----------------------------------|------------|------------------|
| 创建                             | 归属于自己 | 归属于自己       |
| 列表，`all=false`                | 自己的单据 | 自己的单据       |
| 列表，`all=true`                 | 返回 403   | 全部接入方的单据 |
| 查询审批单、读取或覆盖数据、取消 | 自己的单据 | 任意接入方的单据 |
| 调整审批状态                     | 返回 403   | 任意接入方的单据 |

无法访问的单号与不存在的单号均返回 404。创建时不能指定其他接入方为所有者。

| 方法 | 路径                                    | 成功状态码 |
|------|-----------------------------------------|------------|
| POST | `/api/v1/approval`                      | 201        |
| GET  | `/api/v1/approval`                      | 200        |
| GET  | `/api/v1/approval/{approval_id}`        | 200        |
| POST | `/api/v1/approval/{approval_id}/cancel` | 200        |
| PUT  | `/api/v1/approval/{approval_id}/status` | 200        |
| GET  | `/api/v1/approval-data/{approval_id}`   | 200        |
| PUT  | `/api/v1/approval-data/{approval_id}`   | 200        |
| GET  | `/heathz`                               | 200        |

### 创建审批单

`POST /api/v1/approval`

| 请求字段        | 类型          | 必填 | 说明                                    |
|-----------------|---------------|------|-----------------------------------------|
| `content`       | 对象          | 是   | 审批展示内容，创建后不可修改            |
| `expires_at`    | 整数          | 是   | 审批截止时间                            |
| `data`          | 字符串        | 否   | Base64 自定义数据，默认空字节           |
| `reference_key` | 字符串或 null | 否   | 业务关联标识，默认 null，创建后不可修改 |

截止时间必须晚于服务当前时间，且间隔不超过 `max_approval_seconds`。

`content` 的结构与限制：

| 字段              | 类型     | 默认值  | 限制                                       |
|-------------------|----------|---------|--------------------------------------------|
| `title`           | 字符串   | 必填    | 不可全为空白，最多 256 个 UTF-16 代码单元  |
| `description`     | 字符串   | `""`    | 最多 4096 个 UTF-16 代码单元               |
| `fields`          | 对象数组 | `[]`    | 按顺序展示，最多 25 个字段                 |
| `fields[].name`   | 字符串   | 必填    | 不可全为空白，最多 256 个 UTF-16 代码单元  |
| `fields[].value`  | 字符串   | 必填    | 不可全为空白，最多 1024 个 UTF-16 代码单元 |
| `fields[].inline` | 布尔值   | `false` | 是否允许并排展示                           |

标题、说明及所有字段名称和值的总长度不得超过 5500 个 UTF-16 代码单元。
请求 body 和 `content` 中未定义的字段会被拒绝。

请求 JSON：

```json
{
  "content": {
    "title": "申请回档",
    "description": "恢复误操作造成的损失",
    "fields": [
      {"name": "玩家", "value": "Steve", "inline": true},
      {"name": "服务器", "value": "survival", "inline": true},
      {"name": "目标备份", "value": "#123", "inline": false}
    ]
  },
  "expires_at": 1790866200,
  "reference_key": "survival/Steve",
  "data": ""
}
```

实际调用时需将 `expires_at` 替换为未来时间。

响应 JSON：

```json
{
  "approval_id": 1,
  "reference_key": "survival/Steve",
  "status": "pending",
  "created_at": 1790865600,
  "expires_at": 1790866200,
  "updated_at": 1790865600
}
```

创建成功后返回单号，Discord 卡片异步发布。

### 查询审批单

`GET /api/v1/approval/{approval_id}`

| 响应字段        | 类型          | 说明                                                      |
|-----------------|---------------|-----------------------------------------------------------|
| `approval_id`   | 整数          | 审批单号                                                  |
| `client_id`     | 字符串        | 所属接入方                                                |
| `reference_key` | 字符串或 null | 创建时的业务关联标识                                      |
| `content`       | 对象          | 原始展示内容                                              |
| `status`        | 字符串        | 审批单状态，取值见前文                                    |
| `created_at`    | 整数          | 创建时间                                                  |
| `expires_at`    | 整数          | 截止时间                                                  |
| `updated_at`    | 整数          | 最近一次状态或自定义数据变化时间                          |
| `decision`      | 对象或 null   | 待审批时为 null，终态时包含 `reviewer_name` 和 `decided_at` |
| `data`          | 字符串        | 当前 Base64 自定义数据                                    |
| `data_version`  | 整数          | 当前数据版本                                              |

`decision.reviewer_name` 保存操作发生时的展示名称：Discord 审批使用操作人的展示名称，
管理员状态调整使用接入方配置的 `display_name`。后续改名不影响已有决定信息。
超时时名称为 null，时间等于 `expires_at`；通过取消接口取消时名称为 null，时间为取消时间。

接入方轮询该接口获取状态变化。多次更新可能发生在同一秒，版本校验应使用数据版本。

### 列出审批单

`GET /api/v1/approval`

| 查询参数         | 类型   | 默认值  | 说明                           |
|------------------|--------|---------|--------------------------------|
| `status`         | 字符串 | 不设置  | 按一个审批状态筛选             |
| `reference_key`  | 字符串 | 不设置  | 按业务关联标识精确匹配         |
| `created_from`   | 整数   | 不设置  | 创建时间下界，包含该时间       |
| `created_before` | 整数   | 不设置  | 创建时间上界，不包含该时间     |
| `updated_from`   | 整数   | 不设置  | 更新时间下界，包含该时间       |
| `updated_before` | 整数   | 不设置  | 更新时间上界，不包含该时间     |
| `limit`          | 整数   | `100`   | 每页条数，范围 1–1000          |
| `offset`         | 整数   | `0`     | 跳过的条数，必须非负           |
| `all`            | 布尔值 | `false` | 管理员接入方是否查询全部接入方 |

同时设置时间上下界时，下界必须小于上界。结果按创建时间、单号倒序排列。

不设置 `reference_key` 时不限制标识，包括标识为 null 的单据。
设置后按字符串精确匹配，区分大小写，null 不会命中。
查询值 `null` 是普通字符串，空查询值只匹配空字符串。

响应包含 `items`、`limit`、`offset`，每个条目都是完整审批单。
增加 `offset` 可以读取后续页面，返回条数少于 `limit` 表示当前已无下一页。
分页反映查询时的数据，不提供固定快照。

### 取消审批单

`POST /api/v1/approval/{approval_id}/cancel`

没有请求 body，成功返回完整审批单。
未到截止时间的 `pending` 单据转为 `cancelled`，记录取消时间。
重复取消已取消的单据返回 200，不修改时间。

已同意、已拒绝和已超时的单据返回 409，错误码为 `approval_not_pending`。
达到截止时间的待审批单先转为 `timed_out`，再返回 409。
单据不存在、无访问权限或超过保留期限时返回 404。

### 调整审批状态

`PUT /api/v1/approval/{approval_id}/status`

仅管理员接入方可调用，成功返回完整审批单。

请求 JSON：

```json
{
  "status": "approved"
}
```

| 目标状态 | 规则 |
|----------|------|
| `pending` | 尚未到截止时间，清除决定信息，重新开放审批 |
| `approved`、`rejected`、`cancelled` | 保留期内允许设置，保存管理员展示名称及操作时间 |
| `timed_out` | 已到截止时间，名称为 null，决定时间为截止时间 |

设置为当前状态时不修改决定信息或更新时间。
修改终态不受常规审批窗口限制，但不能恢复已到截止时间的单据为 `pending`。
截止时间与目标状态不符时返回 `409 invalid_status_transition`。
达到截止时间的待审批单会先落实超时，即使后续状态调整被拒绝，该超时仍会保存。

状态调整不改变截止时间、保留期限或自定义数据，也不撤销接入方已经执行的业务操作。

### 读取自定义数据

`GET /api/v1/approval-data/{approval_id}`

响应 JSON：

```json
{
  "data": "eyJkb25lIjp0cnVlfQ==",
  "version": 1,
  "updated_at": 1790865601
}
```

`updated_at` 是审批单的更新时间，也可能因审批、超时或取消而变化。

### 覆盖自定义数据

`PUT /api/v1/approval-data/{approval_id}`

| 请求字段           | 类型        | 必填 | 说明                               |
|--------------------|-------------|------|------------------------------------|
| `data`             | 字符串      | 是   | 完整的新数据，使用标准 Base64 编码 |
| `expected_version` | 整数或 null | 否   | 可选的当前数据版本                 |

请求 JSON：

```json
{
  "data": "eyJkb25lIjp0cnVlfQ==",
  "expected_version": 0
}
```

成功后返回新版本和审批单更新时间：

```json
{
  "version": 1,
  "updated_at": 1790865601
}
```

预期版本与当前版本不符时返回 409，原数据保持不变。
省略 `expected_version` 或传 null 时直接覆盖，`data` 传空字符串时清空数据。
自定义数据写入与接入方的业务执行不构成跨系统事务，执行和恢复策略由接入方负责。

### 健康检查

`GET /heathz`

响应 JSON：

```json
{
  "status": "ok",
  "service": true,
  "database": true,
  "discord": true,
  "maintenance": true
}
```

健康时返回 200。降级时返回 503，`status` 为 `"degraded"`，组件字段标识异常位置。
Discord 降级期间，HTTP 业务接口仍可能可用。

### 错误响应

失败响应包含稳定的 `code` 和可读的 `message`：

```json
{
  "code": "data_version_conflict",
  "message": "Custom data version does not match"
}
```

| HTTP 状态码 | 错误码                  | 含义                                          |
|-------------|-------------------------|-----------------------------------------------|
| 401         | `authentication_failed` | 凭据缺失、错误，或接入方已禁用                |
| 403         | `forbidden`             | 普通接入方请求 `all=true` 或调整审批状态      |
| 404         | `approval_not_found`    | 单据不存在、无访问权限或超过保留期限          |
| 409         | `data_version_conflict` | 自定义数据版本不符                            |
| 409         | `approval_not_pending`  | 单据已处于其他终态，无法取消                  |
| 409         | `invalid_status_transition` | 截止时间与目标状态不符                    |
| 422         | `invalid_request`       | 请求 body、参数、截止时间、展示内容或数据无效 |
| 500         | `internal_error`        | 服务内部异常                                  |

路径不存在、方法不支持等框架错误使用 `http_error`，状态码与具体错误对应。
鉴权失败时返回 `WWW-Authenticate: Basic` 响应头。

## Python SDK

[sdk/approval_center_sdk.py](sdk/approval_center_sdk.py) 可直接复制到接入方项目。
SDK 支持 Python 3.9 及以上版本，依赖 HTTPX 和 Pydantic v2，提供同步与异步客户端。
每个接口接收对应的 Request 模型，返回对应的 Response 模型。
