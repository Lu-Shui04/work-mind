<template>
  <!-- frontend/src/components/rag/DocumentList.vue
       知识资产列表：以元数据为主视角（部门/版本/类型/密级/状态/切片数/来源文件）。 -->
  <div class="doc-list">
    <div class="toolbar">
      <input v-model="knStore.filters.keyword" class="search" placeholder="搜索标题 / 文件名"
             @keydown.enter="knStore.loadDocuments()" />
      <select v-model="knStore.filters.department" class="sel" @change="knStore.loadDocuments()">
        <option value="">全部部门</option>
        <option v-for="d in options.departments" :key="d.value" :value="d.value">{{ d.label }}</option>
      </select>
      <select v-model="knStore.filters.docType" class="sel" @change="knStore.loadDocuments()">
        <option value="">全部类型</option>
        <option v-for="t in options.docTypes" :key="t.value" :value="t.value">{{ t.label }}</option>
      </select>
      <select v-model="knStore.filters.version" class="sel" @change="knStore.loadDocuments()">
        <option value="">全部版本</option>
        <option v-for="v in options.versions" :key="v" :value="v">{{ v }}</option>
      </select>
      <select v-model="knStore.filters.status" class="sel" @change="knStore.loadDocuments()">
        <option value="">全部状态</option>
        <option v-for="s in options.docStatuses" :key="s.value" :value="s.value">{{ s.label }}</option>
      </select>
      <button class="ghost" @click="knStore.resetFilters()">重置</button>
      <button class="ghost" @click="knStore.loadDocuments()" :disabled="knStore.loading">刷新</button>
      <button class="ghost danger" :disabled="!knStore.documents.length || knStore.loading" @click="clearAll">
        清空全部
      </button>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>文档</th>
            <th>部门</th>
            <th>版本</th>
            <th>类型</th>
            <th>密级</th>
            <th>状态</th>
            <th class="num">切片</th>
            <th class="num">页</th>
            <th class="num">大小</th>
            <th>上传人</th>
            <th class="ops">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!knStore.documents.length">
            <td colspan="11" class="empty">
              {{ knStore.loading ? '加载中...' : '当前身份可见范围内没有文档' }}
            </td>
          </tr>
          <tr v-for="d in knStore.documents" :key="d.doc_id"
              :class="{ selected: d.doc_id === knStore.selectedId }"
              @click="knStore.selectDocument(d.doc_id)">
            <td class="doc-cell">
              <div class="doc-title">{{ d.document_title }}</div>
              <div class="doc-file" :title="d.file_name">{{ d.file_name }}</div>
            </td>
            <td><span class="tag">{{ deptLabel(d.department) }}</span></td>
            <td class="mono">{{ d.version }}</td>
            <td>{{ typeLabel(d.doc_type) }}</td>
            <td><span class="level" :class="d.security_level">{{ levelLabel(d.security_level) }}</span></td>
            <td>
              <span class="status" :class="d.status">{{ statusLabel(d.status) }}</span>
              <span v-if="d.ingest_status === 'failed'" class="status failed" :title="d.ingest_error">入库失败</span>
            </td>
            <td class="num">{{ d.chunk_count }}</td>
            <td class="num">{{ d.page_count }}</td>
            <td class="num">{{ formatSize(d.file_size) }}</td>
            <td class="dim">{{ d.created_by_name || d.created_by }}</td>
            <td class="ops" @click.stop>
              <button class="op" @click="knStore.selectDocument(d.doc_id)">详情</button>
              <button class="op" @click="download(d)">源文件</button>
              <button class="op" @click="reindex(d)">重建</button>
              <button class="op danger" @click="remove(d)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="foot">
      共 {{ knStore.documents.length }} 篇 · 切片 {{ knStore.stats?.visible?.chunkCount ?? 0 }} ·
      字符 {{ formatNum(knStore.stats?.visible?.charCount ?? 0) }} ·
      存储 {{ formatSize(knStore.stats?.visible?.fileBytes ?? 0) }}
      <span v-if="knStore.stats?.hiddenByPermission" class="hidden-note">
        （另有 {{ knStore.stats.hiddenByPermission }} 篇因权限不可见）
      </span>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, watch } from 'vue'
import { useKnowledgeStore } from '@/stores/knowledge.js'
import { useIdentityStore } from '@/stores/identity.js'
import { useAppStore } from '@/stores/app.js'

const knStore = useKnowledgeStore()
const identity = useIdentityStore()
const appStore = useAppStore()
const options = computed(() => knStore.options)

const DEPT = { general: '通用', hr: '人力', tech: '技术', finance: '财务', legal: '法务', product: '产品', sales: '市场' }
const TYPE = { policy: '制度', manual: '手册', spec: '规范', report: '报告', contract: '合同', other: '其他' }
const LEVEL = { public: '公开', internal: '内部', confidential: '机密' }
const STATUS = { active: '生效中', superseded: '已被替代', archived: '已归档' }

const deptLabel = (v) => DEPT[v] || v
const typeLabel = (v) => TYPE[v] || v
const levelLabel = (v) => LEVEL[v] || v
const statusLabel = (v) => STATUS[v] || v

function formatSize(b) {
  if (!b) return '—'
  if (b < 1024) return b + ' B'
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB'
  return (b / 1048576).toFixed(1) + ' MB'
}
function formatNum(n) { return n > 10000 ? (n / 1000).toFixed(1) + 'k' : String(n) }

// 清空当前身份可见范围内的全部文档（含源文件与向量切片）
async function clearAll() {
  const n = knStore.documents.length
  if (!confirm(`清空当前可见的 ${n} 篇文档？\n\n会一并删除源文件与向量切片，且不可恢复。`)) return
  await knStore.clearAllDocuments()
}

async function download(d) {
  try { await knStore.downloadSource(d.doc_id) }
  catch { appStore.toast.error('下载失败') }
}
async function reindex(d) {
  try { await knStore.reindexDocument(d.doc_id) }
  catch (err) { appStore.toast.error(err?.response?.data?.error?.message || '重建失败') }
}
async function remove(d) {
  if (!confirm('确认删除《' + d.document_title + '》？切片与源文件会一并删除，且不可恢复。')) return
  try { await knStore.deleteDocument(d.doc_id) }
  catch (err) { appStore.toast.error(err?.response?.data?.error?.message || '删除失败') }
}

// 身份切换后自动重新加载（权限变化 → 可见文档集合变化）
watch(() => identity.version, () => knStore.refresh())
onMounted(() => knStore.loadDocuments())
</script>

<style scoped>
.doc-list { display: flex; flex-direction: column; gap: 8px; }
.toolbar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.search, .sel {
  border: 1px solid var(--color-border); border-radius: var(--radius-sm);
  padding: 4px 7px; font-size: 11.5px; background: var(--color-surface); color: var(--color-text); outline: none;
}
.search { flex: 1; min-width: 110px; }
.ghost {
  border: 1px solid var(--color-border); background: var(--color-surface); cursor: pointer;
  border-radius: var(--radius-sm); padding: 4px 9px; font-size: 11.5px; color: var(--color-text-sub);
}
.ghost:hover { border-color: var(--color-primary); color: var(--color-primary); }
.ghost.danger:hover:not(:disabled) { border-color: var(--color-danger); color: var(--color-danger); }
.ghost:disabled { opacity: .45; cursor: not-allowed; }

.table-wrap { overflow: auto; max-height: 46vh; border: 1px solid var(--color-border); border-radius: var(--radius-md); }
table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
thead th {
  position: sticky; top: 0; z-index: 1; text-align: left; white-space: nowrap;
  background: var(--color-bg); color: var(--color-text-sub); font-weight: 600;
  padding: 7px 8px; border-bottom: 1px solid var(--color-border);
}
tbody td { padding: 7px 8px; border-bottom: 1px solid var(--color-border-light); color: var(--color-text); vertical-align: top; }
tbody tr { cursor: pointer; transition: var(--transition); }
tbody tr:hover { background: var(--color-primary-bg); }
tbody tr.selected { background: var(--color-primary-bg); box-shadow: inset 3px 0 0 var(--color-primary); }
.doc-cell { min-width: 170px; }
.doc-title { font-weight: 600; }
.doc-file { font-size: 10.5px; color: var(--color-text-muted); margin-top: 2px; }
.mono { font-family: var(--font-mono); font-size: 11px; }
.num { text-align: right; white-space: nowrap; }
.dim { color: var(--color-text-muted); white-space: nowrap; }
.empty { text-align: center; color: var(--color-text-muted); padding: 24px; }

.tag { background: var(--color-border-light); color: var(--color-text-sub); border-radius: var(--radius-sm); padding: 1px 6px; }
.level, .status { border-radius: var(--radius-full); padding: 1px 7px; font-size: 10.5px; white-space: nowrap; }
.level.public { background: #dcfce7; color: #15803d; }
.level.internal { background: #e0e7ff; color: #4338ca; }
.level.confidential { background: #fee2e2; color: #b91c1c; }
.status.active { background: #dcfce7; color: #15803d; }
.status.superseded { background: #fef3c7; color: #b45309; }
.status.archived { background: var(--color-border-light); color: var(--color-text-muted); }
.status.failed { background: #fee2e2; color: #b91c1c; margin-left: 4px; }

.ops { white-space: nowrap; }
.op { border: none; background: none; cursor: pointer; font-size: 11px; color: var(--color-primary); padding: 1px 4px; }
.op:hover { text-decoration: underline; }
.op.danger { color: var(--color-danger); }

.foot { font-size: 11px; color: var(--color-text-muted); }
.hidden-note { color: var(--color-warning); }
</style>
