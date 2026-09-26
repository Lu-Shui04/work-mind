// frontend/src/router/index.js
// 路由配置：每个模块对应一个一级路由
import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    redirect: '/chat',
  },
  {
    path: '/chat',
    name: 'Chat',
    component: () => import('@/views/ChatView.vue'),
    meta: { title: '智能对话', icon: '💬' },
  },
  {
    path: '/knowledge',
    name: 'Knowledge',
    component: () => import('@/views/KnowledgeView.vue'),
    meta: { title: '知识库', icon: '📚' },
  },
  {
    path: '/agent',
    name: 'Agent',
    component: () => import('@/views/AgentView.vue'),
    meta: { title: '任务 Agent', icon: '🤖' },
  },
  {
    path: '/workflow',
    name: 'Workflow',
    component: () => import('@/views/WorkflowView.vue'),
    meta: { title: '内容工作流', icon: '⚙️' },
  },
  {
    path: '/erp',
    name: 'ERP',
    component: () => import('@/views/ErpView.vue'),
    meta: { title: '报销请假', icon: '📋' },
  },
  {
    path: '/prompt',
    name: 'Prompt',
    component: () => import('@/views/PromptView.vue'),
    meta: { title: 'Prompt 调试', icon: '🔧' },
  },
  {
    path: '/monitor',
    name: 'Monitor',
    component: () => import('@/views/MonitorView.vue'),
    meta: { title: '用量看板', icon: '📊' },
  },
  {
    // 全链路追踪：某一轮请求内部到底走了哪些步骤（含工具入参出参）
    path: '/trace',
    name: 'Trace',
    component: () => import('@/views/TraceView.vue'),
    meta: { title: '全链路追踪', icon: '🔗' },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 路由切换时更新页面 title
router.afterEach((to) => {
  document.title = `${to.meta.title || 'WorkMind'} — WorkMind AI`
})

// ── 懒加载 chunk 失效自愈 ──────────────────────────────────────
// 每次前端重新构建，各视图 chunk 的内容 hash 都会变（AgentView-a1b2.js → AgentView-c3d4.js）。
// 如果用户一直开着老页面不刷新，页面里的旧 bundle 仍然按旧文件名去请求，
// 服务器上已经没有了 → 404 → 动态 import 失败 → 点菜单毫无反应、也不报错。
// 这里捕获该错误并自动整页重载，让用户拿到新 bundle；重载后走的是新的 index.html。
const CHUNK_LOAD_ERROR = /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module|Unable to preload CSS/i
const RELOAD_GUARD_KEY = 'workmind:chunk-reload-at'
const RELOAD_GUARD_MS = 15000

router.onError((error, to) => {
  if (!CHUNK_LOAD_ERROR.test(error?.message || '')) return

  const lastReloadAt = Number(sessionStorage.getItem(RELOAD_GUARD_KEY) || 0)
  if (Date.now() - lastReloadAt > RELOAD_GUARD_MS) {
    // 只自动重载一次；若重载后仍然失败（资源真的没了），走下面兜底提示
    sessionStorage.setItem(RELOAD_GUARD_KEY, String(Date.now()))
    console.warn('[router] chunk 加载失败，正在自动刷新以获取最新前端资源：', error.message)
    window.location.assign(to?.fullPath || window.location.href)
    return
  }

  // 自动刷新都没救回来：明确告诉用户，而不是静默失败
  window.alert('页面资源已更新，请按 Ctrl + F5 强制刷新后重试。')
})

export default router
