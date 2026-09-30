# server-py/app/core/identity.py
"""
身份与可见性规则 —— 权限过滤的唯一裁决处。

设计取舍：
- 演示阶段不做登录体系（那会和另一个项目的 admin 登录重复），改成
  「前端带身份头 + 后端强制校验」：所有查询都在服务端用 user 过滤，
  前端传什么都改变不了越权结果（因为过滤发生在检索层，不在返回层）。
- 默认身份是 anonymous：只能看 security_level=public 的文档，
  即"安全默认"（fail-safe），漏传身份头不会导致越权。
- 换成真登录时，只需把 user_from_headers 换成"从 JWT 解析"，其余代码不动。
"""
from contextvars import ContextVar
from urllib.parse import unquote

from fastapi import Request
from pydantic import BaseModel, Field

# 密级排序：高密级可见低密级，反之不行
_CLEARANCE_ORDER = {"public": 0, "internal": 1, "confidential": 2}


class User(BaseModel):
    tenant_id: str = "tenant-demo"
    user_id: str = "anonymous"
    name: str = "匿名用户"
    departments: list[str] = Field(default_factory=lambda: ["general"])
    clearance: str = "public"

    @property
    def department_labels(self) -> str:
        return ",".join(self.departments)


def _split(v: str | None) -> list[str]:
    if not v:
        return []
    return [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]


def user_from_headers(headers) -> User:
    """从请求头解析当前身份；缺省为最小权限身份。

    姓名做 URL 解码：HTTP 头只能是 latin-1，前端传中文姓名时必须 encodeURIComponent，
    否则会在入库时把 "%E6%9D%8E%E9%9B%B7" 这种编码串写进 created_by_name，
    详情页就会显示成乱码（真实发生过）。
    """
    departments = _split(headers.get("x-user-departments"))
    return User(
        tenant_id=headers.get("x-tenant-id") or "tenant-demo",
        user_id=headers.get("x-user-id") or "anonymous",
        name=unquote(headers.get("x-user-name") or "") or "匿名用户",
        departments=departments or ["general"],
        clearance=(headers.get("x-user-clearance") or "public").lower(),
    )


async def current_user(request: Request) -> User:
    """FastAPI 依赖：路由里声明 user: User = Depends(current_user)。"""
    user = user_from_headers(request.headers)
    # 放到 request.state，日志/审计可用
    request.state.user = user
    return user


# ── 上下文传递 ────────────────────────────────────────────────────────
# Agent 的工具（如 read_doc）在调用链深处执行，拿不到 FastAPI 的请求对象。
# 用 ContextVar 把"当前用户"传下去：asyncio 任务会复制上下文，天然并发安全，
# 比把 user 塞进全局变量或工具闭包更可靠（也不会串号）。
_current_user: ContextVar[User | None] = ContextVar("workmind_current_user", default=None)


def set_current_user(user: User):
    return _current_user.set(user)


def reset_current_user(token) -> None:
    try:
        _current_user.reset(token)
    except ValueError:
        pass


def get_current_user() -> User:
    """调用链深处获取当前身份；缺省为最小权限身份（安全默认）。"""
    return _current_user.get() or User()


# ── 可见性裁决 ────────────────────────────────────────────────────────
def can_view(doc, user: User) -> bool:
    """文档级权限：租户隔离 → 公开 → 部门 → 密级。"""
    if doc.tenant_id != user.tenant_id:
        return False  # 多租户隔离，优先级最高
    if doc.security_level == "public":
        return True
    # 部门匹配（general 表示全员通用文档）
    if doc.department != "general" and doc.department not in user.departments:
        return False
    # 密级匹配
    need = _CLEARANCE_ORDER.get(doc.security_level, 1)
    have = _CLEARANCE_ORDER.get(user.clearance, 0)
    return have >= need


# ── SQL 版本：把"谁能看 / 看哪一版"下推到数据库 WHERE ────────────────────
# 检索必须"先过滤再算相似度"：把权限/版本条件写进 SQL 的 WHERE，
# 数据库在算余弦距离之前就把不可见的切片剔掉 ——
# 既保证越权内容不进候选池（安全），也避免"TopK 被过滤完导致无结果"（召回质量）。
#
# ⚠️ 这里是 can_view / version_visible 的 SQL 同义实现，两处必须保持一致。
#    Python 侧依旧是最终裁决（query.py 命中后还会再走一遍 can_view），
#    SQL 只是把过滤下推做性能优化 —— 安全底线不依赖单点。
_CLEARANCE_RANK = {"public": 0, "internal": 1, "confidential": 2}


class SqlParams:
    """$1/$2/... 占位符生成器：拼 SQL 时不必手写序号，避免错位。"""

    def __init__(self) -> None:
        self.values: list = []

    def add(self, value) -> str:
        self.values.append(value)
        return "$" + str(len(self.values))


def _clearance_rank_sql(col: str) -> str:
    return (f"CASE {col} WHEN 'public' THEN 0 WHEN 'internal' THEN 1 "
            f"WHEN 'confidential' THEN 2 ELSE 1 END")


def doc_visibility_sql(user: User, *, department: str | None = None,
                       version: str | None = None, doc_type: str | None = None,
                       doc_ids: list[str] | None = None,
                       include_superseded: bool = False,
                       alias: str = "d", chunk_alias: str = "c",
                       params: SqlParams | None = None) -> tuple[str, list]:
    """返回 (where_sql, params)，可直接拼进 chunks JOIN documents 的查询。"""
    p = params or SqlParams()
    conds: list[str] = [f"{chunk_alias}.tenant_id = {p.add(user.tenant_id)}",
                        f"{alias}.tenant_id = {p.add(user.tenant_id)}"]

    # 公开 → 直接可见；否则要求「全员通用 或 本部门」且密级足够
    depts = p.add(list(user.departments or ["general"]))
    rank = p.add(_CLEARANCE_RANK.get(user.clearance, 0))
    conds.append(
        f"({alias}.security_level = 'public' OR ("
        f"({alias}.department = 'general' OR {alias}.department = ANY({depts}::text[])) "
        f"AND {_clearance_rank_sql(alias + '.security_level')} <= {rank}))"
    )

    # 显式筛选条件（用户在前端选的部门/类型/版本，或指定文档范围）
    if doc_ids:
        conds.append(f"{chunk_alias}.doc_id = ANY({p.add(list(doc_ids))}::text[])")
    if department:
        conds.append(f"{alias}.department = {p.add(department)}")
    if doc_type:
        conds.append(f"{alias}.doc_type = {p.add(doc_type)}")
    if version:
        conds.append(f"{alias}.version = {p.add(version)}")

    # 版本/时效：默认只命中"当前生效版本"
    if not include_superseded:
        conds.append(f"{alias}.status = 'active'")
        conds.append(f"({alias}.effective_date IS NULL OR {alias}.effective_date <= CURRENT_DATE)")
        conds.append(f"({alias}.expired_date IS NULL OR {alias}.expired_date >= CURRENT_DATE)")

    conds.append(f"{chunk_alias}.embedding IS NOT NULL")
    return " AND ".join(conds), p.values


def version_visible(doc, *, version: str | None = None, include_superseded: bool = False,
                    as_of: str | None = None) -> bool:
    """版本/时效过滤：默认只命中"当前生效版本"。

    - 指定 version 时按版本精确匹配（回溯历史版本用）
    - 默认排除 superseded/archived，并校验生效/失效日期，避免新旧版本同时被检索到
    """
    if version and doc.version != version:
        return False
    if include_superseded:
        return True
    if doc.status != "active":
        return False
    today = as_of or date_today()
    if doc.effective_date and doc.effective_date > today:
        return False  # 尚未生效
    if doc.expired_date and doc.expired_date < today:
        return False  # 已失效
    return True


def date_today() -> str:
    from datetime import date
    return date.today().isoformat()
