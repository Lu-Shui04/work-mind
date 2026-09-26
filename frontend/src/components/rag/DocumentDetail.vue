<template>
  <!-- frontend/src/components/rag/DocumentDetail.vue
       文档详情：完整元数据 + 切片列表 + 版本链 + 检索验证。
       "检索验证"不是聊天，而是证明「权限 / 版本 / 范围过滤」真的生效的工程工具。 -->
  <div class="detail" v-if="doc">
    <div class="head">
      <div class="head-main">
        <div class="title-row">
          <h3>{{ doc.document_title }}</h3>
          <span class="status" :class="doc.status">{{ statusLabel(doc.status) }}</span>
          <span class="level" :class="doc.security_level">{{ levelLabel(doc.security_level) }}</span>
        </div>
        <div class="sub">
          {{ deptLabel(doc.department) }} · {{ typeLabel(doc.doc_type) }} · {{ doc.version }} ·
          生效 {{ doc.effective_date }}<span v-if="doc.expired_date"> ~ {{ doc.expired_date }}</span> ·
          {{ doc.chunk_count }} 切片 / {{ doc.element_count }} 结构单元 / {{ doc.page_count }} 页
        </div>
      </div>
      <div class="head-ops">
        <button class="ghost" @click="knStore.downloadSource(doc.doc_id)">下载源文件</button>
        <button class="ghost" @click="knStore.reindexDocument(doc.doc_id)">重新入库</button>
        <button class="ghost danger" @click="remove">删除</button>
      </div>
    </div>

    <div class="tabs">
      <button v-for="t in tabs" :key="t.id" class="tab" :class="{ active: tab === t.id }" @click="tab = t.id">
        {{ t.label }}<span v-if="t.count !== undefined" class="count">{{ t.count }}</span>
      </button>
    </div>

    <!-- 元数据 -->
    <div v-if="tab === 'meta'" class="tab-body">
      <section v-for="group in metaGroups" :key="group.name" class="group">
        <div class="group-title">{{ group.name }}</div>
        <div class="kv-grid">
          <div v-for="row in group.rows" :key="row.k" class="kv">
            <span class="k">{{ row.k }}</span>
            <span class="v" :class="{ mono: row.mono, warn: row.warn }" :title="row.title || String(row.v ?? '')">{{ row.v ?? '—' }}</span>
          </div>
        </div>
      </section>
    </div>

    <!-- 切片 -->
    <div v-if="tab === 'chunks'" class="tab-body">
      <div class="chunk-stat">
        <span v-for="(n, t) in detail.elementTypeStats" :key="t" class="chip">{{ elementLabel(t) }} {{ n }}</span>
        <span class="chip">共 {{ detail.chunkTotal }} 片，显示前 {{ detail.chunks.length }} 片</span>
      </div>
      <div class="chunk-list">
        <div v-for="c in detail.chunks" :key="c.chunk_id" class="chunk">
          <div class="chunk-head">
            <span class="idx">#{{ c.order_index }}</span>
            <span class="etype" :class="c.element_type">{{ elementLabel(c.element_type) }}</span>
            <span class="loc">{{ pageLabel(c) }}</span>
            <span class="chars">{{ c.char_count }} 字</span>
            <span v-if="c.element_count > 1" class="chars">合并 {{ c.element_count }} 个单元</span>
            <!-- 章节路径：切块是按标题树分的，这里显示它属于哪一节 -->
            <span v-if="c.heading_path && c.heading_path.length" class="hpath" :title="c.heading_path.join(' › ')">
              {{ c.heading_path.join(' › ') }}
            </span>
          </div>
          <div class="chunk-text">{{ expanded[c.chunk_id] ? c.text : c.text.slice(0, 160) + (c.text.length > 160 ? ' …' : '') }}</div>
          <button v-if="c.text.length > 160" class="more" @click="expanded[c.chunk_id] = !expanded[c.chunk_id]">
            {{ expanded[c.chunk_id] ? '收起' : '展开全文' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 版本链 -->
    <div v-if="tab === 'versions'" class="tab-body">
      <div class="version-item current">
        <span class="vtag">当前</span>
        <span class="vt">{{ doc.version }}</span>
        <span class="vs">{{ statusLabel(doc.status) }}</span>
        <span class="vd">生效 {{ doc.effective_date }}</span>
        <span class="vd">{{ doc.created_at?.slice(0, 10) }} 由 {{ doc.created_by_name || doc.created_by }} 上传</span>
      </div>
      <div v-for="v in detail.versionChain" :key="v.docId" class="version-item" @click="knStore.selectDocument(v.docId)">
        <span class="vtag" :class="v.status">{{ statusLabel(v.status) }}</span>
        <span class="vt">{{ v.version }}</span>
        <span class="vd">生效 {{ v.effectiveDate }}</span>
        <span class="vd">{{ v.createdAt?.slice(0, 10) }}</span>
        <span class="link">查看 →</span>
      </div>
      <div v-if="!detail.versionChain.length" class="empty">该文档没有其他版本</div>
      <div class="tip">上传新版本后，旧版本会自动变为「已被替代」，默认不再参与检索（可用检索条件 includeSuperseded 回溯）。</div>
    </div>

    <!-- 检索验证 -->
    <div v-if="tab === 'search'" class="tab-body">
      <div class="search-row">
        <input v-model="question" class="search-input" placeholder="输入一个问题，验证当前身份能检索到什么"
               @keydown.enter="run" />
        <button class="ghost primary" :disabled="!question.trim() || sp.loading" @click="run">
          {{ sp.loading ? '检索中...' : '检索' }}
        </button>
      </div>
      <div v-if="sp.error" class="err">{{ sp.error }}</div>
      <div v-if="sp.diagnostics" class="diag">
        <span>库内切片 {{ sp.diagnostics.totalChunks }}</span>
        <span>通过过滤 {{ sp.diagnostics.candidates }}</span>
        <span v-if="sp.diagnostics.bestScore != null">最高分 {{ sp.diagnostics.bestScore }}</span>
        <span>阈值 {{ sp.diagnostics.similarityThreshold }}</span>
        <span>耗时 {{ sp.diagnostics.elapsedMs }}ms</span>
        <span>身份 {{ sp.diagnostics.user.userId }}（{{ sp.diagnostics.user.departments.join('/') }} · {{ sp.diagnostics.user.clearance }}）</span>
        <span>过滤 {{ filterDesc }}</span>
        <span v-if="sp.diagnostics.storage">存储 {{ sp.diagnostics.storage.backend }}</span>
      </div>
      <!-- 未命中时把原因摆出来，不用再靠翻代码判断是"没入库"还是"阈值太高" -->
      <div v-if="sp.diagnostics && !sp.hits.length && sp.diagnostics.explain" class="diag-reason">
        {{ reasonLabel }}：{{ sp.diagnostics.explain }}
      </div>
      <div v-for="(h, i) in sp.hits" :key="h.chunkId" class="hit">
        <div class="hit-head">
          <span class="score">{{ h.score }}</span>
          <span class="hit-title">{{ h.title }}</span>
          <span class="loc">{{ h.pageLabel || (h.pageNumber ? '第' + h.pageNumber + '页' : '无页码') }}</span>
          <span class="etype" :class="h.elementType">{{ elementLabel(h.elementType) }}</span>
          <span class="hit-tag">{{ deptLabel(h.department) }} · {{ h.version }}</span>
          <button class="more" @click="knStore.selectDocument(h.docId)">打开文档</button>
        </div>
        <div class="hit-text">{{ h.content.slice(0, 220) }}{{ h.content.length > 220 ? ' …' : '' }}</div>
      </div>
      <div v-if="!sp.loading && sp.question && !sp.hits.length && !sp.error" class="empty">
        没有命中任何切片 —— 原因见上方诊断（库空 / 被权限过滤 / 分数低于阈值，处理方式完全不同）
      </div>
    </div>
  </div>

  <div v-else class="empty-detail">
    <div class="empty-icon">📚</div>
    <div class="empty-title">选择一篇文档查看元数据与切片</div>
    <div class="empty-desc">左侧可上传、筛选、删除；这里能看到入库后的全部元数据与解析结果</div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch } from 'vue'
import { useKnowledgeStore } from '@/stores/knowledge.js'
import { useAppStore } from '@/stores/app.js'

const knStore = useKnowledgeStore()
const appStore = useAppStore()

const tab = ref('meta')
const question = ref('')
const expanded = reactive({})
const sp = knStore.searchPreview

// 未命中原因的文案：三类原因的处理方式完全不同（去入库 / 调权限 / 调阈值）
const REASON_LABELS = {
  kb_empty: '知识库为空',
  all_filtered: '候选被权限或版本条件过滤',
  below_threshold: '相似度低于阈值',
  storage_unavailable: '数据库不可用',
  embedding_unavailable: 'embedding 不可用',
}
const reasonLabel = computed(() => REASON_LABELS[sp.diagnostics?.reason] || '未命中')

const doc = computed(() => knStore.detail?.document || null)
const detail = computed(() => knStore.detail || { chunks: [], elementTypeStats: {}, chunkTotal: 0, versionChain: [] })

const tabs = computed(() => ([
  { id: 'meta', label: '元数据' },
  { id: 'chunks', label: '切片', count: knStore.detail?.chunkTotal },
  { id: 'versions', label: '版本链', count: knStore.detail?.versionChain?.length },
  { id: 'search', label: '检索验证' },
]))

const DEPT = { general: '全员通用', hr: '人力资源', tech: '技术研发', finance: '财务', legal: '法务合规', product: '产品设计', sales: '市场销售' }
// 租户：给人类看的名字（后端存的是内部 id，不该直接甩给用户）
const TENANT = { 'tenant-demo': '演示企业' }
const LANG = { zh: '中文', en: '英文', mixed: '中英混合', unknown: '未识别' }
const INGEST = {
  pending: '排队中', parsing: '解析中', chunking: '切分中',
  embedding: '向量化中', indexed: '已完成', failed: '失败',
}
const PARSER = {
  'markdown-heading-tree-v3': 'Markdown 标题树（v3）',
  'text-structure-v2': '纯文本结构（v2）',
  'pypdf+structure-v2': 'PDF 文本抽取（v2）',
  'text-structure-v1': '纯文本结构（v1）',
  'markdown-structure-v2': 'Markdown 结构（v2，已废弃）',
}
const tenantLabel = (v) => TENANT[v] || v || '—'
const langLabel = (v) => LANG[v] || v || '—'
const ingestLabel = (v) => INGEST[v] || v || '—'
const parserLabel = (v) => PARSER[v] || v || '—'
// 后端存的是 UTC ISO，直接 slice 会显示成"半夜 4 点"，这里转成本地时区
function fmtTime(iso) {
  if (!iso) return null
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return iso.replace('T', ' ').slice(0, 19)
  const p = (n) => String(n).padStart(2, '0')
  return t.getFullYear() + '-' + p(t.getMonth() + 1) + '-' + p(t.getDate())
    + ' ' + p(t.getHours()) + ':' + p(t.getMinutes()) + ':' + p(t.getSeconds())
}
const TYPE = { policy: '制度规定', manual: '手册指南', spec: '规范标准', report: '报告', contract: '合同', other: '其他' }
const LEVEL = { public: '公开', internal: '内部', confidential: '机密' }
const STATUS = { active: '生效中', superseded: '已被替代', archived: '已归档' }
const ELEMENT = { title: '标题', paragraph: '段落', list: '列表', table: '表格', code: '代码', other: '其他' }
const deptLabel = (v) => DEPT[v] || v
const typeLabel = (v) => TYPE[v] || v
const levelLabel = (v) => LEVEL[v] || v
const statusLabel = (v) => STATUS[v] || v
const elementLabel = (v) => ELEMENT[v] || v
// 跨页切片显示"第3-4页"，单页显示"第3页"
// 把"部门 × 密级"翻译成人话，避免"以为没填就不会全员可见"
function visibilityText(d) {
  const dept = deptLabel(d.department)
  if (d.security_level === 'public') return '全员可见（密级=公开）'
  if (d.security_level === 'confidential') return `仅「${dept}」且持机密授权的身份可见`
  if (d.department === 'general') return '全员可见（部门=全员通用 + 密级=内部）'
  return `仅「${dept}」成员可见（密级=内部）`
}

const pageLabel = (c) => (c.page_end && c.page_end !== c.page_number)
  ? `第 ${c.page_number}-${c.page_end} 页`
  : (c.page_number ? `第 ${c.page_number} 页` : '无页码')

function fmtSize(b) {
  if (!b) return '—'
  if (b < 1024) return b + ' B'
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB'
  return (b / 1048576).toFixed(1) + ' MB'
}

const metaGroups = computed(() => {
  const d = doc.value
  if (!d) return []
  return [
    {
      name: '权限与版本',
      rows: [
        { k: '可见范围', v: visibilityText(d) },
        { k: '有效期', v: d.expired_date ? `${d.effective_date} ~ ${d.expired_date}` : `自 ${d.effective_date} 起长期有效` },
        { k: '归属部门', v: deptLabel(d.department) },
        { k: '密级', v: levelLabel(d.security_level) },
        { k: '文档类型', v: typeLabel(d.doc_type) },
        { k: '版本号', v: d.version },
        { k: '文档状态', v: statusLabel(d.status) },
        { k: '生效日期', v: d.effective_date },
        { k: '失效日期', v: d.expired_date || '无' },
        { k: '被替代为', v: d.superseded_by ? '已被更新版本替代' : '—' },
        { k: '标签', v: (d.tags || []).join('、') },
        { k: '责任人', v: d.owner },
        { k: '备注', v: d.remark },
        { k: '所属企业', v: tenantLabel(d.tenant_id) },
      ],
    },
    {
      name: '来源文件',
      rows: [
        { k: '文件名', v: d.file_name },
        { k: '文件类型', v: d.file_type, mono: true },
        { k: '文件大小', v: fmtSize(d.file_size) },
        { k: '服务器文件', v: (d.source_path || '').split(/[\\/]/).pop() || '—', mono: true },
        { k: '内容指纹', v: (d.file_sha256 || '').slice(0, 12) + '…', mono: true, title: d.file_sha256 },
      ],
    },
    {
      name: '解析与切块',
      rows: [
        { k: '解析方式', v: parserLabel(d.parser_name) },
        { k: '页数', v: d.page_count },
        { k: '结构单元', v: d.element_count },
        { k: '切成切片', v: d.chunk_count ? `${d.chunk_count} 片（平均 ${Math.round(d.char_count / d.chunk_count)} 字/片）` : '—' },
        { k: '正文字符数', v: d.char_count },
        { k: '语言', v: langLabel(d.language) },
        { k: '切块参数', v: `目标 ${d.chunk_target || '—'} 字 / 上限 ${d.chunk_max || '—'} 字 / 重叠 ${d.chunk_overlap ?? '—'} 字` },
        { k: '入库状态', v: ingestLabel(d.ingest_status), warn: d.ingest_status === 'failed' },
        { k: '解析耗时', v: d.parse_time_ms + ' ms' },
        { k: '入库总耗时', v: d.ingest_time_ms + ' ms' },
        { k: '错误信息', v: d.ingest_error, warn: !!d.ingest_error },
      ],
    },
    {
      name: '来源与时间',
      rows: [
        { k: '上传人', v: d.created_by_name || d.created_by },
        { k: '上传时间', v: fmtTime(d.created_at) },
        { k: '索引完成', v: fmtTime(d.indexed_at) },
        { k: '文档 ID', v: d.doc_id, mono: true },
      ],
    },
  ]
})

const filterDesc = computed(() => {
  const f = sp.appliedFilters
  if (!f) return '—'
  const parts = []
  if (f.department) parts.push('部门=' + deptLabel(f.department))
  if (f.version) parts.push('版本=' + f.version)
  if (f.docType) parts.push('类型=' + typeLabel(f.docType))
  parts.push(f.includeSuperseded ? '含历史版本' : '仅当前版本')
  return parts.join(' / ')
})

function run() {
  knStore.runSearch(question.value, { includeSuperseded: false })
}

async function remove() {
  if (!confirm('确认删除《' + doc.value.document_title + '》？')) return
  try { await knStore.deleteDocument(doc.value.doc_id) }
  catch (err) { appStore.toast.error(err?.response?.data?.error?.message || '删除失败') }
}

// 切换文档时重置检索验证结果与展开状态
watch(() => knStore.selectedId, () => {
  Object.keys(expanded).forEach(k => delete expanded[k])
  sp.hits = []; sp.diagnostics = null; sp.appliedFilters = null; sp.error = ''; sp.question = ''
})
</script>

<style scoped>
.detail { display: flex; flex-direction: column; gap: 12px; height: 100%; }
.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.title-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.title-row h3 { margin: 0; font-size: 15px; color: var(--color-text); }
.sub { font-size: 11.5px; color: var(--color-text-muted); margin-top: 4px; }
.head-ops { display: flex; gap: 6px; flex-shrink: 0; }
.ghost {
  border: 1px solid var(--color-border); background: var(--color-surface); cursor: pointer;
  border-radius: var(--radius-sm); padding: 4px 9px; font-size: 11.5px; color: var(--color-text-sub);
}
.ghost:hover { border-color: var(--color-primary); color: var(--color-primary); }
.ghost.danger:hover { border-color: var(--color-danger); color: var(--color-danger); }
.ghost.primary { background: var(--color-primary); color: #fff; border-color: var(--color-primary); }

.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--color-border); }
.tab {
  border: none; background: none; cursor: pointer; padding: 6px 12px; font-size: 12.5px;
  color: var(--color-text-sub); border-bottom: 2px solid transparent;
}
.tab.active { color: var(--color-primary); border-bottom-color: var(--color-primary); font-weight: 600; }
.count { font-size: 10px; margin-left: 4px; color: var(--color-text-muted); }

.tab-body { overflow: auto; flex: 1; }
.group { margin-bottom: 14px; }
.group-title { font-size: 11.5px; font-weight: 600; color: var(--color-primary-dark); margin-bottom: 6px; }
.kv-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 6px 14px; }
.kv { display: flex; gap: 8px; font-size: 11.5px; border-bottom: 1px dashed var(--color-border-light); padding-bottom: 4px; }
.k { color: var(--color-text-muted); min-width: 78px; flex-shrink: 0; }
.v { color: var(--color-text); word-break: break-all; }
.v.mono { font-family: var(--font-mono); font-size: 11px; }
.v.warn { color: var(--color-danger); }

.chunk-stat { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.chip { font-size: 10.5px; background: var(--color-border-light); color: var(--color-text-sub); padding: 2px 8px; border-radius: var(--radius-full); }
.chunk-list { display: flex; flex-direction: column; gap: 8px; }
.chunk { border: 1px solid var(--color-border-light); border-radius: var(--radius-md); padding: 8px 10px; }
.chunk-head { display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--color-text-muted); margin-bottom: 5px; }
.idx { font-family: var(--font-mono); color: var(--color-primary); }
.hpath {
  color: var(--color-primary-dark); background: var(--color-primary-bg);
  border-radius: var(--radius-full); padding: 0 6px;
  max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.chunk-text { font-size: 12px; color: var(--color-text); white-space: pre-wrap; line-height: 1.6; }
.etype { border-radius: var(--radius-full); padding: 1px 7px; font-size: 10.5px; background: var(--color-border-light); color: var(--color-text-sub); }
.etype.title { background: #e0e7ff; color: #4338ca; }
.etype.table { background: #dcfce7; color: #15803d; }
.etype.list { background: #fef3c7; color: #b45309; }
.etype.code { background: #ede9fe; color: #6d28d9; }
.more { border: none; background: none; color: var(--color-primary); cursor: pointer; font-size: 11px; padding: 2px 0; }

.version-item {
  display: flex; align-items: center; gap: 10px; padding: 8px 10px; margin-bottom: 6px;
  border: 1px solid var(--color-border-light); border-radius: var(--radius-md); font-size: 11.5px;
}
.version-item.current { border-color: var(--color-primary); background: var(--color-primary-bg); }
.vtag { font-size: 10.5px; padding: 1px 7px; border-radius: var(--radius-full); background: var(--color-border-light); color: var(--color-text-sub); }
.vtag.superseded { background: #fef3c7; color: #b45309; }
.vt { font-family: var(--font-mono); font-weight: 600; }
.vs { color: var(--color-text-sub); }
.vd { color: var(--color-text-muted); }
.link { margin-left: auto; color: var(--color-primary); cursor: pointer; }
.tip { font-size: 11px; color: var(--color-text-muted); margin-top: 8px; line-height: 1.6; }

.search-row { display: flex; gap: 8px; margin-bottom: 10px; }
.search-input {
  flex: 1; border: 1px solid var(--color-border); border-radius: var(--radius-sm);
  padding: 6px 9px; font-size: 12px; outline: none; background: var(--color-surface); color: var(--color-text);
}
.search-input:focus { border-color: var(--color-primary); }
.diag {
  display: flex; flex-wrap: wrap; gap: 10px; font-size: 11px; color: var(--color-text-muted);
  background: var(--color-bg); border-radius: var(--radius-sm); padding: 6px 9px; margin-bottom: 10px;
}
.diag-reason {
  font-size: 11.5px; line-height: 1.6; color: #b45309;
  background: #fffbeb; border: 1px solid #fde68a; border-radius: var(--radius-sm);
  padding: 6px 9px; margin-bottom: 10px;
}
.hit { border: 1px solid var(--color-border-light); border-radius: var(--radius-md); padding: 8px 10px; margin-bottom: 8px; }
.hit-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 11px; margin-bottom: 5px; }
.score { font-family: var(--font-mono); font-weight: 600; color: var(--color-success); }
.hit-title { font-weight: 600; color: var(--color-text); font-size: 12px; }
.hit-tag { color: var(--color-text-muted); }
.hit-text { font-size: 11.5px; color: var(--color-text-sub); line-height: 1.6; white-space: pre-wrap; }
.err { color: var(--color-danger); font-size: 12px; }

.empty { text-align: center; color: var(--color-text-muted); font-size: 12px; padding: 20px; }
.empty-detail {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  height: 100%; gap: 6px; color: var(--color-text-muted);
}
.empty-icon { font-size: 32px; }
.empty-title { font-size: 13px; color: var(--color-text-sub); }
.empty-desc { font-size: 11.5px; }
</style>
