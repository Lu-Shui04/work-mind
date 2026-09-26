# server-py/app/schemas/document.py
"""
文档与切片元数据模型 —— RAG 权限过滤 / 版本过滤 / 引用溯源的基础。

字段分三层：
1) DocumentRecord  文档级：一次上传 = 一份文档，含租户、来源文件、解析溯源、权限与版本属性
2) ChunkRecord     切片级：送去 embedding 的最小单元，携带「过滤字段快照」+「定位信息」
3) DocumentMetadata 用户输入：上传时必须由人确认的字段（AI 可预填，但必须人工确认）

为什么这些字段必须「入库时就采集」：
- tenant_id / department / security_level  → 决定"谁能看"（权限下推到检索层，而不是检索完再过滤）
- version / effective_date / status / superseded_by → 决定"该看哪一版"（版本过滤 + 自动失效）
- page_number / element_type / order_index → 决定"引用能不能定位回原文"（溯源、高亮、页码引用）
- parser_name / parse_time_ms / language   → 解析质量可观测；换解析器后可用同一批文档做回归对比

这些信息一旦入库时缺失，事后无法从正文可靠回填（比如"这段文字属于哪个部门"），
所以宁可入库时强制采集、多存几个字段。
"""
from datetime import date
from enum import Enum

from pydantic import BaseModel, Field, field_validator


# ── 枚举字典（前端下拉选项直接由 /api/knowledge/options 提供）──────────
class Department(str, Enum):
    general = "general"      # 全员通用（不受部门限制）
    hr = "hr"
    tech = "tech"
    finance = "finance"
    legal = "legal"
    product = "product"
    sales = "sales"


class DocType(str, Enum):
    policy = "policy"        # 制度 / 规定
    manual = "manual"        # 手册 / 指南
    spec = "spec"            # 规范 / 标准
    report = "report"        # 报告
    contract = "contract"    # 合同
    other = "other"


class SecurityLevel(str, Enum):
    public = "public"              # 全员可见
    internal = "internal"          # 本部门可见
    confidential = "confidential"  # 本部门 + 需要同等密级授权


class DocStatus(str, Enum):
    active = "active"              # 当前生效版本
    superseded = "superseded"      # 已被新版本替代
    archived = "archived"          # 人工归档（不再参与检索）


class IngestStatus(str, Enum):
    """入库状态机：同步执行时也会逐阶段写回，前端可直接展示进度。
    后续换成异步 worker 时，前端无需改动。"""
    pending = "pending"
    parsing = "parsing"
    chunking = "chunking"
    embedding = "embedding"
    indexed = "indexed"
    failed = "failed"


class ElementType(str, Enum):
    """解析出的结构单元类型 —— 标题/表格单独成块，是检索质量的关键。"""
    title = "title"
    paragraph = "paragraph"
    list = "list"
    table = "table"
    code = "code"
    other = "other"


# ── 用户输入（上传表单）────────────────────────────────────────────────
class DocumentMetadata(BaseModel):
    """上传时必须由人确认的字段；AI 只能"预填建议"，最终值以本模型为准。"""
    document_title: str = Field(min_length=1, max_length=200, description="文档标题")
    department: Department = Field(description="归属部门（权限过滤主键）")
    version: str = Field(description="版本号，如 2026-08 / v1.2")
    effective_date: str = Field(description="生效日期 YYYY-MM-DD（版本时效过滤）")
    doc_type: DocType = Field(description="文档类型")
    security_level: SecurityLevel = Field(default=SecurityLevel.internal, description="密级")
    expired_date: str | None = Field(default=None, description="失效日期，可空")
    owner: str | None = Field(default=None, max_length=50, description="责任人/归属人")
    tags: list[str] = Field(default_factory=list, description="标签，便于范围过滤")
    remark: str | None = Field(default=None, max_length=200, description="备注")

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        v = (v or "").strip()
        import re
        if not re.fullmatch(r"(\d{4}-\d{2}|v?\d+(\.\d+)*)", v):
            raise ValueError("版本号格式应为 2026-08 或 v1.2")
        return v

    @field_validator("effective_date", "expired_date")
    @classmethod
    def _check_date(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"日期格式应为 YYYY-MM-DD，收到：{v}")
        return v

    @field_validator("tags", mode="before")
    @classmethod
    def _check_tags(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            v = [t.strip() for t in v.replace("，", ",").split(",")]
        return [t for t in v if t][:10]


# ── 解析产物：结构单元 ─────────────────────────────────────────────────
class ParsedElement(BaseModel):
    """解析器输出的最小结构单元（段落/标题/表格…），切片以它为原子单位。

    page_number / page_end 是"页码区间"：跨页续写的段落或跨页表格会被合并成同一个元素，
    此时 page_number=起始页、page_end=结束页，引用可以标注成"第 3-4 页"。

    heading_path 是**章节路径**（如 ["第一类：岗位认知", "Q1：什么是AI产品经理？"]）：
    - 切块时按它分组，同一节的内容不会被切散、标题也不会单独成块
    - 检索时注入到切片正文开头 —— "这段属于哪一章哪一节"本身就是很强的召回信号
    - 引用时能告诉用户"答案出自哪一章哪一节"，而不是只给一个页码
    """
    order_index: int
    page_number: int | None = None
    page_end: int | None = None
    element_type: ElementType = ElementType.paragraph
    text: str
    heading_path: list[str] = Field(default_factory=list)


class ParsedDocument(BaseModel):
    elements: list[ParsedElement] = Field(default_factory=list)
    page_count: int = 0
    language: str = "unknown"
    parser_name: str = ""
    parse_time_ms: float = 0.0
    char_count: int = 0


# ── 文档级记录（系统持久化对象）────────────────────────────────────────
class DocumentRecord(BaseModel):
    # 主键与租户
    doc_id: str
    tenant_id: str
    # 用户输入字段
    document_title: str
    department: str
    version: str
    effective_date: str
    expired_date: str | None = None
    doc_type: str
    security_level: str
    owner: str | None = None
    tags: list[str] = Field(default_factory=list)
    remark: str | None = None
    # 来源文件
    file_name: str
    file_type: str
    file_size: int
    source_path: str
    file_sha256: str
    # 解析溯源
    page_count: int = 0
    language: str = "unknown"
    parser_name: str = ""
    parse_time_ms: float = 0.0
    element_count: int = 0
    # 生产结果
    chunk_count: int = 0
    char_count: int = 0
    # 切片参数（入库时记录：目标长度/硬上限/重叠长度）—— 便于复现与回溯
    chunk_target: int = 0
    chunk_max: int = 0
    chunk_overlap: int = 0
    ingest_status: str = IngestStatus.pending.value
    ingest_error: str | None = None
    ingest_time_ms: float = 0.0
    created_by: str = ""
    created_by_name: str = ""
    created_at: str = ""
    indexed_at: str | None = None
    # 版本治理
    status: str = DocStatus.active.value
    superseded_by: str | None = None
    preview: str = ""


# ── 切片级记录（同时作为向量库 metadata 的快照）────────────────────────
class ChunkRecord(BaseModel):
    chunk_id: str
    doc_id: str
    tenant_id: str
    order_index: int
    # 下推到检索层的过滤字段（快照）
    department: str
    version: str
    doc_type: str
    security_level: str
    # 引用溯源定位（page_end != page_number 表示这个切片跨页）
    page_number: int | None = None
    page_end: int | None = None
    element_type: str = ElementType.paragraph.value
    element_count: int = 1
    # 章节路径（引用可回溯到"哪一章哪一节"，而不是只有一个页码）
    heading_path: list[str] = Field(default_factory=list)
    # 内容
    text: str
    char_count: int = 0
    embedding: list[float] = Field(default_factory=list)
