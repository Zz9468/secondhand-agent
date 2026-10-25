# SecondHand Agent

当前分支 `refactor/langgraph` 已完成兼容120场景的完整适配、开发回归扩充、回答遗漏及并发死锁修复。
**17.6 P5退出门禁尚未通过：真人风险/质量证据不足，LangSmith月度trace额度耗尽。**
冻结修复提交 `1d0fccd`；协议 **339/339**、单元及真实数据库业务 **437/437**通过。
当前开发集194个非冒烟场景，三次重复加200冒烟共782次；782次主批次已全部通过业务检查；可比组补全为111个，
现有79场景×3旧版对照与新增32场景×3配对补充分别保留。三类风险各需2995个独立、代表性真人标注负例，目前合格样本各0；
开发场景、重复运行、AI标注或本地轨迹完整均不能补足该门槛。
结果与适用范围见 [120场景复验报告](docs/P5_120场景最终复验报告.md)，
提交、测试、失败和费用见 [分阶段记录](docs/P5_120场景续验记录.md)，
机器摘要见 [P5证据](docs/evidence/p5-summary.json)，命令及真实轨迹重传见 [运行文档](docs/P5_运行与复验.md)。
旧320次实验为历史基线，保存在 [首轮实施记录](docs/P5_实施与验收记录.md)。
此前成果见
[P4 实施与验收记录](docs/P4_实施与验收记录.md)，启动、恢复和归档见 [P4 运行与复验](docs/P4_运行与复验.md)。
此前审批与生命周期交付见 [P3 实施与验收记录](docs/P3_实施与验收记录.md)。
P2 统一聊天、条件报价、草案、可信事实及展示因果见 [P2 实施记录](docs/P2_实施与验收记录.md)，
此前的真实模型校准见 [P2 真实模型验收记录](docs/P2_真实模型验收记录.md)。新聊天需要单独运行
`app.workers.graph_worker`；继承的旧 Worker 不领取新图任务。`GRAPH_CHAT_ENABLED=false`
可停止为新建会话绑定图；新图会话使用 `negotiation-v3`，已绑定会话保持版本，不回退给旧 Runtime。
P1 基线见 [P1 实施与验收记录](docs/P1_实施与验收记录.md)。
隔离环境沿用 [P0 运行与复验](docs/P0_运行与复验.md)，基础协议证据见
[P0 实施与验收记录](docs/P0_实施与验收记录.md)。新开发库当前迁移为 `f56ca103e8b2`。
下文保留继承的 V4 功能说明，作为后续迁移对照。

面向个人闲置交易的自主协商卖家助手。V4 通过有界 ReAct 工具循环、完整执行消息流、上下文压缩和协商记忆，让 Agent 连续处理商品咨询、价格讨论与交易条件变更；正式报价、接受、卖家审批和买家确认由后端确定性规则校验并执行。

同一账号可以作为买家或卖家：买家浏览商品并发起协商，卖家管理商品、私有协商策略、审批和恢复任务。

## 功能特性

- **自主工具循环**：Agent 根据当前问题查询商品和会话事实、检索原始历史、评估交易条件，并依据工具返回继续决策；模型步数、工具调用、总调用、时间、Token 和费用均有预算约束。
- **上下文与协商记忆**：保存完整执行流，每次模型请求前检查输入长度，按需压缩旧历史；维护带来源的买家陈述、待回答问题、已尝试方案及低置信度假设。
- **自然回复与发布审核**：普通咨询由模型基于公开事实回答，发布前另一次模型调用审核相关性和事实依据，再检查交易承诺与实际能力；审核不通过时反馈给循环修正。
- **受约束业务执行**：普通聊天与正式报价分离；按卖家净收入划分自动接受、人工审批和禁止接受区间。卖家私有价格阈值不进入模型上下文，正式回复从已验证的业务事实生成。
- **可恢复模型任务**：冻结输入、提示词、工具契约及模型配置；持久化租约、检查点和幂等结果，支持有界重试、迟到结果复核、Worker 接管及卖家人工处置。
- **动态买家评测**：复用版本化场景与买家目标，买家根据卖家的实际回复继续咨询、报价、确认或退出；保存交互轨迹、调用快照、Token、估算成本和门禁结果。
- **商品、账号与人工审批**：支持注册登录、商品发布与上下架、私有协商策略、卖家审批和买家最终确认，按账号及资源归属校验权限。

系统的业务终点是记录交易意向。`AGREED` 不代表付款或实际成交；项目不包含支付、库存锁定和订单履约。

## 架构

~~~mermaid
flowchart TB
    Frontend["Vue 3 前端"] --> Proxy["Vite / Nginx 代理"]
    Proxy --> API["FastAPI：认证、校验与接口"]
    API --> Services["业务 Service：商品、报价、审批、确认"]
    API --> Chat["ChatService：聊天编排与模型任务"]
    Chat --> Agent["SellerAgent：确定性授权路由"]
    Agent --> Runtime["V4 Runtime：有界 ReAct 与检查点"]
    Runtime --> Context["上下文构建、压缩与协商记忆"]
    Runtime -->|"受约束工具"| Services
    Runtime --> Provider["LangChain：工具调用与结构化输出"]
    Provider --> Qwen["千问 OpenAI 兼容接口"]
    Runtime --> Review["普通回复审核与承诺检查"]
    Review --> Provider
    Worker["独立 ModelTaskWorker"] --> Chat
    Worker --> Followup["ApprovalProcessor：审批通知"]
    Followup --> Provider
    Services --> DB[("MySQL：业务数据、执行消息流、上下文缓存与调用快照")]
    Chat --> DB
    Runtime --> DB
    Followup --> DB
~~~

后端采用模块化单体结构，独立 Worker 处理审批通知、到期重试和故障恢复。聊天接口优先同步执行自己的任务，前端通过消息轮询获取后续结果。

需要模型的请求按以下方式执行：

~~~text
短事务保存输入与冻结任务 → 领取租约 → 构建上下文并检查预算
                                          ↓
                                    事务外调用模型
                                          ↓
                          短事务复核租约、版本并保存调用快照
                                          ↓
                     工具执行及回执 → 检查上下文 → 下一次模型调用
                                          ↓
                      结束计划 → 回复审核/业务校验 → 保存公开回复
~~~

自动接受区和审批区的有效正式报价按后端规则直接路由。所有正式业务动作都经过权限复核；正式回复从已校验的报价与审批事实生成。

### Agent 执行与权限边界

1. 保存买家输入；只有提交结构化报价才创建正式 offer。聊天中的金额、讲价意愿或配送询问不能直接触发接受。
2. 后端先检查正式报价是否落在自动接受或人工审批区；可直接处理的授权动作不需要进入模型循环。
3. 其他请求进入 V4 Runtime，模型可调用 `get_product_info`、`get_negotiation_state`、`search_conversation_history` 和 `evaluate_offer` 获取证据。
4. 工具调用及结果写入执行流，再参与下一步上下文构建。正式还价、接受和审批工具在执行时重新校验权限、会话、报价及策略版本。
5. 普通回复使用 `conversation-v1`，通过 `finish_turn` 提交自然回复和可选记忆增量。自由主题标签只辅助记录，新增问题类型无需增加固定回复路由。
6. 另一次模型调用审核普通回复，事实引用需对应公开来源；不通过时将反馈交给循环修正。正式业务回复使用确定性文案。预算耗尽时返回受控兜底并保存停止原因。

回复审核是额外的模型校验，不能替代业务授权，也不能保证全部语义正确。当前 Agent 没有联系卖家、代办支付或实际履约的工具。

### 完整消息流、上下文与记忆

| 表 | 职责 |
|---|---|
| `agent_messages` | 会话完整执行流：买家输入、模型工具调用、工具结果、候选回复、公开回复及摘要/记忆产物，保留来源序号和执行分支 |
| `messages` | 前端可见的会话消息投影，通过 `source_agent_message_id` 关联执行流；不向买家展示内部工具或审核消息 |
| `agent_session_contexts` | 当前摘要、覆盖范围和协商记忆的版本化缓存，避免反复计算已压缩历史 |
| `agent_call_snapshots` | 每次模型调用的实际请求、工具/输出契约、响应、来源、提示词哈希与用量快照 |
| `model_execution_tasks` | 冻结运行配置、租约、检查点、重试与幂等状态，防止旧结果覆盖新业务状态 |

V4 从执行流构建上下文，不再固定只取最近 20 条公开消息。每轮依据任务的历史高水位和当前分支读取有效记录；每次模型调用前检查 Token 预算，组合系统提示词、后端公开事实、摘要、协商记忆及尚未被摘要覆盖的历史。系统提示词来自版本化资源并随任务冻结，不伪装成会话消息；实际输入保存在调用快照中。

压缩保留完整的工具调用与结果配对，保护当前输入及近期交互；无法继续压缩且仍超限时安全停止。摘要是执行流的派生产物，原历史不删除，仍可通过历史工具检索。记忆只保存有来源的协商状态，显式买家陈述必须有原文依据，旧证据不能覆盖更新的偏好；正式报价、审批状态和卖家私有策略仍以业务表为准。

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

**使用当前 V4 功能时，在 `.env` 中设置以下选项。** 模板的 `AGENT_RUNTIME=V3` 保留旧路径默认值，必须改为 `V4` 才启用新运行时：

~~~dotenv
AGENT_RUNTIME=V4
AGENT_REPLY_STRATEGY=conversation-v1
AGENT_MEMORY_ENABLED=true
MODEL_ENABLE_THINKING=false
AGENT_REPLY_REVIEW_ENABLE_THINKING=false
AGENT_OUTPUT_TOKEN_RESERVE=3072
~~~

主模型与回复审核当前均关闭思考，工具循环、记忆、压缩、审核及业务校验仍然执行。未指定审核思考开关时跟随主模型；启用思考后，主模型预算为 `MODEL_THINKING_BUDGET`，审核预算由冻结配置单独设置。思考及可见输出共同占用输出预算，应一并调整预留。

| V4 配置 | 模板值 | 用途 |
|---|---:|---|
| `AGENT_CONTEXT_WINDOW` | 32768 | 上下文总窗口，需匹配所选模型 |
| `AGENT_INPUT_TOKEN_BUDGET` | 16000 | 输入 Token 预算；同时预留输出及安全余量 |
| `AGENT_OUTPUT_TOKEN_RESERVE` | 1024 | 主模型输出预留；上面的 V4 示例使用 3072 |
| `AGENT_MAX_MODEL_STEPS` | 6 | 主循环模型步骤上限 |
| `AGENT_MAX_TOTAL_MODEL_CALLS` | 10 | 含审核、摘要和记忆的总模型调用上限 |
| `AGENT_MAX_MAINTENANCE_CALLS` | 2 | 摘要/记忆维护调用上限 |
| `AGENT_MAX_TOOL_CALLS` | 8 | 工具调用上限 |
| `AGENT_MAX_WALL_SECONDS` | 120 | 单任务执行时间预算 |
| `AGENT_MAX_TOTAL_TOKENS` | 180000 | 单任务累计 Token 上限 |

配置模型输入/输出单价与币种后，可设置 `AGENT_MAX_ESTIMATED_COST` 限制单任务估算费用；没有单价时成本保持未知。`AGENT_V4_SESSION_IDS` 可限制 V4 会话范围。任务创建时冻结这些选项，修改配置只影响之后的新任务。

### 2. 启动服务

配置模型后，启动完整应用：

~~~powershell
docker compose up -d --build api worker frontend
docker compose ps
~~~

Compose 会按依赖顺序启动 MySQL，执行 Alembic 迁移和演示数据初始化，再启动 API、Worker 与前端。MySQL 数据保存在命名卷 `mysql_data` 中。

已有数据库同样通过 `alembic upgrade head` 升级，V4 迁移版本为 `5991c96c16b8`。历史公开消息可在 `backend/` 使用 `python -m scripts.backfill_agent_messages` 幂等补齐到执行流，再用 `--verify-only` 检查投影一致性。回退旧运行路径时设置 `AGENT_RUNTIME=V3` 并重启应用，保留新增表和历史快照；已有任务继续使用其冻结契约。

如果构建在拉取基础镜像时连接 `auth.docker.io` 超时，可通过项目已有的 `DOCKER_REGISTRY` 配置切换镜像源：

~~~powershell
$env:DOCKER_REGISTRY = "docker.m.daocloud.io"
docker compose up -d --build api worker frontend
~~~

该环境变量只对当前 PowerShell 窗口及其子进程生效。需要长期使用时，在根目录 `.env` 中启用 `DOCKER_REGISTRY=docker.m.daocloud.io`。镜像地址替换方式见 [DaoCloud 使用说明](https://github.com/DaoCloud/public-image-mirror#使用方法)。

此配置只切换 Dockerfile 中的 Python、Node 和 Nginx 基础镜像；MySQL 仍使用 Compose 配置的 `mysql:8.4`。若该镜像首次拉取也超时，需要另行检查 Docker Desktop 的代理或镜像加速设置。

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

默认 Pytest 跳过未显式启用的 MySQL 集成测试，不调用真实模型。完整回归使用独立临时数据库，覆盖业务集成、V4 执行流、租约恢复、并发校验及迁移升级/回退：

~~~powershell
python -m scripts.run_isolated_mysql_tests
~~~

该脚本要求 Compose 的 `secondhand-agent-mysql-1` 已健康，且 `.env` 配置有效；自动创建测试库、迁移、运行 Pytest，最后清理测试库，避免与正在运行的 Worker 共用业务数据。使用其他 MySQL 环境时，需按 [迁移测试](backend/tests/integration/test_v21_account_migration.py) 和测试夹具要求单独配置测试库。

在 `frontend/` 执行：

~~~powershell
npm run type-check
npm run build
npm audit --omit=dev --registry=https://registry.npmjs.org
~~~

`backend/requirements.lock` 与 `frontend/package-lock.json` 固定依赖解析结果。修改依赖后应同步更新锁文件并执行相应检查。注释与项目文档使用中文，代码标识和接口字段使用英文。

2026-10-07 本地验证：隔离 MySQL 全量回归 **355 项通过**，Ruff 检查和前端类型检查/生产构建通过；G4 脚本动态评测 20 个场景 × 3 次重复（60 次交互）门禁通过。另从暂存代码独立构建 wheel，确认五份提示词资源可加载且内容与哈希未改变；这些验证未新增真实模型调用。

## 演示与评测

### V4 动态买家评测

动态评测从版本化场景加载商品事实、私有授权策略、买家预算和必要条件。开场后，模拟买家根据卖家的实际公开回复选择下一步，不逐条重放固定消息；买家私有目标不会进入卖家模型上下文。审批结果按照场景指定的卖家授权执行。

V4 数据集复用原有 100 个场景，并扩展 14 个开放问答、4 个长对话压力及 2 个条件变更场景。评测支持 G0–G4 消融配置，用于验证上下文、工具循环及记忆的作用；也可只运行完整方案 G4。历史 V3 结果保留为参考，不作为当前方案有效性的结论。

在 `backend/` 运行不产生模型费用的管线检查：

~~~powershell
python -m evaluation.dynamic_runner --groups G4 --count 20 --repeats 3 --model scripted --buyer-model scripted
~~~

默认创建新的 `evaluation/results/v4-<batch-id>/`，保存场景/配置/代码哈希、运行明细、执行流、实际调用快照、费用预留、汇总和门禁。脚本模型只验证流程及报告，不用于证明真实协商质量。评测使用隔离内存业务适配器，不写入正在运行的业务数据库。

真实模型批次需要显式选择 `--model qwen --buyer-model qwen --allow-real-model`，配置每百万 Token 单价和币种，并在执行前设定 `--max-cost`、`--max-calls`、`--max-tokens` 和 `--timeout`。`--max-cost` 按配置币种解释；输出目录必须是新目录，不能覆盖已记录批次。每次 RPC 前预留预算，未知用量保留预留值，失败调用同样记账。

可使用 `--scenario-file` 和 `--profiles-file` 指定冻结输入，使用 `--protocol-file` 记录预先确定的实验口径。评测与复核入口见 [动态评测执行器](backend/evaluation/dynamic_runner.py)、[独立安全检查](backend/evaluation/dynamic_safety.py)、[动态报告](backend/evaluation/dynamic_report.py)、[运行稳定性复算](backend/scripts/recompute_v4_runtime_stability.py) 和 [停止案例职责复核](backend/scripts/review_v4_agent_duties.py)。

### 最近一次真实模型结果

2026-10-06，使用 `qwen3.7-flash`、`conversation-v1`、开启协商记忆且关闭主模型/审核思考，完成 **120 个版本化合成场景 × 2 次重复，共 240 次动态交互**。统计使用输入修正后的完整集合：原批中两个场景将未知运费误编码为正式报价，修正开场后各完整补跑两次；其余 118 个场景未修改，原批及失败补测记录保留。指标对应当时冻结的代码与提示词，不能自动视为此后修复版本的真实模型复测结果。

| 指标 | 结果 | 解释 |
|---|---:|---|
| 交易意向达成 | 135/240（56.25%） | 记录买家确认意向，不代表付款成交 |
| 正常业务回复返回 | 1096/1122（97.68%） | 包含正常拒绝，不评价全部回复语义 |
| 受控预算停止 | 26/1122（2.32%） | 影响 26/240 次会话，仍可能中断业务 |
| 异常执行终止 | 0/1122 | 本批卖家已执行轮次的观察值 |
| 买家模拟器失败 | 2/240 | 与卖家执行结果分开记录 |
| 已捕获记忆更新异常 | 12/240 次会话 | 辅助模块异常未掩盖，不能声称记忆全部正确 |
| 执行报价、审批及确认的独立事实检查违规 | 0/240 | 只描述业务事实校验范围 |

1,122 个卖家轮次全部正常返回或按预设预算安全退出，**不等于全部决策正确或线上可用性 100%**。原自动门禁将 26 次受控停止计入卖家失败后触发阈值，结果仍为 **FAIL**；事后复算没有修改原始门禁。

26 条停止案例的职责复核区分出 14 次攻击防护、6 次功能/资料边界、1 次审核链路问题及 5 次有证据的职责/决策问题；其中两条存在承诺联系或反馈卖家的公开文本，超出当前工具能力。因此不能宣称公开回复零错误，回复质量仍需独立人工评审。

该统计集合包含 4,257 次实际模型请求、11,777,052 个已知 Token，未知用量调用为 0。该轮累计已知费用估算为 2.956701 元、保守预留为 17.288519 元，均属于费用台账估计。结果来自隔离内存环境和合成买家，不覆盖真实用户分布、HTTP/MySQL 压测或线上恢复稳定性；没有记忆消融结果支持因果提升结论。原始评测产物保存在本地 `backend/evaluation/results/`，已被 Git 忽略。

### 历史应用闭环演示

配置完成且使用默认 API / 前端端口时，在项目根目录执行：

~~~powershell
.\scripts\run_v3_demo.ps1
~~~

脚本构建并启动完整容器栈，验证真实 HTTP 交易意向闭环，再运行 100 个场景 × A/B/C 三组的确定性评测。它会新增合成买家与协商记录；结果写入 `backend/evaluation/results/<batch-id>/`。已有镜像时可添加 `-SkipBuild`。

### 历史离线对照评测

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
| 构建时连接 `auth.docker.io` 超时 | 使用启动步骤中的备用镜像源，或检查 Docker Desktop 的代理与网络配置 |
| 消息请求返回 `409` | 根据错误提示判断处理中、业务冲突或需要人工恢复；同输入重试保留原请求标识 |
| API 与前端端口被占用 | 检查是否同时启动了源码与容器应用 |
| 新环境仍走旧协商路径 | 将 `.env` 中的 `AGENT_RUNTIME` 改为 `V4`，重启 API 和 Worker 后创建新任务 |
| 回复较慢 | 一轮可能包含工具探索、结束计划、回复审核和记忆维护等多次 RPC；按调用快照的用途与耗时定位，并检查思考开关及模型延迟 |
| 返回预算停止兜底 | 检查执行流中的停止原因及调用快照，区分模型步数、工具、上下文、Token、时间和费用预算 |

观测事件保存在 `observability_events`，可通过 `backend/scripts/export_observability.py` 按会话、关联 ID 或模型任务导出。LangSmith 是默认关闭的脱敏元数据镜像；配置模型单价后记录估算成本，缺少价格配置时成本保持未知。

## 项目结构

~~~text
backend/
  app/
    api/             HTTP 接口与依赖
    agent/           V4 Runtime、上下文压缩、记忆、回复审核与受约束工具
      prompt_resources/  版本化 .txt 提示词，随包分发并记录哈希
    services/        执行流持久化、模型任务、业务规则与短事务
    workers/         模型任务和审批通知处理
    db/              ORM 模型与数据库连接
    observability/   脱敏事件与可选 Trace 镜像
  migrations/        Alembic 数据库迁移
  evaluation/        动态买家、隔离业务适配器、安全检查、报告与门禁
  scripts/           消息补齐、隔离测试、迁移/回滚验证与评测复核
  tests/             单元与集成测试
frontend/
  src/               页面、路由、账号状态与 API 客户端
docs/                设计、实验协议与验收文档
scripts/             项目级复现脚本
compose.yaml         本地容器配置
.env.example         配置模板
~~~

## 实现入口与历史文档

- [V4 工具循环](backend/app/agent/runtime.py)
- [上下文构建与压缩](backend/app/agent/context_builder.py)
- [协商记忆与来源校验](backend/app/agent/negotiation_memory.py)
- [自然回复审核](backend/app/agent/conversation_reply.py)
- [执行流与短事务适配](backend/app/services/agent_runtime_service.py)
- [V4 数据迁移](backend/migrations/versions/5991c96c16b8_v4_agent_execution_stream_contexts_and_.py)
- [人工验收流程](docs/V2.1_验收清单.md)
- [短事务与迟到结果防护](docs/V3_阶段3_短事务模型调用与迟到结果防护.md)
- [有界重试与人工恢复](docs/V3_阶段4_有界重试与人工恢复.md)
- [可观测性与成本采集](docs/V3_阶段6_结构化可观测性与成本采集.md)
- [评测报告与可复现交付](docs/V3_阶段8_评测报告回归门禁与可复现交付.md)
- [正式效果评测方案与结果](docs/V3_正式效果评测方案与结果.md)
