<template>
  <!-- frontend/src/views/KnowledgeView.vue
       知识库 = 知识资产管理台（上传 / 元数据 / 切片 / 版本 / 删除），不提供对话。
       检索能力由「智能对话」与「任务 Agent」消费；这里只保留"检索验证"作为工程工具。 -->
  <div class="kb-view">
    <div class="stat-bar">
      <div class="stat">
        <span class="num">{{ stats.visible?.documentCount ?? 0 }}</span>
        <span class="lbl">可见文档</span>
      </div>
      <div class="stat">
        <span class="num">{{ stats.visible?.chunkCount ?? 0 }}</span>
        <span class="lbl">切片总数</span>
      </div>
      <div class="stat">
        <span class="num">{{ stats.visible?.elementCount ?? 0 }}</span>
        <span class="lbl">结构单元</span>
      </div>
      <div class="stat">
        <span class="num">{{ (stats.visible?.charCount ?? 0) > 9999 ? ((stats.visible.charCount / 1000).toFixed(1) + 'k') : (stats.visible?.charCount ?? 0) }}</span>
        <span class="lbl">总字符</span>
      </div>
      <div class="stat">
        <span class="num">{{ fmtSize(stats.visible?.fileBytes) }}</span>
        <span class="lbl">源文件占用</span>
      </div>
      <div class="stat">
        <span class="num">{{ stats.visible?.byDepartment ? Object.keys(stats.visible.byDepartment).length : 0 }}</span>
        <span class="lbl">涉及部门</span>
      </div>
      <div class="scope">
        <span class="scope-title">当前可见范围</span>
        <span class="scope-body">
          {{ identity.current.name }} ·
          {{ identity.current.departments.map(d => deptLabel(d)).join(' / ') }} ·
          {{ clearanceLabel(identity.current.clearance) }}
        </span>
        <span v-if="stats.hiddenByPermission" class="scope-warn">
          另有 {{ stats.hiddenByPermission }} 篇因权限不可见
        </span>
        <span v-if="stats.tenantTotal === 0" class="scope-warn">租户内暂无文档</span>
      </div>
    </div>

    <div class="body">
      <aside class="left">
        <div class="card">
          <DocumentUploader />
        </div>
        <div class="card grow">
          <div class="card-title">知识资产</div>
          <DocumentList />
        </div>
      </aside>
      <main class="right">
        <DocumentDetail />
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, watch } from 'vue'
import { useKnowledgeStore } from '@/stores/knowledge.js'
import { useIdentityStore } from '@/stores/identity.js'
import DocumentUploader from '@/components/rag/DocumentUploader.vue'
import DocumentList from '@/components/rag/DocumentList.vue'
import DocumentDetail from '@/components/rag/DocumentDetail.vue'

const knStore = useKnowledgeStore()
const identity = useIdentityStore()
const stats = computed(() => knStore.stats || {})

const DEPT = { general: '通用', hr: '人力', tech: '技术', finance: '财务', legal: '法务', product: '产品', sales: '市场' }
const deptLabel = (d) => DEPT[d] || d
const clearanceLabel = (c) => ({ public: '公开', internal: '内部', confidential: '机密' }[c] || c)

function fmtSize(b) {
  if (!b) return '0 B'
  if (b < 1024) return b + ' B'
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB'
  return (b / 1048576).toFixed(1) + ' MB'
}

// 首次进入加载；身份切换时列表组件内部也会 refresh
onMounted(() => knStore.refresh())
watch(() => identity.version, () => knStore.loadStats())
</script>

<style scoped>
.kb-view { display: flex; flex-direction: column; gap: 12px; height: 100%; }

.stat-bar {
  display: flex; align-items: stretch; gap: 8px;
  background: var(--color-surface); border: 1px solid var(--color-border);
  border-radius: var(--radius-lg); padding: 10px 14px;
}
.stat { display: flex; flex-direction: column; min-width: 84px; }
.stat .num { font-size: 18px; font-weight: 700; color: var(--color-text); line-height: 1.2; }
.stat .lbl { font-size: 11px; color: var(--color-text-muted); }
.scope {
  margin-left: auto; display: flex; flex-direction: column; align-items: flex-end;
  justify-content: center; gap: 2px; text-align: right;
}
.scope-title { font-size: 10.5px; color: var(--color-text-muted); }
.scope-body { font-size: 11.5px; color: var(--color-primary-dark); font-weight: 600; }
.scope-warn { font-size: 10.5px; color: var(--color-warning); }

.body { display: flex; gap: 12px; flex: 1; min-height: 0; }
/* 左栏自己滚动：上传表单很长，不滚动会把"入库"按钮和文档列表顶出可视区 */
.left { width: 560px; flex-shrink: 0; display: flex; flex-direction: column; gap: 12px; min-height: 0; overflow-y: auto; padding-right: 4px; }
.left::-webkit-scrollbar { width: 8px; }
.left::-webkit-scrollbar-thumb { background: var(--color-border); border-radius: 4px; }
.right {
  flex: 1; min-width: 0; display: flex; flex-direction: column;
  background: var(--color-surface); border: 1px solid var(--color-border);
  border-radius: var(--radius-lg); padding: 14px; overflow: hidden;
}
.card {
  background: var(--color-surface); border: 1px solid var(--color-border);
  border-radius: var(--radius-lg); padding: 12px;
  flex-shrink: 0;
}
/* flex:1 里含 flex-shrink:1 + flex-basis:0 —— 左栏空间不够时这张卡会被压到 20 多像素，
   里面的文档列表既看不见、也不会让左栏滚动（实测 1280x800 下就只有 24px 高）。
   改成"能撑大但不许压扁"：有空间就填满，没空间就保持自身高度，交给 .left 去滚。 */
.card.grow { flex: 1 0 auto; min-height: 0; display: flex; flex-direction: column; gap: 8px; }
.card-title { font-size: 13px; font-weight: 600; color: var(--color-text); }
</style>
