# SecondHand Agent

面向个人闲置交易的自主协商卖家助手。当前已完成 V1、V2 和 V2.1：注册用户使用同一个账号买卖闲置商品，在商品大厅浏览公开信息，并在明确发起协商后进入受规则、审批和最终确认约束的议价闭环。

## 当前阶段

V1 阶段一已完成最小工程骨架：

- Python 3.11 + FastAPI 后端；
- LangChain 1.x 模型能力边界；
- MySQL 8.4（InnoDB）本地 Compose 服务；
- Vue 3 + Vite 最简状态页面；
- 进程健康和数据库就绪检查；
- 后端基础测试与前端构建检查。

V1 阶段二已增加独立的价格规则核心：

- 所有业务金额仅接受 Python `Decimal`，并按 MySQL `DECIMAL(12,2)` 范围校验；
- 按卖家承担的运费和其他优惠计算卖家净收入；
- 将报价划分为自动接受区、审批区和禁止接受区；
- 成本未知时拒绝进行授权判断，避免 Agent 对不可计算条件作出承诺；
- 价格规则不依赖 HTTP、数据库和大模型，可通过单元测试独立验证。

V1 阶段三已建立持久化基础：

- 商品、卖家规则、协商会话、消息和报价 SQLAlchemy ORM 模型；
- 使用 Alembic 管理 MySQL 表结构；
- 使用 `DECIMAL(12,2)` 保存业务金额，报价条件以快照形式持久化；
- 可重复执行的演示数据脚本，固定创建演示商品 `1001` 和会话 `1001`；
- MySQL 集成测试覆盖基础增删查改、金额精度和种子数据幂等性。

V1 阶段四已接通业务服务与 Agent 工具：

- Product Service 只返回当前会话可公开的商品事实，不暴露卖家私有价格规则；
- Negotiation Service 负责会话访问校验、报价历史、事务锁和状态变更；
- `evaluate_offer` 只返回可执行权限，不向模型返回最低价或自动接受阈值；
- `submit_counter_offer` 只允许写入自动授权区的 Agent 正式还价；
- `accept_offer` 会重新读取数据库最新状态和规则，只接受当前有效的买家报价；
- 五个 LangChain 工具使用后端绑定的会话上下文，模型不能指定买家、卖家或会话身份。

V1 阶段五已完成 Agent 与正式回复安全层：

- 使用 Pydantic 定义结构化协商决策，并通过 LangChain `create_agent` 与模型原生 JSON Schema 约束输出；
- 提供千问 OpenAI 兼容接口适配层，模型地址、名称、思考模式、超时和重试次数均通过环境变量配置；
- Seller Agent 先读取可信商品与会话状态，再执行结构化决策，模型不能直接调用写库工具；
- 完整正式报价由后端权限结果确定处理：自动接受区直接接受、审批区直接申请审批，禁止区只允许模型还价或拒绝；
- 普通咨询先由模型解析为可组合的语义指令，再由后端生成可信回复计划；一句话中的商品、价格、配送和履约请求可以分别裁决，只有确实缺少必要条件时才进行针对性澄清；
- 商品详情和一般交流可采用通过安全校验的模型候选文案，价格试探、库存、配送、履约承诺及所有交易动作使用后端可信表达；
- 正式还价与接受回复忽略模型候选文案，只从已校验并持久化的报价快照生成；
- 非法价格、未知附加条件、虚假审批、格式错误和越权承诺均降级为非正式安全回复；
- 集成测试使用可控的模拟决策提供者，不消耗真实模型额度，也可稳定复现安全边界。

V1 阶段六已完成最小聊天闭环与验收场景：

- 提供查询协商状态、发送消息和读取消息记录的 FastAPI 接口；
- V1 买家身份最初由请求头绑定并校验；V2 阶段一已将这一临时方式替换为服务端签名访客身份；
- 聊天文字与正式报价字段分离，金额和运费由后端以 `Decimal` 校验；
- 同一会话通过行锁串行处理；买家消息、结构化报价、Agent 工具动作和最终回复在同一外层事务中提交或回滚；
- 幂等指纹覆盖聊天文字和完整结构化报价，重放时返回原始结果和正式报价编号；
- 所有正式还价入口都会校验最大议价轮次，最后一轮只能回应本轮买家正式报价；
- Seller Agent 每轮读取数据库中的商品、报价历史和最近聊天上下文；
- Vue 页面可展示商品、协商进度、当前报价和历史消息，并提交普通咨询或正式报价；
- 自动化测试覆盖商品咨询、多轮低价、包邮净收入、审批区提示、Prompt Injection、身份隔离、完整幂等、事务回滚和最大轮次；
- 种子脚本支持显式重置演示会话，便于从干净状态重复演示。
- MySQL 开发端口只绑定 `127.0.0.1`，不会监听局域网网卡。

V2 阶段一已完成身份认证与权限基础：

- 新增卖家账号表，密码使用带随机盐的 `scrypt` 哈希保存，商品所有权通过外键绑定卖家账号；
- 提供卖家登录、当前身份和退出接口，登录态使用服务端签名的 HttpOnly Cookie；
- 买家首次访问时由服务端签发独立访客 Cookie，API 不再信任客户端填写的 `X-Buyer-ID`；
- 前端会为当前访客创建或复用演示商品的协商会话，不再固定绑定 `demo-buyer` 和会话 `1001`；
- 认证签名密钥必须显式配置且至少 32 个字符，未配置时认证接口关闭并返回 `503`；
- 权限测试覆盖伪造身份头、跨买家会话访问、卖家登录失败、退出失效和不同浏览器访客隔离。

V2 阶段二已完成商品、策略与动态会话入口：

- 买家可匿名读取已上架商品的公开信息，响应不包含卖家底价和自动接受阈值；
- 卖家登录后可以创建、编辑、上架或下架自己的商品，并维护私有协商策略；
- 商品写操作按登录卖家校验所有权，其他卖家访问时按资源不存在处理；
- 策略更新使用期望版本校验并在同一事务中递增版本，避免旧页面静默覆盖新规则；
- 买家页面从公开商品接口选择商品并创建当前访客会话，不再硬编码商品 `1001`；
- 页面新增最小卖家管理入口，种子商品仍可用于演示和回归测试。

V2 阶段三已完成审批数据模型与事务状态机：

- 新增 `approval_requests` 表，审批绑定具体会话、不可变报价及申请时的策略版本；
- 审批状态支持待处理、通过、拒绝、取消和过期，并预留审批后回复处理字段；
- 创建审批时锁定会话并重新校验当前报价、商品状态、报价时效、策略版本和价格区间；
- 只有审批区内且交易条件可校验的当前买家报价可以进入 `PENDING`；
- 使用会话行锁及 MySQL 生成列唯一索引，保证同一会话最多存在一个待审批请求；
- Approval Service 支持创建、查询、取消和过期处理，取消或过期后恢复会话为可协商状态；
- 本阶段只建立审批领域能力；Agent 接入已由阶段四完成，卖家同意或拒绝接口已由阶段五完成。

V2 阶段四已完成 Agent 创建审批请求：

- 新增受后端会话、买家身份和本轮正式报价约束的 `request_approval` Tool；
- Seller Agent 只有在本轮当前买家报价位于审批区且条件完整时，才能创建唯一 `PENDING` 审批；
- 审批成功落库后才回复“已提交卖家确认”，并将会话切换为 `WAITING_APPROVAL`，不会声称已经接受或成交；
- 等待审批期间仍可安全查询公开商品信息；提交新正式报价时会在同一事务中撤销旧审批，再处理新报价；
- 模型伪造报价编号、强行审批禁止区报价或重复申请均不能新增有效审批，模型决策失败会回滚整轮聊天事务；
- 阶段四只负责创建审批；卖家处理已由阶段五补全，审批结果通知仍属于阶段六。

V2 阶段五已完成卖家审批 API 与页面：

- 登录卖家可以查看自己商品产生的审批列表与详情，其他卖家的审批按不存在处理；
- 同意和拒绝操作在事务中重新校验审批状态、有效期、商品状态、当前报价、报价条件及策略版本；
- 审批操作使用独立幂等键，重复提交同一请求只返回既有结果，不会重复变更状态或生成后续任务；
- 同意或拒绝结果与 `followup_status=PENDING`、`followup_request_id` 同事务保存，为阶段六 Worker 留出可靠任务；
- 拒绝或失效会恢复会话为可协商状态；审批同意只代表卖家授权当前报价，不会直接写入 `AGREED`；
- 卖家管理页面新增报价审批列表、报价详情、卖家意见以及同意和拒绝操作。

V2 阶段六已完成审批后续处理与买家轮询：

- 新增独立审批 Worker，通过 MySQL 行锁领取 `PENDING` 或 `FAILED` 后续任务，多个 Worker 不会重复处理同一审批；
- Worker 每次重新读取审批、会话、商品、报价和最新规则，状态变化时发送失效通知而不是继续承诺旧报价；
- 审批通知会触发新的结构化模型调用，但最终文案只由数据库报价快照和正式回复安全层生成，不发送模型自由文本；
- 审批通过通知、报价状态、会话恢复、Agent 消息和 `followup_status=SENT` 在同一事务内提交；
- 模型调用失败时任务标记为 `FAILED`，后续轮询可以安全重试；消息使用 `followup_request_id` 防止重复发送；
- 买家页面每两秒增量读取新消息和会话状态，无需刷新整页即可看到卖家审批结果。

V2 阶段七已完成买家最终确认与会话收口：

- 买家确认请求必须绑定当前有效 `offer_id` 和幂等标识，后端重新校验买家身份、会话状态、报价归属、时效、当前性和授权来源；
- 可确认的授权来源只有三类：Agent 合法生成的当前还价、Agent 已自动接受的当前买家报价，以及卖家审批通过且后续通知已发送的当前买家报价；
- 确认成功会在同一事务中保存确认报价、时间、唯一请求标识和授权来源，写入系统消息并把会话变为 `AGREED`；
- 重复确认只返回已有结果，并发确认只会写入一份交易意向；确认后的会话不能继续发送消息或议价；
- 买家可主动关闭仍在进行的会话，关闭时会同步取消待审批请求，不遗留有效审批；
- 前端只在当前报价满足基本可确认条件时展示“确认交易意向”，最终合法性始终由后端判定；`AGREED` 仅表示记录交易意向，不代表付款、锁定库存或实际成交。

V2 阶段八已完成双端整合与全量验收能力：

- 卖家新增协商会话列表与详情接口，只能读取自己商品下的会话，响应不包含买家访客标识；
- 卖家页面整合商品、审批和协商会话三类入口，可查看消息、报价、最新审批、通知状态和最终确认结果；
- 买家与卖家页面分别按生命周期轮询，切换页面或卸载组件时不会遗留额外轮询任务；
- 卖家可在审批和对应协商详情之间跳转，审批文案明确区分“卖家授权”和“买家确认交易意向”；
- 新增完整 V2 端到端集成测试，以及身份、权限、状态机、并发、幂等、迁移和 Worker 故障回归；
- 人工验收步骤见 [`docs/V2_验收清单.md`](docs/V2_验收清单.md)。

V2.1 七个阶段已完成统一账号与商品大厅升级：

- `user_accounts` 同时承载商品卖家和会话买家，历史访客按原访客 ID 一对一迁移为不可登录账号；
- 注册和登录统一使用 `secondhand_user_session` HttpOnly Cookie，买家/卖家模式只是前端路由上下文；
- 商品大厅、个人卖家公开主页和商品详情均可直接浏览，刷新或切换浏览页面不会创建协商会话；
- 只有登录用户在商品详情点击“与卖家协商”后，才会创建或恢复自己对该商品的进行中会话；
- 用户不能协商自己发布的商品，商品、会话、审批和确认操作均按当前账号及资源归属校验；
- 买家协商列表和卖家工作台已拆分为 Vue Router 页面，同一登录态可随时切换；
- V2 原有的价格规则、Agent 安全边界、审批 Worker、幂等处理和交易意向确认语义保持不变；
- V2.1 人工验收步骤见 [`docs/V2.1_验收清单.md`](docs/V2.1_验收清单.md)。

V3 八个阶段已全部完成，覆盖回归基线、持久化模型任务、短事务执行、有界恢复、安全事务加固、结构化可观测性、离线三组对比和可复现交付：

- 将验证分为无外部依赖快速回归、MySQL 业务集成、专用数据库迁移和真实模型冒烟四层，真实模型不会被普通 Pytest 或构建命令隐式调用；
- 盘点正式还价权限、Prompt Injection、虚假审批、履约越权、重复请求、跨账号访问、事务回滚和 Worker 恢复的现有测试证据及后续缺口；
- 固定交易意向达成率、有效结束率、运行级违规率、正式承诺违规率、平均协商轮次、审批率和模型成本的公式与分母；
- 规定评测必须保存代码提交、模型、Prompt/场景版本、随机种子、预算、终态、错误和 Token 用量等可追溯元数据；
- 新增 `model_execution_tasks`，统一保存聊天决策和审批通知的业务幂等键、不可变输入、会话/报价/策略版本、任务状态、尝试次数、重试时间、租约、错误分类及模型用量；
- `ModelTaskService` 已实现幂等创建、并发安全领取、过期租约接管、成功完成、延后重试、终止失败和迟到结果失效，不调用真实模型，也不把模型结果直接写成正式业务事实；
- 聊天和审批通知均已改为“短事务保存输入并领取任务 → 事务外调用模型 → 新短事务重新加锁复核并写入”；模型等待期间不持有业务行锁，旧版本结果进入 `STALE`，不能执行旧还价、接受或审批事实；
- 同步聊天 API 使用 `409` 表达处理中、`503` 表达可重试模型失败；前端保留原请求幂等键并支持安全重试，轮询结果按消息 ID 去重；
- 聊天与审批回访共用错误分类、确定性抖动的指数退避、`next_retry_at`、租约接管和最大尝试次数；
- 超过上限或遇到永久错误时进入 `FAILED` / `MANUAL_REQUIRED`，不再自动热重试；
- 统一恢复 Worker 可在进程退出或服务重启后接管过期租约，并对“回复已写入、任务未收口”的部分成功直接对账；
- 卖家工作台可查看自己的模型任务，对失败任务授权一次额外重试或安全终止，操作人、原因和时间会持久化。
- 多语言、全角字符和拆词形式的底价、审批、成交、包邮、发货及留货候选承诺会被安全回复模板替换；
- 阶段五新增报价替换、策略更新、伪造报价 ID、未知履约条件和完成事务故障注入回归，迟到结果不能形成正式事实；
- 模型任务查询、重试和终止已覆盖未登录与跨卖家访问，审批备注不能向正式通知注入新条件；
- 真实模型冒烟固定为 5 个场景级调用并显示超时、重试和理论最大调用预算，包含英文与繁体中文对抗输入；
- HTTP 响应统一返回 `X-Request-ID` 与 `X-Correlation-ID`；聊天、模型任务、受约束写工具、审批通知、人工恢复和最终确认使用脱敏事件贯通；
- `observability_events` 是不依赖外部平台的本地事实源，可按会话、关联 ID 或模型任务导出 JSONL/聚合摘要；模型任务和事件同时保存 Token、缓存 Token、运行时单价快照、币种及估算成本；
- LangSmith 是显式开启的可选元数据镜像，只发送脱敏字段，不启用会上传 Prompt/回复的自动追踪；未配置或发送失败不影响核心交易路径；
- 独立 `backend/evaluation/` 默认使用场景集 2.0.0 的 100 个版本化合成场景，对比 A 纯 Prompt、B 模型加规则、C 完整工作流；三组均使用隔离内存适配器，不读写业务数据库；
- C 组与正式 Seller Agent 共用报价授权和确定性路由，使用生产 Prompt、非敏感授权上下文、多轮历史及相同结构化纠错重试；自动接受区和审批区报价均绕过模型，未知费用在模型调用前安全失败；
- 每次评测保存清单、逐轮事件、运行明细和指标汇总；单样本故障不会中断批次，真实模型必须显式开启并受样本、调用、Token、费用和超时预算约束；
- 报告生成器只从落盘 JSONL 重算 JSON/CSV/Markdown 派生产物，记录原始文件 SHA-256，并提供比率的 Wilson 95% 区间以及完成样本的均值、中位数与 P95；
- 多批次聚合器要求干净提交、相同场景/Prompt/模型/价格快照和不同随机种子，只在相同高风险场景上生成 A/C 匹配对比；
- 确定性评测具备失败即非零退出的安全门禁，真实模型结果明确标为 `NOT_APPLICABLE` 并进入预定义人工复核队列；
- 后端、前端基础镜像与本地 Compose 已覆盖 MySQL、迁移、种子、API、Worker 和前端，一键脚本可完成真实 HTTP 交易意向闭环及容器内评测；
- 阶段一至八的设计与验收记录分别见 [`docs/V3_阶段1_回归基线与指标契约.md`](docs/V3_阶段1_回归基线与指标契约.md)、[`docs/V3_阶段2_持久化模型任务.md`](docs/V3_阶段2_持久化模型任务.md)、[`docs/V3_阶段3_短事务模型调用与迟到结果防护.md`](docs/V3_阶段3_短事务模型调用与迟到结果防护.md)、[`docs/V3_阶段4_有界重试与人工恢复.md`](docs/V3_阶段4_有界重试与人工恢复.md)、[`docs/V3_阶段5_安全与事务回归加固.md`](docs/V3_阶段5_安全与事务回归加固.md)、[`docs/V3_阶段6_结构化可观测性与成本采集.md`](docs/V3_阶段6_结构化可观测性与成本采集.md)、[`docs/V3_阶段7_离线模拟买家与三组对比实验.md`](docs/V3_阶段7_离线模拟买家与三组对比实验.md) 和 [`docs/V3_阶段8_评测报告回归门禁与可复现交付.md`](docs/V3_阶段8_评测报告回归门禁与可复现交付.md)。正式真实模型协议与最终结果见 [`docs/V3_正式效果评测方案与结果.md`](docs/V3_正式效果评测方案与结果.md)。

V2.1 的完整设计、迁移原则和七阶段实施记录见 [`docs/V2.1_统一账号与商品大厅升级计划.md`](docs/V2.1_统一账号与商品大厅升级计划.md)。下一步进入 V4，聚焦云服务器部署与运维；阶段八容器化只用于本地复现，不代表生产部署已经完成。

真实千问调用需要在本地 `.env` 中填写 `MODEL_BASE_URL` 和 `MODEL_API_KEY`，并选择同时支持 Tool Calling 与结构化输出的模型。不同地域的兼容接口地址可能不同，因此模板不预设地址。协商决策默认设置 `MODEL_ENABLE_THINKING=false` 以降低响应延迟和超时概率；确有需要时可以显式开启。`.env` 已被 Git 忽略，禁止将真实密钥写入 `.env.example` 或提交到仓库。

## 开发约定

- 代码注释、docstring 和项目文档使用中文；
- 变量名、函数名、接口字段、枚举值和第三方配置键保持英文；
- 注释重点说明业务约束和设计原因，避免简单复述代码。
- 每个实施阶段结束前必须完成自检：复核业务与权限边界，运行相关测试、Ruff、Alembic 结构检查、前端类型与构建检查，并确认差异中没有密钥或意外文件。

## 环境要求

- Python 3.11
- Node.js `^22.18.0 || >=24.12.0`
- Docker Engine 与 Docker Compose

## 本地启动

以下示例使用 Windows PowerShell，并默认在项目根目录执行。后端必须运行在 Python 3.11 虚拟环境中；推荐使用 Conda，也可以使用 Python 自带的 `venv`。虚拟环境只需创建一次，但每次打开新的终端都要重新激活。macOS/Linux 用户可将 `Copy-Item` 换成 `cp`，并使用 `source .venv/bin/activate` 激活 `venv`。

### 首次安装

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

打开根目录下的 `.env`，替换所有 `CHANGE_ME` 占位值：两个数据库口令应不同，并把 `MYSQL_PASSWORD` 的值同步写入 `DATABASE_URL`；`AUTH_SECRET` 至少使用 32 个随机字符；`DEMO_SELLER_PASSWORD` 设置为本地演示卖家的强密码。真实模型配置可以暂时留空；未配置模型时仍可查看商品和历史数据，但不能发送协商消息。

可以在已激活的 Python 环境中生成认证随机密钥，再将输出复制到 `.env` 的 `AUTH_SECRET`：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

启动 MySQL：

```powershell
docker compose up -d mysql
docker compose ps
```

创建并激活 Python 3.11 虚拟环境。以下两种方式任选一种。

使用 Conda：

```powershell
conda create -n secondhand-agent python=3.11 pip
conda activate secondhand-agent
python --version
```

不使用 Conda 时，可以使用标准 `venv`（需要本机已经安装 Python 3.11）：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
```

`python --version` 应显示 Python 3.11。然后安装后端依赖、执行数据库迁移并初始化演示数据：

```powershell
cd backend
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
python -m alembic upgrade head
python scripts/seed_data.py
cd ..
```

安装前端依赖：

```powershell
cd frontend
npm install
cd ..
```

种子脚本只幂等补齐演示统一账号 `demo-seller`、公开商品 `1001` 及其私有策略，不创建、删除或重置任何协商会话。协商会话必须由已登录买家在商品详情明确发起。

### Docker Compose 一键复现

准备好 `.env` 后，也可以不在宿主机安装 Python/Node 依赖，直接启动完整本地演示栈。`CONTAINER_DATABASE_URL` 与 `DATABASE_URL` 使用相同账号、URL 编码后的密码和数据库名，但主机名必须是 `mysql`。阶段八以前创建的 `.env` 可由一键脚本在当前进程安全派生该地址；脚本不会回写或输出数据库口令。

确保 Docker Desktop 已启动，在项目根目录执行：

```powershell
.\scripts\run_v3_demo.ps1
```

脚本会构建并启动 MySQL、迁移、种子、API、Worker 和前端，验证前端反向代理，通过真实 HTTP 接口生成一条本地合成交易意向，随后把宿主机 Git 提交号及工作区状态注入 API 容器，运行场景集 2.0.0 的 100 场景 × A/B/C 共 300 个确定性样本及安全门禁。报告写入 `backend/evaluation/results/<batch-id>/report.md`；该目录被 Git 忽略。演示会新增合成买家及其协商记录，但不会删除或重置已有数据。

若镜像已经构建，可执行 `.\scripts\run_v3_demo.ps1 -SkipBuild`。Docker Hub 当前网络不可达时，可在当前 PowerShell 会话临时设置 `$env:DOCKER_REGISTRY = "docker.m.daocloud.io"` 后重试。完成后可保留 MySQL 并停止应用进程：

```powershell
docker compose stop frontend worker api
```

基础 Compose 只绑定本机回环地址，用于 V3 本地复现。TLS、生产 Nginx、备份恢复、监控告警、镜像发布和回滚属于 V4。

### 从旧版本升级

已有 `.env` 的开发者无需覆盖原文件，但必须补充 `AUTH_SECRET`、`USER_SESSION_MINUTES` 和 `DEMO_SELLER_PASSWORD`。随后升级数据库并重新执行种子脚本。迁移会把旧卖家账号升级为统一账号，并为每个历史访客建立独立、不可登录的账号映射；商品、会话、消息、报价、审批和确认记录会保留：

```powershell
conda activate secondhand-agent
cd backend
python -m alembic upgrade head
python scripts/seed_data.py
cd ..
```

### 日常启动

先确保 Docker Desktop 已启动，然后在项目根目录启动 MySQL：

```powershell
docker compose up -d mysql
```

打开第一个终端，激活首次安装时创建的虚拟环境并启动后端。

Conda 用户：

```powershell
conda activate secondhand-agent
cd backend
python -m uvicorn app.main:app --reload
```

`venv` 用户：

```powershell
.\.venv\Scripts\Activate.ps1
cd backend
python -m uvicorn app.main:app --reload
```

保持后端终端运行，再打开第二个终端，在项目根目录启动前端：

```powershell
cd frontend
npm run dev
```

聊天故障恢复和审批回访需要打开第三个终端，激活同一个 Python 环境并启动统一 Worker：

```powershell
conda activate secondhand-agent
cd backend
python -m app.workers.model_task_worker
```

Worker 与后端读取同一份 `.env`，需要有效的数据库和模型配置。它会持续领取聊天决策与审批回访任务，并接管过期租约；按 `Ctrl+C` 可以停止。若只想手动处理当前一批任务并退出，可执行：

```powershell
cd backend
python -m app.workers.model_task_worker --once
```

浏览器访问 `http://localhost:5173`。后端接口：

- `GET http://localhost:8000/api/health`：仅检查 API 进程；
- `GET http://localhost:8000/api/ready`：检查 MySQL、认证和模型配置状态；
- `POST http://localhost:8000/api/auth/register`：注册统一账号并建立登录态，注册时不选择身份；
- `POST http://localhost:8000/api/auth/login`：统一账号登录；演示账号用户名为 `demo-seller`，密码取自本地 `DEMO_SELLER_PASSWORD`；
- `GET http://localhost:8000/api/auth/me`：读取当前统一账号；
- `POST http://localhost:8000/api/auth/logout`：退出统一账号；
- `GET http://localhost:8000/api/products`：列出已上架商品的公开信息；
- `GET http://localhost:8000/api/products/{product_id}`：读取已上架商品的公开详情；
- `GET http://localhost:8000/api/sellers`：列出拥有公开商品的个人卖家；
- `GET http://localhost:8000/api/sellers/{seller_id}`：读取卖家公开主页；
- `GET http://localhost:8000/api/sellers/{seller_id}/products`：读取该卖家的公开商品；
- `POST http://localhost:8000/api/products`：当前账号创建商品及初始私有策略；
- `GET http://localhost:8000/api/seller/products`：当前账号读取自己发布的商品和策略；
- `PUT http://localhost:8000/api/seller/products/{product_id}`：编辑自己的商品或上下架；
- `DELETE http://localhost:8000/api/seller/products/{product_id}`：删除本人未上架且从未产生协商记录的商品；
- `PUT http://localhost:8000/api/seller/products/{product_id}/policy`：按版本更新自己的私有策略；
- `GET http://localhost:8000/api/seller/approvals`：登录卖家读取自己的审批列表，可用 `status` 过滤；
- `GET http://localhost:8000/api/seller/approvals/{approval_id}`：读取审批、商品及报价详情；
- `POST http://localhost:8000/api/seller/approvals/{approval_id}/approve`：幂等同意当前有效报价；
- `POST http://localhost:8000/api/seller/approvals/{approval_id}/reject`：幂等拒绝当前有效报价；
- `GET http://localhost:8000/api/seller/negotiations`：登录卖家读取自己商品下的协商列表，可用 `status` 过滤；
- `GET http://localhost:8000/api/seller/negotiations/{session_id}`：读取卖家有权访问的消息、报价与审批时间线；
- `GET http://localhost:8000/api/seller/model-tasks`：读取当前卖家商品产生的模型任务及脱敏故障状态；
- `POST http://localhost:8000/api/seller/model-tasks/{task_id}/retry`：对终止失败任务授权一次额外尝试；
- `POST http://localhost:8000/api/seller/model-tasks/{task_id}/terminate`：安全终止可处置任务并记录人工原因；
- `GET http://localhost:8000/api/buyer/negotiations`：读取当前账号作为买家的协商列表；
- `POST http://localhost:8000/api/negotiations`：在用户明确发起时创建或复用当前账号对商品的进行中会话；
- `GET http://localhost:8000/api/negotiations/{session_id}`：读取当前账号拥有的买家协商状态；
- `GET http://localhost:8000/api/negotiations/{session_id}/messages`：读取聊天记录；
- `POST http://localhost:8000/api/negotiations/{session_id}/messages`：发送消息并触发 Seller Agent；
- `POST http://localhost:8000/api/negotiations/{session_id}/confirm`：按当前有效报价明确确认交易意向；
- `POST http://localhost:8000/api/negotiations/{session_id}/close`：结束当前协商并取消仍待处理的审批；
- `GET http://localhost:8000/docs`：OpenAPI 文档。

系统只有一个统一账号登录态，保存在 `secondhand_user_session` HttpOnly Cookie 中，前端请求会自动携带。同一浏览器配置的标签页共享该 Cookie：任一标签页登录、注册或退出后，其他标签页会自动刷新为同一账号；如需同时操作两个账号，必须使用无痕窗口、独立浏览器配置或不同浏览器。旧的访客 Cookie、卖家专用 Cookie、身份请求头和旧认证 URL 均不再是授权来源；不要在请求体或请求头中自行传递用户 ID。

`/api/ready` 会分别返回认证和模型配置状态。未配置 `AUTH_SECRET` 时不能注册或登录；未配置模型时仍可浏览公开信息和管理商品，但不能发送协商消息。

配置真实千问地址和密钥后，可显式执行一次结构化输出与对抗性冒烟测试；脚本固定运行 5 个场景，并在调用前输出超时、重试和理论最大提供商尝试数。该命令会真实调用模型并可能产生少量费用：

```powershell
cd backend
python scripts/smoke_model.py
```

离线 A/B/C 评测默认使用确定性候选模型，不访问网络或业务数据库；结果写入被 Git 忽略的 `backend/evaluation/results/`。运行结束会同时生成原始 JSONL、JSON/CSV 汇总、Markdown 报告、人工复核队列和确定性安全门禁；门禁失败时命令返回退出码 `2`：

```powershell
cd backend
python -m evaluation.cli --batch-id v3-deterministic

# 只从已落盘原始文件重新计算报告和门禁
python -m evaluation.report_cli evaluation/results/v3-deterministic
```

真实模型评测必须额外传入 `--model qwen --allow-real-model`，显式设置样本、提供商尝试、Token、费用和超时预算，并在 `.env` 提供价格快照。C 是正式效果主组，默认全量运行 100 个场景；真实模型批次会生成报告与人工复核队列，但不会冒充确定性门禁，`gate_status` 为 `NOT_APPLICABLE`：

```powershell
python -m evaluation.cli `
    --batch-id v3-qwen-c `
    --model qwen `
    --allow-real-model `
    --groups C `
    --tag c_primary `
    --max-samples 100 `
    --max-model-calls 140 `
    --max-tokens 250000 `
    --max-cost 0.20 `
    --timeout-seconds 1800
```

正式协议使用 C 全量 100 场景和 A 高风险 30 场景各 3 次重复，再由 `python -m evaluation.aggregate_cli ... --output-dir ...` 聚合；聚合器默认拒绝 dirty、模拟模型或元数据不一致的批次。场景、隔离与预算设计见 [`docs/V3_阶段7_离线模拟买家与三组对比实验.md`](docs/V3_阶段7_离线模拟买家与三组对比实验.md)；报告、门禁、聚合和容器化复现见 [`docs/V3_阶段8_评测报告回归门禁与可复现交付.md`](docs/V3_阶段8_评测报告回归门禁与可复现交付.md)。

## 验证

完整的双账号 V2.1 人工验收流程与异常场景见 [`docs/V2.1_验收清单.md`](docs/V2.1_验收清单.md)；V2 历史验收记录保留在 [`docs/V2_验收清单.md`](docs/V2_验收清单.md)。

```powershell
cd backend
python -m pytest
python -m ruff check app evaluation tests scripts migrations
python -m pip_audit -r requirements.lock

# MySQL 运行且已迁移时，额外执行集成测试
$env:RUN_MYSQL_INTEGRATION = "1"
python -m pytest tests/integration
Remove-Item Env:RUN_MYSQL_INTEGRATION

cd ..\frontend
npm run type-check
npm run build
npm audit --omit=dev --registry=https://registry.npmjs.org

cd ..
docker compose config --quiet
docker compose build api frontend
```

`backend/requirements.lock` 固定了通过当前 V1 验证的 Python 依赖版本。修改 `pyproject.toml` 后需要重新解析依赖、更新锁文件并重新执行漏洞审计。

## 目录

```text
backend/      FastAPI、业务 Service、Agent 工具、Worker、ORM、迁移、评测、报告、种子脚本和测试
frontend/     Vue 3 页面、Vite 开发配置及本地复现用 Nginx 镜像
docs/         分阶段设计、契约与验收记录
scripts/      项目级本地复现脚本
compose.yaml V3 本地完整复现栈；V4 再提供生产覆盖配置
```

V1 最小闭环、V2 八个阶段和 V2.1 七个阶段均已完成。系统终点是记录交易意向，不包含支付、库存锁定或订单履约。
