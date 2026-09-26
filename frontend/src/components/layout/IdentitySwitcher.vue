<template>
  <!-- frontend/src/components/layout/IdentitySwitcher.vue
       当前身份切换器：决定"能看到哪些知识库文档"。
       切换后不刷新页面，靠 identity.version 让各页面重新拉数据。 -->
  <div class="identity" ref="rootEl">
    <button class="identity-btn" :class="{ open }" @click="open = !open" :title="current.hint">
      <span class="avatar">{{ current.name.slice(0, 1) }}</span>
      <span class="meta">
        <span class="name">{{ current.name }}</span>
        <span class="dept">
          <span v-for="d in current.departments.slice(0, 2)" :key="d" class="dept-tag">{{ deptLabel(d) }}</span>
          <span v-if="current.departments.length > 2" class="dept-tag">+{{ current.departments.length - 2 }}</span>
        </span>
      </span>
      <span class="caret">▾</span>
    </button>

    <div v-if="open" class="identity-menu">
      <div class="menu-head">
        <div class="menu-title">切换身份</div>
        <div class="menu-sub">权限在后端强制校验，前端切换只改变可见范围</div>
      </div>
      <button
        v-for="p in presets"
        :key="p.userId"
        class="menu-item"
        :class="{ active: p.userId === current.userId }"
        @click="choose(p)"
      >
        <span class="item-avatar">{{ p.name.slice(0, 1) }}</span>
        <span class="item-body">
          <span class="item-name">{{ p.name }}<span class="item-label">{{ p.label }}</span></span>
          <span class="item-hint">{{ p.hint }}</span>
        </span>
        <span v-if="p.userId === current.userId" class="item-check">✓</span>
      </button>
      <div class="menu-foot">
        密级：<b>{{ clearanceLabel(current.clearance) }}</b> · 租户：<b>{{ identity.tenantId }}</b>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useIdentityStore, IDENTITY_PRESETS } from '@/stores/identity.js'

const identity = useIdentityStore()
const presets = IDENTITY_PRESETS
const open = ref(false)
const rootEl = ref(null)

const current = computed(() => identity.current)

const DEPT_LABELS = {
  general: '通用', hr: '人力', tech: '技术', finance: '财务',
  legal: '法务', product: '产品', sales: '市场',
}
const deptLabel = (d) => DEPT_LABELS[d] || d
const clearanceLabel = (c) => ({ public: '公开', internal: '内部', confidential: '机密' }[c] || c)

function choose(p) {
  identity.setIdentity(p)
  open.value = false
}

// 点击外部关闭
function onClickOutside(e) {
  if (rootEl.value && !rootEl.value.contains(e.target)) open.value = false
}
onMounted(() => document.addEventListener('click', onClickOutside))
onUnmounted(() => document.removeEventListener('click', onClickOutside))
</script>

<style scoped>
.identity { position: relative; }
.identity-btn {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 10px; border: 1px solid var(--color-border);
  background: var(--color-surface); border-radius: var(--radius-md);
  cursor: pointer; transition: var(--transition);
}
.identity-btn:hover, .identity-btn.open { border-color: var(--color-primary); background: var(--color-primary-bg); }
.avatar {
  width: 26px; height: 26px; border-radius: 50%;
  background: var(--color-primary); color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 600; flex-shrink: 0;
}
.meta { display: flex; flex-direction: column; align-items: flex-start; line-height: 1.25; }
.name { font-size: 13px; font-weight: 600; color: var(--color-text); }
.dept { display: flex; gap: 4px; }
.dept-tag {
  font-size: 10px; color: var(--color-primary-dark);
  background: var(--color-primary-bg); border-radius: var(--radius-sm);
  padding: 0 4px;
}
.caret { font-size: 10px; color: var(--color-text-muted); }

.identity-menu {
  position: absolute; right: 0; top: calc(100% + 6px); z-index: 50;
  width: 300px; background: var(--color-surface);
  border: 1px solid var(--color-border); border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg); overflow: hidden;
}
.menu-head { padding: 10px 12px; border-bottom: 1px solid var(--color-border-light); }
.menu-title { font-size: 13px; font-weight: 600; color: var(--color-text); }
.menu-sub { font-size: 11px; color: var(--color-text-muted); margin-top: 2px; }
.menu-item {
  display: flex; align-items: center; gap: 10px; width: 100%;
  padding: 9px 12px; background: none; border: none; cursor: pointer;
  text-align: left; transition: var(--transition);
}
.menu-item:hover { background: var(--color-border-light); }
.menu-item.active { background: var(--color-primary-bg); }
.item-avatar {
  width: 24px; height: 24px; border-radius: 50%; flex-shrink: 0;
  background: var(--color-border); color: var(--color-text-sub);
  display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 600;
}
.menu-item.active .item-avatar { background: var(--color-primary); color: #fff; }
.item-body { display: flex; flex-direction: column; flex: 1; min-width: 0; }
.item-name { font-size: 12.5px; color: var(--color-text); font-weight: 600; }
.item-label { font-weight: 400; color: var(--color-text-muted); margin-left: 6px; font-size: 11px; }
.item-hint { font-size: 11px; color: var(--color-text-muted); }
.item-check { color: var(--color-primary); font-size: 13px; }
.menu-foot {
  padding: 8px 12px; border-top: 1px solid var(--color-border-light);
  font-size: 11px; color: var(--color-text-muted); background: var(--color-bg);
}
</style>
