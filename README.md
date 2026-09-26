# WorkMind AI — 智能办公 Agent 平台

基于 Vue3 + Python(FastAPI) + LangChain/LangGraph + DeepSeek 构建的智能办公 Agent 系统。

## 项目模块

| 模块 | 说明 | 状态 |
|------|------|------|
| 智能对话助手 | 多轮对话 / 流式输出 / 用户画像 | ✅ 已完成 |
| 知识库问答   | 文档上传 / RAG 检索 / 来源标注 / 未命中原因诊断 | ✅ 已完成 |
| 任务 Agent   | Function Call / ReAct / 工具可视化 | 🔄 开发中 |
| 内容工作流   | 周报/纪要/邮件/PRD 工作流 | 🔄 开发中 |
| ERP 报销请假 | 智能填单 / Multi-Agent 审批 | 🔄 开发中 |
| Prompt 调试  | A/B测试 / 版本管理 | 🔄 开发中 |
| 用量看板     | Token消耗 / 费用 / 缓存统计 | 🔄 开发中 |

## 技术栈

| 模块 | 选型 |
|------|------|
| 前端 | Vue3 + Vite + Pinia + Vue Router + Element Plus |
| Web 框架 | FastAPI + Uvicorn |
| 校验 | Pydantic |
| AI 框架 | LangChain (Python) / LangGraph (Python) |
| 对话模型 | DeepSeek（OpenAI 兼容） |
| Embedding | 智谱 AI / OpenAI 兼容(SiliconFlow) |
| 向量库 | PostgreSQL 17 + pgvector（HNSW 索引，过滤下推到 SQL WHERE） |
| 文件上传 | FastAPI `UploadFile` |
| PDF 解析 | pypdf |
| 限流 | 自实现令牌桶 |
| 部署 | Docker + docker-compose |

## 项目结构

```
workmind/
├── frontend/                 Vue3 前端
│   ├── src/
│   │   ├── views/            各模块页面
│   │   ├── components/       UI 组件
│   │   ├── stores/           Pinia 状态
│   │   ├── router/           路由
│   │   ├── utils/            工具（http 等）
│   │   └── styles/           全局样式
│   └── vite.config.js
│
├── server-py/                Python 后端
│   ├── app/
│   │   ├── main.py           入口：中间件、路由注册
│   │   ├── config.py         环境变量统一读取
│   │   ├── middleware.py     限流、日志、prompt 注入检测
│   │   ├── routes/           health / chat / knowledge / agent / workflow / erp / prompt / monitor
│   │   ├── services/         业务逻辑（db / model / cache / rag / agent / erp / prompt / workflow）
│   │   └── utils/            日志、错误处理、SSE 封装
│   ├── requirements.txt
│   ├── Dockerfile
│   └── uploads/              知识库文档上传目录（运行时生成）
│
└── docker-compose.yml
```

## 一、本地运行

### 1. 准备 Python 环境

要求 Python 3.11+（已用 3.11.9 验证）。

```bash
cd server-py
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 配置环境变量

密钥只有一份：**仓库根目录的 .env**（已被 .gitignore 忽略），后端和 docker-compose 都读它。

```bash
cp .env.example .env     # 在仓库根目录执行
```

编辑它，至少填入：

```
DEEPSEEK_API_KEY=sk-xxxx        # 必填，对话模型
ZHIPU_API_KEY=xxxx.xxxx         # 可选，RAG 知识库功能需要（优先于 OPENAI_API_KEY）
OPENAI_API_KEY=sk-xxxx          # 可选，没有智谱 Key 时的 Embedding 备选
PORT=3000
ALLOWED_ORIGINS=http://localhost:5173
```

### 3. 启动服务

```bash
# 开发模式（代码变更自动重载）
uvicorn app.main:app --reload --port 3000

# 或者直接用 python -m
python -m uvicorn app.main:app --port 3000
```

启动成功后会看到：

```
🚀 WorkMind Server (FastAPI) 已启动
   地址: http://localhost:3000
   健康检查: http://localhost:3000/health
```

### 4. 验证

```bash
curl http://localhost:3000/health/live
# {"status":"ok","uptime":xx}

curl http://localhost:3000/api/chat/roles
curl http://localhost:3000/api/agent/tools
curl http://localhost:3000/api/workflow/templates
```

FastAPI 自带交互式文档，浏览器打开 `http://localhost:3000/docs` 可以直接试调所有接口（流式 SSE 接口在 Swagger UI 里看不到实时流，建议用 `curl -N` 或前端联调）。

## 二、配合前端联调

前端是 `frontend/`（Vite 项目），[vite.config.js](frontend/vite.config.js) 已把 `/api` 代理到 `http://localhost:3000`，启动顺序：

```bash
# 后端
cd server-py && uvicorn app.main:app --port 3000

# 前端单独启动
cd frontend && npm install && npm run dev
# 页面打开在 http://localhost:5173
```

## 三、Docker 运行

```bash
cd server-py
docker build -t workmind-server-py .
docker run -d --name workmind-server-py \
  -p 3000:3000 \
  -e DEEPSEEK_API_KEY=sk-xxxx \
  -e ZHIPU_API_KEY=xxxx.xxxx \
  -e ALLOWED_ORIGINS=http://localhost:5173 \
  -v $(pwd)/uploads:/app/uploads \
  workmind-server-py
```

容器内置健康检查，命中 `/health/live`。

### 一键启动前端 + 后端（推荐）

根目录的 `docker-compose.yml` 会同时起两个容器：

| 服务 | 容器名 | 地址 | 说明 |
|------|--------|------|------|
| db | `workmind-db` | localhost:5433 → 5432 | PostgreSQL 17 + pgvector，知识库持久化（数据卷 `workmind-pgdata`） |
| server | `workmind-server` | http://localhost:3000 | FastAPI 后端（`/docs` 有交互式文档） |
| frontend | `workmind-frontend` | http://localhost:5173 | Vue3 页面，Nginx 托管并把 `/api` 反代到后端 |

```bash
# 1. 准备环境变量（compose 读取根目录 .env；密钥只放这一份）
cp .env.example .env
# 编辑 .env，至少填入 DEEPSEEK_API_KEY

# 2. 一键启动
docker compose up -d

# 3. 验证
curl http://localhost:3000/health/live
# 浏览器打开 http://localhost:5173
```

> 前端是**同源**访问后端（Nginx 反向代理），因此不存在跨域问题，
> `ALLOWED_ORIGINS` 只在前后端分开跑（如 `npm run dev`）时才生效。

> 如果构建前端时拉取 npm 依赖超时，可以换源：
> ```bash
> docker compose build --build-arg NPM_REGISTRY=https://registry.npmmirror.com frontend
> ```

> 知识库已从"进程内存"迁到 **PostgreSQL + pgvector**（compose 里的 `db` 服务），
> 文档与切片持久化，**容器重建/重启都不会丢索引**。
> 表结构由后端启动时幂等创建（`app/services/db.py`），不需要手工执行建表脚本。
> 用客户端连库看看：`psql postgresql://workmind:workmind@localhost:5433/workmind`

## 四、知识库存储与检索（PostgreSQL + pgvector）

### 为什么落库

之前 `documents` / `chunks` 全在进程内存里：容器一重建（`--build`、重启、改代码）
索引就没了，而 `uploads/` 里的源文件还在 —— 表现和"权限过滤掉了"一模一样：
库里看着有文档，检索却 0 条，且没有任何日志能看出来。现在两张表都在 PostgreSQL 里。

### 表结构

| 表 | 作用 | 关键点 |
|----|------|--------|
| `documents` | 文档级元数据 | 权限（tenant/department/security_level）、版本治理（status/effective_date/expired_date/superseded_by）、解析溯源 |
| `chunks` | 切片 + 向量 | `embedding vector(2048)`（智谱 embedding-3 的维度）+ HNSW 余弦索引；`ON DELETE CASCADE` 跟随文档删除 |

设计约定：**切片只存"不会变的过滤字段快照"（tenant/department/version/doc_type/security_level），
会变的字段（status / superseded_by / effective_date）检索时 JOIN documents 现算** —— 避免双写不一致。

### 检索为什么是"先过滤再算相似度"

权限与版本条件全部下推到 SQL 的 WHERE（见 `identity.doc_visibility_sql`），
数据库先剔掉不可见的切片，再按 `embedding <=> 查询向量` 排序取 TopK。
不会出现"TopK 全被权限过滤掉 → 明明有权限内文档却答不出来"。
命中后还会再用 Python 的 `can_view` / `version_visible` 复核一遍（安全底线不依赖 SQL 单点）。

### 要不要查知识库：默认"召回优先"（recall_first）

**不要**把"该不该查库"交给模型凭常识判断——它不知道公司库里有什么。
实测：输入「rag召回失败」（库里正好有讲 RAG 的资料），旧的严格规则判"不需要检索"
（没有疑问词、没命中关键词），连模型都没问；就算问模型，它也会答
"这属于通用技术问答，无需检索公司内部文档"。用户看到的就是"我问了它却不查库"。

现在（`RAG_INTENT_MODE=recall_first`，默认）：

| 输入 | 行为 |
|------|------|
| 「rag召回失败」 | **默认检索** → 命中就带引用回答，没命中就说明原因 |
| 「你好」「谢谢」「1+1」 | 跳过检索（打招呼/客套话/纯算式） |
| 库里一条切片都没有 | 仍然会检索，界面直接显示"知识库当前没有可用内容" |
| 用户选「强制」/「关闭」 | 覆盖上面的判定，前后端都认 |

"智能对话"和"任务 Agent"**共用同一个判定**（`app/services/rag/intent.py`）。
Agent 会**先真的检索一次**，再决定路由：

- 检索有命中 → 走知识分支（带引用回答）
- 检索没命中 → 交还给模型路由器在 tool / chat 之间二选一，
  并把"没命中"的原因（候选数 / 最高分 / 阈值）显示在任务卡片上

为什么不是"需要查库就直接进知识分支"：Agent 还有计算、日期、通知等工具。
一句「算一下 1500+800*0.8」如果被塞进知识分支，会因为没命中而直接结束，
**工具根本没机会执行**。实测现在这条正确走 `calculate` 并返回 2140，
而「rag召回失败」走知识分支命中 6 条。

以前 Agent 有自己的路由器，模型会把"rag召回失败"判成"直接回答"，于是两条链路都不查库。

想恢复旧行为：`RAG_INTENT_MODE=strict`。

### 检索不到时答不答：`RAG_ANSWER_POLICY`（默认 grounded）

| 取值 | 行为 |
|------|------|
| `grounded`（默认，知识优先） | 需要查库但没查到 → **不调用模型**，直接回答"知识库中未找到相关内容" + 检索情况 + 下一步怎么办。不拿模型自身知识去顶，避免与公司资料口径不一致 |
| `fallback` | 查不到就交给模型凭自身知识正常回答（旧行为） |

配套的两条规则：
1. 命中资料时，system 里写死"这些资料是本轮回答的**唯一依据**"，资料不足的部分明确说没找到，
   **不许补充、推测、举一反三**（以前只写了"不要编造"，模型还是会顺着自身知识往下讲）；
2. `grounded` 的固定答复里会告诉用户可以切「知识库：关闭」用通用知识回答，
   所以"什么是向量数据库"这类问题不会被卡死 —— 只是默认不去猜。

### 角色预设 → 检索范围预设

原来的四个角色（通用助手 / 技术顾问 / HR 助理 / 法务助理）**已合并为一个助手**，理由：

- 它们在代码里只做一件事：换一句 system prompt。**不影响检索到哪些文档**，
  也不影响权限、模型、温度 —— 对用户能感知的结果没有差异；
- 它们和"知识优先"直接冲突：技术顾问那句"回答要有代码示例、说明清楚原理"、
  HR 那句"回答要有温度"，都会诱导模型在资料之外发挥，而现在的规则是
  "资料不足就明确说没找到，不许补充、推测、举一反三"；
- 它们还和旁边的**身份切换**语义打架：一个改"我是谁（能看哪些文档，真影响结果）"，
  一个改"你扮演谁（只影响措辞）"，很容易把权限问题误判成角色问题。

那一行 UI 换成了**检索范围预设** —— 每一项都真的改变"查哪些文档"：

| 预设 | 下发的参数 | 效果 |
|------|-----------|------|
| 全部可见（默认） | 无 | 当前身份能看到的全部文档 |
| 全员通用 | `knowledgeDepartment=general` | 只看部门=全员通用的文档 |
| 本部门 | `knowledgeDepartment=<当前身份首个部门>` | 切换身份后自动跟随 |
| 制度规定 / 手册指南 | `knowledgeDocType=policy/manual` | 按文档类型收窄 |
| 含历史版本 | `includeSuperseded=true` | 把"已被替代"的版本也纳入检索 |

选了范围却没命中时，"未找到"的答复会**优先说明是自己把范围选窄了**，
并提示切回「全部可见」，而不是笼统地说"被权限过滤掉了"。

> `role` 字段与 `GET /api/chat/roles` 仍然保留（老客户端不报错），但已废弃、不再改变行为。
> 想自定义人设请用 `systemPrompt` 字段（后端已支持，前端暂未暴露输入框）。

### 召回不到内容时，先看是哪一类原因

`POST /api/knowledge/search` 和对话流的 `sources` 事件都会带上 `recall`：

| reason | 含义 | 怎么处理 |
|--------|------|----------|
| `kb_empty` | 库里一条切片都没有 | 去知识库页重新入库 |
| `all_filtered` | 有切片但全被权限/版本/显式筛选条件挡住 | 看文档的密级/部门/生效日期/状态，或换身份验证 |
| `below_threshold` | 有候选，但最高相似度低于阈值 | **按当前 embedding 模型重新标定 `RAG_SIMILARITY_THRESHOLD`** |
| `storage_unavailable` | 数据库连不上 | 检查 `db` 容器与 `DATABASE_URL` |
| `embedding_unavailable` | 没配 embedding Key | 配 `ZHIPU_API_KEY` 或 `OPENAI_API_KEY` |

前端会把原因和数字（库内切片数 / 通过过滤数 / 最高分 / 阈值）直接显示在回答上方，
不再统一显示成"当前身份下未命中内容"。

### 阈值必须按 embedding 模型标定

实测（智谱 `embedding-3`，2048 维）：

| 查询 | 原始最高相似度 |
|------|----------------|
| 与切片近乎逐字重复 | 0.57 |
| 语义相关、换了说法 | 0.42 ~ 0.45 |
| 只看标题关键词 | 0.69 |

也就是说**同一话题换个问法只有 0.4 出头**，阈值写 0.5 会让这类查询整片召不回。
所以默认 `RAG_SIMILARITY_THRESHOLD=0.35`。换 embedding 模型后要重新标定：
拿几个"应该命中"和"明显不相关"的问题各跑一遍 `/api/knowledge/search`，
把阈值放在两组 `bestScore` 之间。库里同一话题文档很多时，可以用
`RAG_SIMILARITY_MARGIN`（只保留 ≥ 最高分 − MARGIN 的切片）收敛噪音，默认 0 = 关闭。

> 会话输入框和 Agent 页都有「知识库：自动 / 强制 / 关闭」三态开关，
> 分别对应 `useKnowledge: undefined / true / false`，不用猜后端这次会不会去查。

> `department` 是**硬过滤**（用户在前端明确选的部门），
> 意图模型猜出来的 `department_hint` 只做提示、**不参与候选集裁剪**
> —— 猜错一次（把 general 文档猜成 tech）会让整份文档召不回的代价太大。

### 本地不用 Docker 跑后端

单独起一个 PostgreSQL（`pgvector/pgvector:pg17` 镜像或本机安装 + `CREATE EXTENSION vector`），
然后在 `.env` 里配任一形式：

```
DATABASE_URL=postgresql://workmind:workmind@localhost:5433/workmind
# 或 PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE
```

连不上时后端**仍会启动**，但知识库接口统一返回 503 并说明原因（`STORAGE_UNAVAILABLE`），
`GET /health` 的 `status` 会变成 `degraded` —— 不会静默降级成"检索无结果"。

## 五、接口一览

| 路由前缀 | 功能 |
|---------|------|
| `GET /health`、`/health/live` | 健康检查 |
| `/api/chat/*` | 流式对话、会话管理、用户画像、角色预设 |
| `/api/knowledge/*` | 文档上传/列表/删除、RAG 流式问答 |
| `/api/agent/*` | ReAct Agent 任务执行（流式）、工具列表、示例任务 |
| `/api/workflow/*` | 4 个内置工作流（周报/会议纪要/邮件润色/PRD），支持人工审核暂停/恢复 |
| `/api/erp/*` | 自然语言转结构化表单、Multi-Agent 审批流 |
| `/api/prompt/*` | Prompt 单测(流式)、A/B 测试、模板管理 |
| `/api/monitor/*` | 用量看板：调用统计、Token、成本、缓存命中率 |

所有 SSE 流式接口的事件格式均为：

```
event: <type>
data: <json>

```

错误统一返回：

```json
{ "error": { "code": "...", "message": "...", "retryable": false } }
```

## 六、常见问题

**Q: 启动时报错 `❌ 缺少 DEEPSEEK_API_KEY`？**
`.env` 里没填 `DEEPSEEK_API_KEY`，对话/Agent/工作流/ERP 等所有调用大模型的功能都依赖它，必须配置。

**Q: 调用 `/api/chat/stream` 等接口返回 401 / "服务配置错误"？**
说明 DeepSeek（或智谱）API Key 无效/过期，可以用以下命令直接验证 Key 是否可用：

```bash
curl https://api.deepseek.com/v1/chat/completions \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}'
```

**Q: 上传 PDF 知识库文档失败？**
确认已安装 `pypdf`（在 `requirements.txt` 中），且文件后缀是 `.txt` / `.md` / `.pdf`，单文件不超过 10MB。
