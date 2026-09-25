# SecondHand Agent

面向个人闲置交易的自主协商卖家助手。V1 已形成一个演示卖家、一个商品和一个买家会话的最小聊天闭环，用于验证自主协商与硬约束。

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
- 普通咨询只从数据库公开商品字段生成确定性回复，不发送模型自由文本；
- 正式还价与接受回复忽略模型候选文案，只从已校验并持久化的报价快照生成；
- 非法价格、未知附加条件、虚假审批、格式错误和越权承诺均降级为非正式安全回复；
- 集成测试使用可控的模拟决策提供者，不消耗真实模型额度，也可稳定复现安全边界。

V1 阶段六已完成最小聊天闭环与验收场景：

- 提供查询协商状态、发送消息和读取消息记录的 FastAPI 接口；
- 买家身份由请求头绑定并校验，不能跨买家读取或操作会话；
- 聊天文字与正式报价字段分离，金额和运费由后端以 `Decimal` 校验；
- 同一会话通过行锁串行处理；买家消息、结构化报价、Agent 工具动作和最终回复在同一外层事务中提交或回滚；
- 幂等指纹覆盖聊天文字和完整结构化报价，重放时返回原始结果和正式报价编号；
- 所有正式还价入口都会校验最大议价轮次，最后一轮只能回应本轮买家正式报价；
- Seller Agent 每轮读取数据库中的商品、报价历史和最近聊天上下文；
- Vue 页面可展示商品、协商进度、当前报价和历史消息，并提交普通咨询或正式报价；
- 自动化测试覆盖商品咨询、多轮低价、包邮净收入、审批区提示、Prompt Injection、身份隔离、完整幂等、事务回滚和最大轮次；
- 种子脚本支持显式重置演示会话，便于从干净状态重复演示。
- MySQL 开发端口只绑定 `127.0.0.1`，不会监听局域网网卡。

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

打开根目录下的 `.env`，将两个 `CHANGE_ME` 数据库口令替换为不同的随机值，并把 `MYSQL_PASSWORD` 的值同步写入 `DATABASE_URL`。真实模型配置可以暂时留空；未配置模型时仍可查看页面和演示数据，但不能发送协商消息。

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

如需清空固定演示会话 `1001` 的消息和报价并重新演示，请显式执行：

```powershell
cd backend
python scripts/seed_data.py --reset-session
cd ..
```

该选项只重置固定演示会话；不带参数执行时仍是非破坏性的幂等初始化。

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

浏览器访问 `http://localhost:5173`。后端接口：

- `GET http://localhost:8000/api/health`：仅检查 API 进程；
- `GET http://localhost:8000/api/ready`：检查 MySQL 连接和模型配置状态；
- `GET http://localhost:8000/api/negotiations/1001`：读取演示协商状态；
- `GET http://localhost:8000/api/negotiations/1001/messages`：读取聊天记录；
- `POST http://localhost:8000/api/negotiations/1001/messages`：发送消息并触发 Seller Agent；
- `GET http://localhost:8000/docs`：OpenAPI 文档。

协商接口要求请求头 `X-Buyer-ID: demo-buyer`。前端已经为演示会话绑定该身份；它只是 V1 的临时访客标识，不等同于正式登录认证。

`/api/ready` 还会返回模型配置状态。未配置模型时页面仍可查看演示数据，但会显示“模型未配置”并禁止发送消息。

配置真实千问地址和密钥后，可显式执行一次结构化输出冒烟测试；该命令会真实调用模型并可能产生少量费用：

```powershell
cd backend
python scripts/smoke_model.py
```

## 验证

```powershell
cd backend
pytest
ruff check app tests scripts migrations
python -m pip_audit -r requirements.lock

# MySQL 运行且已迁移时，额外执行集成测试
$env:RUN_MYSQL_INTEGRATION = "1"
pytest tests/integration
Remove-Item Env:RUN_MYSQL_INTEGRATION

cd ..\frontend
npm run type-check
npm run build
npm audit --omit=dev --registry=https://registry.npmjs.org
```

`backend/requirements.lock` 固定了通过当前 V1 验证的 Python 依赖版本。修改 `pyproject.toml` 后需要重新解析依赖、更新锁文件并重新执行漏洞审计。

## 目录

```text
backend/      FastAPI、业务 Service、Agent 工具、ORM、迁移、种子脚本和测试
frontend/     Vue 3 最简页面
compose.yaml 本地 MySQL
```

V1 最小闭环已经完成；完整卖家审批、买家最终确认意向和正式身份认证属于 V2 范围。
