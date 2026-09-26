// frontend/src/stores/identity.js
// 当前身份（模拟登录态）：决定"能看到哪些知识库文档"。
// 设计取舍：演示阶段不做登录体系，改成"前端选身份 + 后端强制校验"。
// 后端按请求头解析身份，所有过滤发生在检索层/查询层，前端改不了越权结果。
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

const STORAGE_KEY = 'workmind.identity'

// 预置身份：覆盖"同部门 / 跨部门 / 高密级 / 无权限"四种典型场景
export const IDENTITY_PRESETS = [
  {
    userId: 'u-hr-01', name: '韩梅梅', departments: ['hr'], clearance: 'internal',
    label: '人力资源 · HR 专员', hint: '可见：全员通用 + 人力资源（内部）',
  },
  {
    userId: 'u-tech-01', name: '李雷', departments: ['tech'], clearance: 'internal',
    label: '技术研发 · 前端工程师', hint: '可见：全员通用 + 技术研发（内部）',
  },
  {
    userId: 'u-fin-01', name: '王芳', departments: ['finance'], clearance: 'internal',
    label: '财务部 · 财务专员', hint: '可见：全员通用 + 财务（内部）',
  },
  {
    userId: 'u-dir-01', name: '张总', departments: ['hr', 'tech', 'finance', 'legal', 'product', 'sales', 'general'],
    clearance: 'confidential', label: '管理层 · 全部门', hint: '可见：全部文档（含机密）',
  },
  {
    userId: 'anonymous', name: '匿名访客', departments: ['general'], clearance: 'public',
    label: '未登录访客', hint: '可见：仅公开文档',
  },
]

export const useIdentityStore = defineStore('identity', () => {
  const tenantId = ref('tenant-demo')

  function load() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      if (raw) return JSON.parse(raw)
    } catch { /* 忽略损坏的本地缓存 */ }
    return IDENTITY_PRESETS[1]   // 默认：技术研发
  }

  const current = ref(load())
  // version 自增：各页面 watch 它来重新拉数据，避免整页刷新
  const version = ref(0)

  const departments = computed(() => current.value.departments || [])
  const label = computed(() =>
    IDENTITY_PRESETS.find(p => p.userId === current.value.userId)?.label || '自定义身份'
  )

  function setIdentity(preset) {
    current.value = { ...preset }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(current.value))
    version.value++
  }

  // 所有请求（axios + SSE）统一从这里取身份头
  function headers() {
    const c = current.value
    return {
      'X-Tenant-Id': tenantId.value,
      'X-User-Id': c.userId,
      'X-User-Name': encodeURIComponent(c.name || ''),
      'X-User-Departments': (c.departments || []).join(','),
      'X-User-Clearance': c.clearance || 'public',
    }
  }

  return { tenantId, current, version, departments, label, presets: IDENTITY_PRESETS, setIdentity, headers }
})
