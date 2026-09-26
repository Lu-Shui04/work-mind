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
| 用量看板     | Token消耗 / 费用 / 缓存统计（全模块统一记账，落 PostgreSQL） | ✅ 已完成 |
| 全链路追踪   | 一次请求内部的完整执行链路：意图 / 向量化 / 检索 / 重排 / 缓存 / 提示词 / 模型 / 工具入参出参 / 异常 | ✅ 已完成 |

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
├── docs/company/             公司制度文档源文件 + 元数据清单（灌库用）
├── scripts/                  运维脚本（seed-knowledge-base.sh 等）
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

### 知识库三态：自动 / 强制 / 关闭（语义以 `resolve_knowledge_mode` 为准）

对话页和 Agent 页输入框下方都有这个开关，三条规则由 `rag/query.py::resolve_knowledge_mode` **统一裁决**
（以前散在路由的 if 里，出现过"用户明明选了关闭，却还回一句知识库未找到"这种自相矛盾的行为）：

| 模式 | 查到资料 | 库里没有 |
|------|---------|---------|
| **自动**（默认） | 只依据资料回答，标注引用【来源：…】 | 先说明「知识库未命中：<原因>」，**然后照常用通用知识回答** |
| **强制** | 同上 | 只回「知识库中未找到相关内容」，**不许**用模型自己的知识补充 |
| **关闭** | ——（压根不检索） | 直接回答，不会出现任何「未找到」话术 |

> 部署方想让「自动」也走严格模式：`RAG_ANSWER_POLICY=grounded`（默认 `fallback`）。
> 这个开关**只影响自动模式**，强制模式永远只依据知识库。
> 前端的三态选择会存 localStorage —— 以前是纯内存状态，刷新后静默回到「自动」，
> 用户以为还是「关闭」，下一句就又被知识库拦了。

### 会话记忆：摘要 + 最近 N 轮（对话与 Agent 共用，落 PostgreSQL）

`services/chat/memory.py` 是唯一的记忆实现，两条链路都用它：

| 层 | 内容 | 说明 |
|----|------|------|
| 短期 | 最近 `MEMORY_KEEP_ROUNDS`（默认 10）轮**原文** | 每轮请求都带上 |
| 中期 | 更早对话的**滚动摘要** | 轮数超过 `MEMORY_SUMMARY_AFTER_ROUNDS`（默认 10）时，**异步**用大模型把更早的对话合并进摘要 |
| 跨会话 | 用户画像 | 从对话里异步抽取的姓名/部门/偏好等 |

为什么要摘要而不是无限带原文：一轮 = 一问一答，全部重发会让每次请求的 token 越聊越多；
而只做 token 滑窗又会把早期说过的话直接丢掉 —— 用户问"刚才那个再改一下"，模型完全不知道指什么。
摘要把"已经确认的事实、未完成的事项、关键数字"留下，寒暄和重复丢掉，
并且**异步执行**：本轮回答不会被摘要阻塞，摘要失败也只是保留原文、不会丢记忆。

#### 持久化：重启不丢

三种记忆都落在同一个 PostgreSQL（和知识库一个库，见 `db.py` 的建表语句）：

| 表 | 存什么 |
|----|--------|
| `chat_sessions` | 会话本身 + 滚动摘要（主键 `(tenant_id, session_id)`） |
| `chat_messages` | 每一轮的问与答（外键级联到会话） |
| `user_profiles` | 跨会话的用户画像（JSONB） |

> 主键用 `(tenant_id, session_id)` 复合键：`session_id` 是前端生成的，
> 只用它做主键的话，别的租户猜到一个 id 就能读到别人的对话内容。

以前这三样都在进程内的 dict 里 —— 后端一重启（改一行代码、`docker compose up --build`、机器重启）
就全没了，用户上一句刚说过的名字下一句就不认识，而且没有任何提示，看起来像"模型变笨了"。

**数据库连不上时**会自动退回进程内存（对话不至于整个不可用），并在日志里记一条降级提示
（`memory: 数据库不可用，会话记忆退回进程内存（重启会丢）`）—— 不再出现"重启就丢却没人知道为什么"。

验收脚本（会真的重启一次后端容器）：

```bash
bash scripts/check-memory-persistence.sh
# [1/3] 写入记忆 → [2/3] 重启后端容器 → [3/3] 重启后回忆
```

Agent 侧：任务按 `sessionId` 归属（前端持久化在 localStorage，点「清空」会换新会话并清掉后端记忆），
所以第二个任务可以直接说"刚才那次出差，酒店改成 700 一晚"。

### 检索不到时答不答：`RAG_ANSWER_POLICY`

| 取值 | 行为 |
|------|------|
| `fallback`（默认） | 自动模式下查不到 → 先说明未命中，再用通用知识回答 |
| `grounded` | 自动模式下查不到 → **不调用模型**，只回"知识库中未找到相关内容" + 检索情况 + 下一步怎么办 |

配套的两条规则：
1. 命中资料时，system 里写死"这些资料是本轮回答的**唯一依据**"，资料不足的部分明确说没找到，
   **不许补充、推测、举一反三**（以前只写了"不要编造"，模型还是会顺着自身知识往下讲）；
2. 强制模式下没命中时，答复里会告诉用户可以切「自动」或「关闭」拿到通用知识回答。

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
| `rerank_rejected` | 召回有候选，但**重排判定它们都回答不了这个问题**，被下限丢弃 | 正常现象（问题不在库里）。若误杀真命中，调低 `RAG_RERANK_MIN_SCORE` |
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

### 重排下限：向量分够、但答案不对的"假命中"必须丢掉

向量阈值只保证"主题沾边"，拦不住"讲的是同一类东西、但回答不了这个问题"的切片。
这类假命中留在结果里不只是答得不好 —— Agent 会据此认为"知识库命中 N 条"，
于是把整次任务锁死在知识分支，回一句"未找到相关内容"就结束，工具（联网搜索/计算/报告）
完全没机会执行。所以重排分数低于下限的候选**直接丢弃**，而不是"排到后面"。

实测同一套库（`RERANK_PROVIDER=bge`，`BAAI/bge-reranker-v2-m3`）：

| 查询 | 重排最高分 | 处置 |
|------|-----------|------|
| B端和C端AI产品的设计差异是什么 | 1.00 | 命中，正常带引用回答 |
| 什么是 RAG | 0.98 | 命中 |
| 对比 Vue3 和 React…生成技术选型报告 | **0.04** | 全部丢弃 → 交回模型路由走工具分支 |

`RAG_RERANK_MIN_SCORE` 不填时按 provider 取默认（bge=0.3，llm=5），设 0 = 关闭过滤。
重排接口调用失败时**不启用**该过滤（fail-open），不让重排把检索搞挂。

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

### 灌入公司制度文档（示例任务依赖）

Agent 和智能对话的示例任务要查的是**公司制度**（差旅报销标准、年假政策…），
而数据库重建后（换机器、`docker compose down -v`、删掉 `workmind-pgdata` 卷）知识库是空的，
示例任务就会「查不到公司信息」，看起来像 Agent 坏了。所以制度文档在仓库里有源文件，可一键灌入：

```
bash scripts/seed-knowledge-base.sh          # 默认 http://localhost:3000
API_BASE=http://localhost:3000 bash scripts/seed-knowledge-base.sh
```

| 位置 | 内容 |
|------|------|
| `docs/company/*.md` | 8 份公司制度源文件（员工手册、差旅报销、年假、考勤加班、前端规范、付款预算、合同保密、PRD 规范） |
| `docs/company/manifest.txt` | 每份文档的元数据（部门 / 类型 / 密级 / 版本 / 生效日期 / 标签），脚本按它上传 |
| `scripts/seed-knowledge-base.sh` | 逐份调用 `POST /api/knowledge/documents`，可重复执行 |

脚本是**幂等**的：后端按文件 sha256 去重，重复执行只会复用已入库的文档，
不重复解析、不重复向量化、不重复计费；改了元数据（部门/密级/版本）会自动按新元数据重新入库。

文档的部门/密级是按演示需要设计的，灌完后各身份的可见范围：

| 身份 | 可见文档 |
|------|---------|
| 技术研发（tech / internal） | 3 份 general 制度 + 前端研发规范 + 2 份公开面试资料 |
| 人力资源（hr / internal） | 3 份 general 制度 + 考勤与加班管理规定 + 2 份公开面试资料 |
| 管理层（全部门 / confidential） | 全部 10 份（含法务的机密文档） |
| 匿名访客（public） | 只有 2 份公开面试资料 |

> 公司级制度（员工手册、差旅报销、年假）用 `department=general` + `internal`：
> `general` 不受部门限制、`internal` 是绝大多数员工的密级，所以**任何身份都能查到** ——
> 这符合真实企业里全员适用制度的发布方式，也让示例任务对默认身份可用；
> 部门专属文档保留部门限制，用来演示权限过滤。

## 五、流式输出审计（哪些地方原来不是流式）

用户在界面上等的是"字一个个出来"，所以**凡是会生成大段文字的地方都必须是流式的**。
2026-09 做了一轮全量排查，发现三处名不副实，都已修掉：

| 位置 | 原来的问题 | 现在 |
|------|-----------|------|
| 工作流 生成结果（周报/纪要/邮件/PRD） | 事件里**根本没有 token**：节点用的模型是 `streaming=False` 创建的，`astream_events` 不会发 `on_chat_model_stream`；而且 `/start/stream` 连转发 token 的代码都没有 | 模型开流式；`start` 与 `resume` 共用一段事件翻译；token 带 `nodeId` + `isResult` |
| 工作流 点「开始执行」后的界面 | 一直停在输入表单上，没有进度也没有转圈 —— `startWorkflow` 先置 `running=true` 又调用 `reset()`，而 `reset()` 把它置回了 false | 顺序修正；执行中显示节点进度列表，并把左侧流程图滚进视野 |
| Prompt A/B 对比 | 跑 **5 次模型调用**（2 生成 + 3 评分）后一次性返回，最长的一个等待 | 改成 `POST /api/prompt/ab-test/stream`：两个变体并行流式、token 带 `variant` 各进各的列，评分阶段单独发 `scoring` 事件 |

排查过但**有意保持非流式**的（输出是短结构体，不是长文本，流式没有收益）：

- `POST /api/erp/parse`：自然语言 → 结构化表单（一个 JSON，2~4 秒）
- `POST /api/knowledge/metadata/suggest`：AI 预填元数据（同上）
- `POST /api/knowledge/search`：检索结果列表

已经天然流式、无需改动的：对话、知识库问答、Agent 任务、ERP 审批流、Prompt 单测。

### 工作流的 token 怎么分流

同一个工作流里多个节点都会调模型，token 全塞给正文就会串台，所以事件里带两个标记：

- `isResult=true` —— 结果节点（`WORKFLOW_META[].resultNode`）的 token，追加到右侧正文面板
- 其它节点的 token 贴在左侧流程图**对应的卡片**上（截断到 240 字），流程走到哪一步、正在生成什么都能实时看见

顺带修掉一个一致性坑：`resume` 用的是 `onToken` 回调，而 `fetchStream` 里 `token` 是 else-if 分支 ——
给了 `onToken` 就收不到带 `nodeId` 的 `onEvent`，节点卡片上的流式预览会丢。现在统一走 `onEvent`。
## 六、Agent 工具（`GET /api/agent/tools`）

| name | 中文名 | 说明 |
|------|--------|------|
| `web_search` | 联网搜索 | **真实联网**：`ZHIPU_API_KEY`（智谱 Web Search，知识库那个 key 直接复用）/ `TAVILY_API_KEY` / `BOCHA_API_KEY` 配任一个即可；都没配才进 demo 模式（只返回演示数据并明确标注未接入） |
| `read_doc` | 检索知识库 | 按当前身份检索企业知识库，走权限过滤；未命中时把原因（库空/权限过滤/分数低/重排判定答不了）一并返回 |
| `calculate` | 数学计算 | AST 白名单求值（不是 eval）；支持千分位、全角数字、百分比；**看不懂的输入直接报错，不会静默算错** |
| `get_date` | 日期查询 | 今天日期、日期差、工作日数、日期加减；按 `APP_TIMEZONE`（默认北京时间）算，不用容器的 UTC |
| `write_report` | 生成报告 | 把结果整理成结构化 Markdown（`format=plain` 输出纯文本） |
| `send_notify` | 发送通知 | 演示环境只生成通知内容，返回值里带 `simulated: true`，不会假装真的发出去了 |

两个容易踩的坑：

1. **工具清单写死在提示词里会漂移**。`AGENT_SYSTEM` 的「可用工具」由 `all_tools` 生成，
   别再手写 —— 之前手写只列了 3 个，`web_search`/`write_report`/`send_notify` 虽然
   `bind_tools` 了但提示词没提，模型基本不用，示例任务「技术调研」就只会去查知识库。
2. **中文名映射要覆盖每一个工具**。`_TOOL_LABELS` 漏掉的会直接显示英文原名，
   前端左侧「可用工具」看起来就像"工具被改掉了"。

### 联网搜索怎么开

```bash
bash scripts/check-web-search.sh    # 自检：现在真搜还是演示数据，并实测一次查询
```

优先级是 `TAVILY > BOCHA > ZHIPU`（自动识别，也可用 `SEARCH_PROVIDER=tavily|bocha|zhipu|demo` 指定）：

| 后端 | 需要的 key | 说明 |
|------|-----------|------|
| `zhipu` | `ZHIPU_API_KEY` | 智谱 Web Search。**知识库已经用这个 key 算 embedding 了，直接复用**，国内部署通常不用再申请服务 |
| `tavily` | `TAVILY_API_KEY` | https://tavily.com ，国际通用，注册即有免费额度 |
| `bocha` | `BOCHA_API_KEY` | https://open.bochaai.com ，国内可直连的中文搜索 |
| `demo` | 无 | 只返回内置演示数据，并**如实标注未接入真实搜索** |

智谱搜索档位用 `ZHIPU_SEARCH_ENGINE` 切换：`search_std`（标准版，便宜，默认）/ `search_pro`（高级版，更准也更贵）。
搜索结果带发布时间，会一起喂给模型，回答里能标出「这条信息是哪天的」。

### Agent 循环的四个保护（都是踩过坑才加的）

1. **步数用尽会强制收尾**：模型正打算调工具时如果直接结束循环，用户拿到的「最终回答」
   其实是它调工具前的一句过程说明（实测出现过 `I have enough information now. Let me ...
   produce the report.` 之后什么都没有）。现在这种情况会走 `finalize` 分支 ——
   把工具调用记录压成纯文本喂给**不带工具**的模型，强制交一份基于已有信息的总结。
2. **过程说明与最终回答分开**：模型每次决定调工具前吐的那些话（"我先搜一下…"）会被
   后端用 `answer_reset` 事件标出来，前端挪到灰色的「过程说明」区，不再冒充最终回答。
   为什么不能只靠 `tool_call` 事件判断：步数用尽时会强制收尾，那一步的 tool_calls 不会执行，
   也就不会有 `tool_call` 事件。
3. **重复调用防护**：同一个「工具 + 参数」在本次任务里只真正执行一次，第二次起返回
   「已跳过重复调用」。实测模型会陷进「换个说法再搜一遍」的循环（一次任务发了 14 次搜索），
   步数、搜索费用、上下文全烧在重复调用上。防护层内部直接调原函数而不是 `ainvoke`，
   否则会产生嵌套 runnable，SSE 事件发两遍、前端步骤卡片翻倍。
4. **LangGraph 递归上限跟着步数一起放大**：每个 ReAct 循环占 2 个「超级步」
   （agent 一步、tools 一步），LangGraph 默认上限 25 只够跑到 12 步左右 —— 超过就在图里抛
   `Recursion limit of 25 reached`，而且是在我们自己的步数判断之前抛。
   所以 `RECURSION_LIMIT = MAX_STEPS * 2 + 5` 必须一起改（实测把步数从 10 调到 14 时踩到）。

#### 步数上限是怎么定的（`AGENT_MAX_STEPS`）

把上限临时放到 20、让 18 次真实任务自然收敛，实测**模型步数**分布：

| 任务类型 | 模型步数 | 工具调用 | 输入 tokens |
|---------|---------|---------|------------|
| 知识库问答（走知识分支） | 0 | 0 | 1.4K |
| 单工具任务（费用计算、知识查询） | 3 ~ 4 | 3 ~ 4 | 9 ~ 12K |
| 搜索 + 报告（能查到答案） | 4 ~ 6 | 5 ~ 8 | 15 ~ 37K |
| 搜索 + 报告（答案查不到，反复换关键词） | 7 ~ 10 | 8 ~ 14 | 37 ~ 95K |

中位数 5，观测最大 10。**默认取 14 = 最大值 + 40% 余量**：既不裁掉任何能自然收敛的任务，
又能拦住真正失控的循环（早期版本 8 步里发了 14 次搜索还停不下来）。
真要撞上限也不会给半截话 —— 会走 `finalize` 交一份如实说明「查到了什么、缺什么」的总结。

两个真实踩过的坑，现在都有回归测试兜着（`server-py/tests/test_agent_tools.py`）：

- **联网搜索没配 key 却假装搜过**：早期不管查什么，工具都回一句「该话题在技术社区有广泛讨论」。
  模型把它当成「搜过了但没结果」，于是换个关键词反复重试（一次任务烧掉 8 步），最后向用户道歉。
  用户看到的是「联网搜索坏了」，真实原因是压根没接搜索服务。现在 demo 模式会直接说明未接入。
- **算错的表达式不报错**：早期 `calculate` 用正则把非数字字符过滤掉再 `eval`，
  「3天，共580元」被过滤成 `3580`，直接算出一个错答案且毫无提示。现在归一化不掉的输入一律明确报错，
  求值也换成了 AST 白名单（不允许 `**`，避免 `9**9**9` 把进程算死）。

### 跑后端测试与评测集

```bash
bash scripts/run-tests.sh                              # 全部回归测试
bash scripts/run-tests.sh tests/test_agent_tools.py    # 只跑 Agent 工具
bash scripts/run-tests.sh tests/test_resilience.py     # 只跑韧性单测（不联网，12 个用例）
bash scripts/run-evals.sh                              # 跑评测集（见 server-py/evals/README.md）
```

脚本把 `server-py` 挂进镜像里跑，改完代码不用重新构建；`docker exec workmind-server python /app/tests/xxx.py`
也可以（镜像里已经带了 `tests/`）。

| 测试文件 | 覆盖什么 | 要不要联网 |
|---------|---------|-----------|
| `tests/test_resilience.py` | 韧性状态机：退避与抖动、可重试判定、超时、熔断开/半开/合、取消不算失败、指标 | 否 |
| `tests/test_fallback.py` | **故障注入**：把主模型指向必死地址，验证真的降级到智谱、流式降级、跳闸后不再等超时 | 是（打真实上游） |
| `tests/test_memory.py` / `test_agent_tools.py` / `test_parser_cross_page.py` | 会话记忆、Agent 工具、PDF 跨页解析 | 否 |

评测集（`server-py/evals/`）：RAG 检索、意图判定、回答依据、Agent 工具、ERP 解析共 5 个集，
逐条判定并输出通过率 / 平均与 P95 耗时 / 失败明细（JSON + Markdown 报告）。


## 七、工程化：韧性、缓存与可观测

### 1. 韧性四件套（`server-py/app/services/resilience.py`）

| 能力 | 做什么 | 为什么必须有 |
|------|--------|-------------|
| **超时** | 每次模型/工具调用都有上限（`LLM_TIMEOUT` / `TOOL_TIMEOUT`） | 没有超时的重试不是更可靠，而是把一次抖动放大成雪崩 |
| **指数退避重试 + 抖动** | 只重试超时/连接/429/5xx；等待 `min(MAX, BASE×2^n)` 再叠加随机抖动 | 上游 429/502 隔几百毫秒重试通常就好了；抖动防止一批请求齐步重试（惊群） |
| **熔断** | 连续失败达阈值 → 跳闸，后续请求**不碰上游**直接降级；冷却后半开探测 | 上游真挂了时，重试只会让每个用户都卡满超时（实测跳闸后 4ms vs 超时 2s） |
| **降级** | 主模型（DeepSeek）不可用 → 智谱 GLM 顶上；都不可用则明确报错 | 用户要的是「能回答」，不是「哪个厂商在服务」 |

工具侧同样有这三层，并且**有副作用的工具（发通知）不重试** —— 重试等于重复执行。
工具失败时不会把异常抛给 Agent（那会直接打断整条任务），而是回一句**可行动**的说明：
失败了、失败在哪、现在该换个参数还是改用别的工具。

熔断器按资源名（`model:deepseek-chat` / `tool:web_search`）注册单例，状态可查：

```bash
curl http://localhost:3000/health/resilience
# { counters: {calls, retries, timeouts, fallbacks, breakerRejected...},
#   breakers: [{name, state: closed|open|half_open, totalFailures, recoverInSec, lastError}],
#   config: {...} }
```

### 2. 两级答案缓存（`app/services/cache.py`）

- **L1 进程内**：最热的问答走这里，不跨进程、不序列化；
- **L2 Redis**：跨实例共享 + **重启不丢**（TTL 由 Redis 自己过期）；
- **Redis 不可用自动降级为纯 L1**：缓存是加速手段，不能因为它挂了让业务不可用；
  降级状态在 `health.cache.backend`（`redis+l1` / `memory-only`）里如实标注。

实测（同一句问题，中间重启一次服务）：第一次 MISS → 第二次命中（L1）→ **重启后再问仍然命中（L2）**，
`l2Hits=1`。缓存键带权限隔离域（租户+部门+密级+命中切片），不会把 A 权限的答案发给 B。

### 3. 全链路追踪与用量看板

见前文「用量看板」与「全链路追踪」两节：每次请求一行 run、每个步骤一行 step，
重试/降级/熔断跳闸都会作为 `resilience` 步骤记进同一条链路 —— 排查时不用在日志里对时间。

### 4. 持续集成（GitHub Actions）

两个 workflow，分工的原则是：**每次 push 跑的必须「不花钱、结论确定」；需要真实模型的放手动/定时**。
否则红点会变成噪声，谁都不看。

#### `.github/workflows/ci.yml`（push / PR 触发，不需要任何密钥）

| Job | 做什么 | 挡的是哪类事故 |
|-----|--------|---------------|
| `backend-tests` | 编译所有后端模块 + 跑 4 个离线回归测试（韧性 12 例、记忆 17 例、工具 54 例、解析用例） | 语法/依赖改动把模块改坏；韧性状态机被改错 |
| `frontend-build` | `pnpm install --frozen-lockfile` + `pnpm build` | lockfile 与 package.json 漂移、前端编译不过 |
| `compose-smoke` | 用**假 key** 建镜像并起整栈，核对 `/health/`、`/health/resilience`、前端 200 | Dockerfile/requirements/compose 环境变量写错、新加的服务（如 Redis）漏配 |
| `fault-injection`（可选） | 配了 `ZHIPU_API_KEY` 才跑：把主模型指向必死地址，验证真的降级到智谱 | 降级链路配置写错（base_url/模型名）——单元测试全绿也发现不了 |

这几个 job 的用例都是真实踩过的坑：本次工程化改动里就出现过「requirements 漏了 `redis` 包」
「compose 少了一个服务」「pnpm-lock 与 package.json 不一致」这类问题，
它们不会在本地立刻暴露，但会让「换台机器就起不来」。

#### `.github/workflows/evals.yml`（手动触发 + 每晚 02:00）

起整栈 → 灌公司制度文档（按 sha256 幂等，不重复计费）→ 跑 5 个评测集 69 条 →
把报告写进 Job Summary 并作为 artifact 上传。需要仓库 Secrets 里配 `DEEPSEEK_API_KEY` / `ZHIPU_API_KEY`。

本地等价命令：

```bash
bash scripts/run-tests.sh    # 后端全部回归测试（含故障注入）
bash scripts/run-evals.sh    # 评测集
```



## 八、接口一览

| 路由前缀 | 功能 |
|---------|------|
| `GET /health`、`/health/live` | 健康检查 |
| `/api/chat/*` | 流式对话、会话管理、用户画像、角色预设 |
| `/api/knowledge/*` | 文档上传/列表/删除、RAG 流式问答 |
| `/api/agent/*` | ReAct Agent 任务执行（流式）、工具列表、示例任务 |
| `/api/workflow/*` | 4 个内置工作流（周报/会议纪要/邮件润色/PRD），支持人工审核暂停/恢复 |
| `/api/erp/*` | 自然语言转结构化表单、Multi-Agent 审批流 |
| `/api/prompt/*` | Prompt 单测(流式)、A/B 测试(流式 `POST /api/prompt/ab-test/stream`)、模板管理 |
| `/api/monitor/*` | 用量看板：调用统计、Token、成本、缓存命中率（`GET /stats`、`POST /reset`、`PUT /budget`） |
| `/api/trace/*` | 全链路追踪：`GET /runs`（列表/筛选/搜索）、`GET /runs/{runId}`（完整时间线）、`GET /stats`、`DELETE /runs` |
| `GET /health/resilience` | 韧性可观测快照：熔断器状态、重试/超时/降级计数、缓存后端与命中率 |

用量看板的数据来源是 **每一次模型调用写一行 `usage_calls`**（PostgreSQL），
`GET /api/monitor/stats` 从库里聚合；数据库不可用时降级为进程内统计，
并在 `overview.dataSource` 里如实标注（`postgres` / `memory`）。
记账点：对话（含**缓存命中的 savedTokens**、真实延迟）、知识库（检索向量化 + 重排 + AI 预填 + 入库向量化）、
Agent（每步模型调用累加）、工作流（整条图的 token）、ERP（填单解析 + 每个审批节点）、Prompt 调试。
其中 embedding / bge 重排这类**接口不返回 usage** 的调用按字符估算，记录里标 `estimated=true`，
看板上显示为「估算」，不跟模型返回的真实 token 混为一谈。

#### 计费单价（`server-py/app/services/pricing.py`）

单价不写死在代码注释里，而是照官方价目表逐项实现，并且**按模型 × 缓存命中 × 峰谷时段**实算：

| 项目（元 / 百万 tokens） | deepseek-flash | deepseek-v4-pro |
|--------------------------|----------------|-----------------|
| 输入（缓存命中）空闲      | 0.02           | 0.15            |
| 输入（缓存未命中）空闲    | 1              | 4.5             |
| 输出 空闲                | 4              | 13.5            |

- 高峰时段价格 = 空闲 × 2；高峰为**北京时间**周一至周五 9:00-12:00、14:00-18:00，
  其余（含周末与法定节假日全天）为空闲。
- 模型名映射：`deepseek-flash` / `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` → Flash；
  `deepseek-v4-pro` → Pro；老名字 `deepseek-chat`（本项目默认用的就是它）实际由
  DeepSeek-V4.1-Flash 提供服务，同样按 Flash 计费（实测 response_metadata.model_name 返回 deepseek-flash）。
- **提示缓存**（DeepSeek 的 `usage.input_token_details.cache_read`）单列统计并单独计价 ——
  它单价只有未命中的 1/50，不分出来的话费用会明显偏高。
- 第三方接口（智谱 embedding-3 = 0.5 元/百万；bge 重排无公开单价）另建档位：
  bge 重排默认**按 0 计**并在看板标「未计价」，要计就配 `PRICE_RERANK_PER_M` —— 不编价格。
- 价格来源：<https://api-docs.deepseek.com/zh-cn/quick_start/pricing>（核对时间写在 `pricing.py` 顶部）。
  改价只需改这一个文件；看板顶部会显示**当前生效的档位、时段与单价**，费用可人工核对。

> 历史数据说明：换计价口径之前落库的 `usage_calls` 仍是旧价算出来的，
> 想让今日费用从干净的口径开始，看板上点「重置统计」即可。

### 全链路追踪（`/trace` 页面 / `/api/trace/*`）

用途和开发者盯终端看流程一样，区别是**可回看、可搜索、能对着某一条聊天记录打开**：
一次请求 = 一行 `trace_runs`，一个步骤 = 一行 `trace_steps`（PostgreSQL，默认保留 7 天）。

覆盖模块：对话助手、任务 Agent、RAG 知识库检索、内容工作流、ERP（填单 + 审批链）、Prompt 调试（单测 + A/B）。

记录的步骤：
`request`（收到提问/任务）→ `intent`（为什么查/不查知识库）→ `embedding`（问句向量化）
→ `vector_search`（过了权限过滤还剩多少候选、最高分、取回的 TopK）→ `rerank`（打分 / 跳过原因）
→ `retrieval`（命中几条、为什么没命中）→ `prompt`（组装后的 system prompt）
→ `cache`（命中 / 未命中）→ `route`（Agent 走哪条分支、为什么）→ `llm`（每次模型调用的耗时与产出）
→ `tool_call` / `tool_result`（**工具入参与返回**）→ `node`（工作流节点进出）
→ `response` / `error`（异常留档）。

实现（`app/services/trace.py`）：
- 用 **ContextVar** 传递追踪上下文（和 `current_user` 同一路子），
  所以调用链深处（意图 / 检索 / 重排 / 工具）直接 `trace_step(...)` 即可，不用改函数签名；
- 步骤先缓冲在内存，`finish()` 时**一个事务**写 run + 全部 step，不拖慢流式请求；
- 字符串与单步 detail 都有上限截断，避免一条 trace 把库撑爆；
- 没有追踪上下文时全部是空操作（脚本 / 测试无感）。

入口：对话里每条 AI 消息、Agent 每张任务卡片的「最终回答」旁都有 🔗**全链路**按钮；
也支持深链 `/trace?run=<runId>`，方便把某一次执行直接发给别人看。

⚠️ 与 `/api/admin/*` 一样，这是开发/运维接口：**上生产必须加鉴权**。

所有 SSE 流式接口的事件格式均为：

```
event: <type>
data: <json>

```

错误统一返回：

```json
{ "error": { "code": "...", "message": "...", "retryable": false } }
```

## 九、常见问题

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
