<!-- frontend/src/components/chat/ScopeSelector.vue -->
<!-- 检索范围预设 —— 替代原来的"角色选择器"。
     角色（通用/技术/HR/法务）在后端只做一件事：换一句 system prompt，
     既不影响检索到哪些文档，也不影响权限，用户能感知的差异几乎为零；
     而且"要有代码示例""回答要有温度"这类要求会诱导模型在资料之外发挥，
     与"知识优先、资料不足就明说"的规则冲突。所以四个角色合并成一个助手，
     这一行改成**真的会改变检索结果**的范围预设。 -->
<template>
  <div class="scope-selector">
    <span class="scope-label">检索范围</span>
    <button
      v-for="p in presets"
      :key="p.id"
      class="scope-btn"
      :class="{ active: chatStore.scopeId === p.id }"
      :title="p.desc"
      @click="select(p)"
    >
      {{ p.label }}
    </button>
    <span class="scope-hint">{{ currentDesc }}</span>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useChatStore } from '@/stores/chat.js'

const chatStore = useChatStore()

// patch 会原样下发给后端：
//   department → knowledgeDepartment（硬过滤）
//   docType    → knowledgeDocType
//   includeSuperseded → 把"已被替代"的历史版本也纳入检索
const presets = [
  { id: 'all',     label: '全部可见',   desc: '不限部门/类型：检索当前身份能看到的全部文档', patch: {} },
  { id: 'general', label: '全员通用',   desc: '只看「归属部门 = 全员通用」的文档', patch: { department: 'general' } },
  { id: 'mine',    label: '本部门',     desc: '只看当前身份所属部门的文档（切换身份后自动跟随）', patch: { department: '__mine__' } },
  { id: 'policy',  label: '制度规定',   desc: '只看「文档类型 = 制度规定」', patch: { docType: 'policy' } },
  { id: 'manual',  label: '手册指南',   desc: '只看「文档类型 = 手册/指南」', patch: { docType: 'manual' } },
  { id: 'history', label: '含历史版本', desc: '把已被新版本替代的历史版本也纳入检索（默认只查当前生效版本）', patch: { includeSuperseded: true } },
]

const currentDesc = computed(() => presets.find(p => p.id === chatStore.scopeId)?.desc || '')

function select(p) {
  chatStore.setScope(p.id, p.patch)
}
</script>

<style scoped>
.scope-selector {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
  overflow-x: auto;
}

.scope-label {
  font-size: 11px;
  color: var(--color-text-muted);
  flex-shrink: 0;
}

.scope-btn {
  padding: 4px 11px;
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border);
  background: transparent;
  color: var(--color-text-sub);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  transition: all var(--transition);
}
.scope-btn:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
}
.scope-btn.active {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: #fff;
}

.scope-hint {
  font-size: 11px;
  color: var(--color-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-left: 4px;
}
</style>
