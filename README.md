# SecondHand Agent

面向个人闲置交易的自主协商卖家助手。系统结合大模型的自然语言理解能力与后端确定性规则，完成商品咨询、报价协商、卖家审批和买家交易意向确认。

同一账号可以作为买家或卖家：买家浏览商品并发起协商，卖家管理商品、私有协商策略、审批和恢复任务。

## 功能特性

- **商品与账号管理**：统一注册登录，支持商品大厅、卖家主页、商品发布与上下架，按账号和资源归属校验权限。
- **受约束协商**：普通咨询与正式报价分离；按卖家净收入划分自动接受、人工审批和禁止接受区间。
- **结构化模型决策**：模型生成候选决策，由后端绑定会话的工具执行；私有价格阈值不提供给模型。
- **审批与最终确认**：审批绑定具体报价和策略版本，处理结果通知买家，再由买家确认当前有效报价。
- **可恢复模型任务**：持久化输入、租约、幂等、有界重试和过期结果复核，支持 Worker 接管及卖家人工处置。
- **可观测与评测**：记录脱敏事件、Token 和估算成本，提供隔离的 A/B/C 对照评测、报告生成与安全门禁。

系统的业务终点是记录交易意向。`AGREED` 不代表付款或实际成交；项目不包含支付、库存锁定和订单履约。

## 架构

~~~mermaid
flowchart TB
    Frontend["Vue 3 前端"] --> Proxy["Vite / Nginx 代理"]
    Proxy --> API["FastAPI：认证、校验与接口"]
    API --> Services["业务 Service：商品、报价、审批、确认"]
    API --> Chat["ChatService：聊天编排与模型任务"]
    Chat --> Agent["SellerAgent：上下文准备与决策应用"]
    Agent -->|"受约束工具"| Services
    Chat --> Provider["LangChain：结构化输出与模型适配"]
    Provider --> Qwen["千问 OpenAI 兼容接口"]
    Worker["独立 ModelTaskWorker"] --> Chat
    Worker --> Followup["ApprovalProcessor：审批通知"]
    Followup --> Provider
    Services --> DB[("MySQL：业务数据、模型任务、观测事件")]
    Chat --> DB
    Followup --> DB
~~~

后端采用模块化单体结构，独立 Worker 处理审批通知、到期重试和故障恢复。聊天接口优先同步执行自己的任务，前端通过消息轮询获取后续结果。

需要模型的请求按以下方式执行：

~~~text
短事务保存输入与任务 → 短事务领取租约 → 事务外调用模型
                                            ↓
                     新短事务复核租约、版本与报价 → 执行并保存结果
~~~

自动接受区和审批区的有效正式报价按后端规则直接路由。所有正式业务动作都经过权限复核；正式回复从已校验的报价与审批事实生成。

### 技术栈

| 层次 | 技术 |
|---|---|
| 前端 | Vue 3、TypeScript、Vue Router、Vite |
| API 与数据契约 | Python 3.11、FastAPI、Pydantic |
| 模型接入 | LangChain 1.x、langchain-openai、Qwen |
| 数据持久化 | MySQL 8.4 / InnoDB、SQLAlchemy 2.x、Alembic |
| 本地运行 | Docker Compose、Nginx、Uvicorn |
| 验证 | Pytest、Ruff、vue-tsc、隔离评测与回归门禁 |

## 快速开始

以下命令使用 Windows PowerShell，默认从项目根目录执行。Docker 方式只需要 Docker Engine / Docker Desktop 与 Compose；源码方式还需要 Python 3.11 和 Node.js `^22.18.0 || >=24.12.0`。

### 1. 配置环境变量

首次运行时复制模板；已有 `.env` 时直接检查配置：

~~~powershell
Copy-Item .env.example .env
~~~

编辑根目录 `.env`，替换所有 `CHANGE_ME` 占位值：

| 配置项 | 要求 |
|---|---|
| `MYSQL_PASSWORD` / `MYSQL_ROOT_PASSWORD` | 设置不同的数据库口令 |
| `DATABASE_URL` | 宿主机后端连接地址，主机名为 `127.0.0.1`，端口与 `MYSQL_PORT` 一致 |
| `CONTAINER_DATABASE_URL` | 容器内连接地址，主机名为 `mysql`，内部端口为 `3306` |
| `AUTH_SECRET` | 至少 32 个字符的随机签名密钥，不包含占位文本 |
| `DEMO_SELLER_PASSWORD` | 至少 12 个字符，用于初始化演示卖家 |
| `MODEL_BASE_URL` / `MODEL_API_KEY` | 运行协商和 Worker 所需的千问兼容接口地址与密钥 |

两个数据库 URL 中的账号、密码和数据库名应与 MySQL 配置一致；密码包含特殊字符时，URL 中的密码部分需要编码。模型地址应与 API Key 所属地域一致，所选模型需要满足项目的结构化输出与模型能力检查。

`.env` 已被 Git 忽略。完整配置说明见 [.env.example](.env.example)，模型与认证配置由后端启动时读取；修改后需要重启 API 和 Worker。

### 2. 启动服务

配置模型后，启动完整应用：

~~~powershell
docker compose up -d --build api worker frontend
docker compose ps
~~~

Compose 会按依赖顺序启动 MySQL，执行 Alembic 迁移和演示数据初始化，再启动 API、Worker 与前端。MySQL 数据保存在命名卷 `mysql_data` 中。

如果暂未配置模型，可以先启动 `api` 和 `frontend`，使用账号及商品管理功能；发送协商消息和启动 Worker 需要有效模型配置：

~~~powershell
docker compose up -d --build api frontend
~~~

### 3. 访问与体验

默认访问地址：

| 入口 | 地址 |
|---|---|
| 前端 | [http://127.0.0.1:5173](http://127.0.0.1:5173) |
| API 文档 | [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) |
| 进程健康 | [http://127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health) |
| 数据库与配置就绪 | [http://127.0.0.1:8000/api/ready](http://127.0.0.1:8000/api/ready) |

`/api/ready` 检查数据库连接，并报告认证和模型配置是否齐全；它不验证真实模型调用是否成功。修改 `API_PORT` / `FRONTEND_PORT` 后，使用对应的访问端口。

演示卖家用户名为 `demo-seller`，密码是配置的 `DEMO_SELLER_PASSWORD`；初始化商品 ID 为 `1001`。

1. 登录演示卖家，查看商品与协商策略。
2. 在另一浏览器或独立会话中注册买家账号。
3. 买家打开商品详情，点击“与卖家协商”，发送咨询或正式报价。
4. 需要审批时，卖家在工作台同意或拒绝；买家轮询获取通知。
5. 买家确认当前有效报价，系统记录交易意向。

种子脚本补齐演示账号、商品和策略，按配置同步演示卖家密码，不创建或重置协商会话。用户不能协商自己发布的商品。

### 服务管理

~~~powershell
# 查看运行日志
docker compose logs --tail 100 api worker

# 修改 .env 后重新创建应用容器，使配置生效
docker compose up -d --force-recreate api worker

# 停止应用，保留数据库运行
docker compose stop frontend worker api
~~~

Compose 的服务端口只绑定本机回环地址，用于本地开发与复现。生产环境的 TLS、发布回滚、备份恢复和监控告警需另行配置。

## 源码开发

### 安装依赖与初始化数据库

在项目根目录创建并激活 Python 3.11 虚拟环境。下面使用 `venv`；也可以使用已有的 Python 3.11 Conda 环境。

~~~powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

docker compose up -d mysql

cd backend
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
python -m alembic upgrade head
python scripts/seed_data.py

cd ..\frontend
npm ci
cd ..
~~~

运行迁移前，通过 `docker compose ps` 确认 MySQL 已健康。使用本地安装的 MySQL 时，配置 `DATABASE_URL` 后可省略容器启动命令。

### 分别启动 API、前端与 Worker

每个终端从项目根目录开始；Python 终端需要激活同一个虚拟环境。

终端一，启动 API：

~~~powershell
.\.venv\Scripts\Activate.ps1
cd backend
python -m uvicorn app.main:app --reload
~~~

终端二，启动前端：

~~~powershell
cd frontend
npm run dev
~~~

终端三，启动 Worker：

~~~powershell
.\.venv\Scripts\Activate.ps1
cd backend
python -m app.workers.model_task_worker
~~~

源码启动默认使用 API `8000` 端口，Vite 将 `/api` 代理至 `http://localhost:8000`。若修改 API 监听端口，在 `frontend/.env.local` 设置 `VITE_API_PROXY_TARGET`。源码方式与完整容器方式选择其一，避免端口冲突。

Worker 读取与 API 相同的数据库和模型配置。单批处理可使用 `python -m app.workers.model_task_worker --once --batch-size 20`；持续进程可按 `Ctrl+C` 停止。

## 测试与质量检查

在已激活的 Python 环境中，从 `backend/` 执行：

~~~powershell
python -m pytest
python -m ruff check app evaluation tests scripts migrations
python -m pip_audit -r requirements.lock
~~~

默认 Pytest 跳过未显式启用的 MySQL 集成测试，不调用真实模型。使用已迁移的开发数据库启用业务集成验证：

~~~powershell
$env:RUN_MYSQL_INTEGRATION = "1"
python -m pytest tests/integration
Remove-Item Env:RUN_MYSQL_INTEGRATION
~~~

数据库迁移回退测试使用单独的开关和专用空测试库，配置要求见 [迁移测试](backend/tests/integration/test_v21_account_migration.py)。

在 `frontend/` 执行：

~~~powershell
npm run type-check
npm run build
npm audit --omit=dev --registry=https://registry.npmjs.org
~~~

`backend/requirements.lock` 与 `frontend/package-lock.json` 固定依赖解析结果。修改依赖后应同步更新锁文件并执行相应检查。注释与项目文档使用中文，代码标识和接口字段使用英文。

## 演示与评测

### 应用闭环演示

配置完成且使用默认 API / 前端端口时，在项目根目录执行：

~~~powershell
.\scripts\run_v3_demo.ps1
~~~

脚本构建并启动完整容器栈，验证真实 HTTP 交易意向闭环，再运行 100 个场景 × A/B/C 三组的确定性评测。它会新增合成买家与协商记录；结果写入 `backend/evaluation/results/<batch-id>/`。已有镜像时可添加 `-SkipBuild`。

### 离线对照评测

在 `backend/` 执行：

~~~powershell
python -m evaluation.cli
~~~

默认使用可控模拟模型，三组均通过隔离内存适配器运行，不访问真实模型或业务数据库。每次生成独立批次目录，保存运行明细、事件、报告和门禁结果；确定性门禁失败时返回非零退出码。

真实模型评测需要显式传入 `--model qwen --allow-real-model`，配置模型单价与币种，并设置费用等预算。模型冒烟脚本 `python scripts/smoke_model.py` 也会访问真实模型并可能产生费用。详细命令与实验协议见 [离线评测](docs/V3_阶段7_离线模拟买家与三组对比实验.md) 和 [正式效果评测报告](docs/V3_正式效果评测方案与结果.md)。

## 任务恢复与故障排查

卖家工作台的恢复任务页面可查看失败任务、授权额外一次重试或安全终止。任务只允许商品所属卖家处置，操作记录保存在数据库中。

| 问题 | 检查方式 |
|---|---|
| 注册或登录返回 `503` | 检查 `AUTH_SECRET` 的长度及占位值 |
| 无法发送协商消息 | 检查 `/api/ready`、模型地址与密钥、API 日志 |
| 审批已处理但买家未收到通知 | 检查 Worker 是否运行，以及通知任务是否需要人工恢复 |
| 数据库连接失败 | 检查 MySQL 健康状态、账号口令及宿主机 / 容器 URL |
| 消息请求返回 `409` | 根据错误提示判断处理中、业务冲突或需要人工恢复；同输入重试保留原请求标识 |
| API 与前端端口被占用 | 检查是否同时启动了源码与容器应用 |

观测事件保存在 `observability_events`，可通过 `backend/scripts/export_observability.py` 按会话、关联 ID 或模型任务导出。LangSmith 是默认关闭的脱敏元数据镜像；配置模型单价后记录估算成本，缺少价格配置时成本保持未知。

## 项目结构

~~~text
backend/
  app/
    api/             HTTP 接口与依赖
    agent/           结构化决策、受约束工具与回复策略
    services/        业务规则、事务与任务恢复
    workers/         模型任务和审批通知处理
    db/              ORM 模型与数据库连接
    observability/   脱敏事件与可选 Trace 镜像
  migrations/        Alembic 数据库迁移
  evaluation/        隔离评测、聚合、报告与门禁
  scripts/           种子、演示、冒烟与观测导出
  tests/             单元与集成测试
frontend/
  src/               页面、路由、账号状态与 API 客户端
docs/                设计、实验协议与验收文档
scripts/             项目级复现脚本
compose.yaml         本地容器配置
.env.example         配置模板
~~~

## 相关文档

- [人工验收流程](docs/V2.1_验收清单.md)
- [短事务与迟到结果防护](docs/V3_阶段3_短事务模型调用与迟到结果防护.md)
- [有界重试与人工恢复](docs/V3_阶段4_有界重试与人工恢复.md)
- [可观测性与成本采集](docs/V3_阶段6_结构化可观测性与成本采集.md)
- [评测报告与可复现交付](docs/V3_阶段8_评测报告回归门禁与可复现交付.md)
- [正式效果评测方案与结果](docs/V3_正式效果评测方案与结果.md)
