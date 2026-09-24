# SecondHand Agent

面向个人闲置交易的自主协商卖家助手。当前正在实施 V1：一个演示卖家、一个商品和一个买家会话，先验证自主协商与硬约束。

## 当前阶段

阶段一提供最小工程骨架：

- Python 3.11 + FastAPI 后端；
- LangChain 1.x 模型能力边界；
- MySQL 8.4（InnoDB）本地 Compose 服务；
- Vue 3 + Vite 最简状态页面；
- 进程健康和数据库就绪检查；
- 后端基础测试与前端构建检查。

阶段一不会调用真实模型，也不需要模型 API Key。实际千问接入在 V1 阶段五完成。

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

启动后端：

```powershell
Set-Location backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
uvicorn app.main:app --reload
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
ruff check app tests

Set-Location ..\frontend
npm run type-check
npm run build
```

## 目录

```text
backend/      FastAPI、配置、数据库连接、模型能力边界和测试
frontend/     Vue 3 最简页面
compose.yaml 本地 MySQL
```

业务规则、ORM 模型、Agent 工具和正式聊天闭环将在后续 V1 阶段依次加入。

