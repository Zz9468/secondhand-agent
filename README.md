# SecondHand Agent

面向个人闲置交易的自主协商卖家助手。当前正在实施 V1：一个演示卖家、一个商品和一个买家会话，先验证自主协商与硬约束。

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

当前阶段不会调用真实模型，也不需要模型 API Key。实际千问接入在 V1 阶段五完成。

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

先复制环境变量模板并启动 MySQL：

```powershell
Copy-Item .env.example .env
docker compose up -d mysql
docker compose ps
```

安装后端依赖，执行迁移并初始化演示数据：

```powershell
conda activate secondhand-agent
Set-Location backend
python -m pip install -e ".[dev]"
python -m alembic upgrade head
python scripts/seed_data.py
```

启动后端：

```powershell
Set-Location backend
python -m uvicorn app.main:app --reload
```

启动前端：

```powershell
Set-Location frontend
npm install
npm run dev
```

浏览器访问 `http://localhost:5173`。后端接口：

- `GET http://localhost:8000/api/health`：仅检查 API 进程；
- `GET http://localhost:8000/api/ready`：检查 API 与 MySQL 连接；
- `GET http://localhost:8000/docs`：OpenAPI 文档。

## 验证

```powershell
Set-Location backend
pytest
ruff check app tests scripts migrations

# MySQL 运行且已迁移时，额外执行集成测试
$env:RUN_MYSQL_INTEGRATION = "1"
pytest tests/integration
Remove-Item Env:RUN_MYSQL_INTEGRATION

Set-Location ..\frontend
npm run type-check
npm run build
```

## 目录

```text
backend/      FastAPI、业务 Service、Agent 工具、ORM、迁移、种子脚本和测试
frontend/     Vue 3 最简页面
compose.yaml 本地 MySQL
```

Seller Agent、正式回复安全校验和聊天闭环将在后续 V1 阶段依次加入。
