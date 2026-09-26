// frontend/src/stores/chat.js
// 对话模块全局状态：会话列表、当前会话消息、角色、用户画像
import { defineStore } from 'pinia'
import { ref, computed, reactive, watch } from 'vue'
import { fetchStream } from '@/utils/http.js'
import http from '@/utils/http.js'
import { useAppStore } from './app.js'
import { useMonitorStore } from './monitor.js'
import { useIdentityStore } from './identity.js'

export const useChatStore = defineStore('chat', () => {
  const appStore     = useAppStore()
  const monitorStore = useMonitorStore()
  const identity     = useIdentityStore()

  // ── 会话列表 ──────────────────────────────────────────────────
  // 每个会话：{ id, title, messages: [], createdAt }
  const sessions    = ref([])
  const currentId   = ref(null)

  const currentSession = computed(() =>
    sessions.value.find(s => s.id === currentId.value) || null
  )

  const messages = computed(() =>
    currentSession.value?.messages || []
  )

  // ── 初始化：创建第一个会话 ────────────────────────────────────
  function init() {
    if (sessions.value.length === 0) {
      newSession()
    }
  }

  function newSession() {
    const id = `session_${Date.now()}`
    sessions.value.unshift({
      id,
      title: '新对话',
      messages: [],
      createdAt: new Date().toISOString(),
    })
    currentId.value = id
    return id
  }

  function switchSession(id) {
    currentId.value = id
  }

  function deleteSession(id) {
    const idx = sessions.value.findIndex(s => s.id === id)
    if (idx === -1) return
    sessions.value.splice(idx, 1)

    // 如果删的是当前会话，切到第一个
    if (currentId.value === id) {
      currentId.value = sessions.value[0]?.id || null
      if (!currentId.value) newSession()
    }

    // 同步删除服务端会话历史
    http.delete(`/chat/sessions/${id}`).catch(() => {})
  }

  // 根据第一条消息自动生成会话标题
  function updateTitle(sessionId, firstMessage) {
    const s = sessions.value.find(s => s.id === sessionId)
    if (s && s.title === '新对话') {
      s.title = firstMessage.slice(0, 20) + (firstMessage.length > 20 ? '...' : '')
    }
  }

  // ── 知识库检索模式（用户显式控制，覆盖后端的自动判定）────────
  // 三种模式的语义（后端严格按这个执行）：
  //   auto  = 自动：默认就查库；查到就用资料回答（带引用），
  //           **查不到会明确说明，然后照样用通用知识回答** —— 不会拿一句"没找到"把人堵死
  //   force = 强制：只依据知识库作答，查不到就说查不到，不许用模型自己的知识补充
  //   off   = 关闭：压根不查库，直接回答
  //
  // 要持久化：以前是纯内存状态，刷新页面/切页面后静默回到"自动"，
  // 用户明明选了"关闭"，下一次提问又走了知识库 —— 这是个真实的坑。
  const KB_MODE_KEY = 'workmind.chat.kbMode'
  const knowledgeMode = ref(localStorage.getItem(KB_MODE_KEY) || 'auto')
  watch(knowledgeMode, (v) => {
    try { localStorage.setItem(KB_MODE_KEY, v) } catch { /* 隐私模式下写不了，忽略 */ }
  })

  // ── 检索范围（替代原来的"角色选择器"）────────────────────────
  // 角色只换一句 system prompt、不影响检索到哪些文档；这里每一项都真的
  // 改变"查哪些文档"，是用户能感知到差异的地方。
  // department 里的 '__mine__' 在发送时解析成当前身份的第一个部门。
  const EMPTY_SCOPE = { department: null, docType: null, version: null, includeSuperseded: false }
  const scopeId = ref('all')
  const scope = ref({ ...EMPTY_SCOPE })

  function setScope(id, patch = {}) {
    scopeId.value = id
    scope.value = { ...EMPTY_SCOPE, ...patch }
  }

  // "本部门"预设：用当前身份的第一个部门；身份切换后自动跟着变
  const resolvedDepartment = computed(() => {
    const d = scope.value.department
    if (!d) return undefined
    if (d === '__mine__') return identity.departments?.[0] || undefined
    return d
  })

  // ── 用户画像 ──────────────────────────────────────────────────
  const profile = ref({})
  const userId  = ref('user-demo')

  async function loadProfile() {
    try {
      const data = await http.get(`/chat/profile/${userId.value}`)
      profile.value = data
    } catch {}
  }

  // ── 发送消息（核心）──────────────────────────────────────────
  const loading = ref(false)

  async function sendMessage(text) {
    if (!text.trim() || loading.value) return
    if (!currentId.value) newSession()

    const session = currentSession.value
    loading.value = true

    // 添加用户消息
    const userMsg = {
      id:      `msg_${Date.now()}`,
      role:    'user',
      content: text,
      time:    new Date().toISOString(),
    }
    session.messages.push(userMsg)
    updateTitle(currentId.value, text)

    // 添加 AI 消息占位（流式填充）
    // 必须用 reactive() 包裹，使本地引用也是响应式代理
    // 否则 push 后 Vue 给数组元素套的 Proxy 与本地变量是两个对象，onToken 里的赋值不触发更新
    const aiMsg = reactive({
      id:         `msg_${Date.now() + 1}`,
      role:       'assistant',
      content:    '',
      fromCache:  false,
      streaming:  true,
      // 知识库联动：意图判定结果 + 命中的引用来源（后端已按当前身份过滤）
      intent:     null,
      sources:    [],
      // 召回诊断：未命中时说明原因（库是空的 / 被权限过滤 / 分数低于阈值）
      recall:     null,
      time:       new Date().toISOString(),
    })
    session.messages.push(aiMsg)

    await fetchStream(
      '/api/chat/stream',
      {
        message:   text,
        sessionId: currentId.value,
        // role 已废弃（四个角色预设合并成一个助手），保留字段只为兼容
        role:      'default',
        userId:    identity.current.userId,
        // 检索范围（前端显式选择 → 后端当硬过滤；不选则按身份自动）
        knowledgeDepartment: resolvedDepartment.value,
        knowledgeDocType: scope.value.docType || undefined,
        knowledgeVersion: scope.value.version || undefined,
        includeSuperseded: scope.value.includeSuperseded || undefined,
        // undefined=自动判定 / true=强制检索 / false=关闭检索
        useKnowledge: knowledgeMode.value === 'auto' ? undefined : knowledgeMode.value === 'force',
      },
      {
        onToken: (token) => {
          aiMsg.content += token
        },
        onEvent: (event, data) => {
          if (event === 'cache_hit') aiMsg.fromCache = true
          if (event === 'start')     aiMsg.streaming = true
          // 意图判定：这次为什么（不）去查知识库，前端直接展示，便于解释与排障
          if (event === 'intent')    aiMsg.intent = data
          if (event === 'sources') {
            aiMsg.sources = data.sources || []
            aiMsg.recall  = data.recall || null
          }
        },
        onDone: () => {
          aiMsg.streaming = false
          // 用量由**后端**记账：每次模型调用写一行 usage_calls（命中也记，含省下的 token）。
          // 以前前端在这里自己累加 todaySpend，那份数字只活在当前浏览器标签里，
          // 一刷新就归零，和看板上的数字永远对不上。这里只负责稍后刷新看板。
          setTimeout(() => monitorStore.refresh(), 600)
          // 刷新画像（后台可能更新了）
          loadProfile()
        },
        onError: (err) => {
          aiMsg.streaming = false
          aiMsg.content   = aiMsg.content || '抱歉，出现了一些问题，请重试。'
          appStore.toast.error(err.message || '发送失败')
        },
      }
    )

    loading.value = false
  }

  // 重新生成最后一条 AI 回复
  async function regenerate() {
    const msgs = currentSession.value?.messages || []
    // 找最后一条用户消息
    const lastUser = [...msgs].reverse().find(m => m.role === 'user')
    if (!lastUser) return

    // 移除最后一条 AI 消息
    const lastAiIdx = msgs.length - 1
    if (msgs[lastAiIdx]?.role === 'assistant') {
      msgs.splice(lastAiIdx, 1)
    }

    await sendMessage(lastUser.content)
  }

  // ── 清空当前会话（前端消息 + 服务端历史）────────────────────
  async function clearCurrentSession() {
    const session = currentSession.value
    if (!session) return
    session.messages.splice(0, session.messages.length)
    session.title = '新对话'
    try {
      // 服务端历史也要清，否则模型还会"记得"清空前的内容
      await http.delete(`/chat/sessions/${session.id}`)
      appStore.toast.success('已清空当前会话（含服务端上下文）')
    } catch {
      appStore.toast.warning('本地已清空，但服务端历史清除失败')
    }
  }

  // 清空全部会话（测试时批量清理）
  async function clearAllSessions() {
    try {
      await http.delete('/chat/sessions')
    } catch { /* 服务端失败不阻断本地清理 */ }
    sessions.value = []
    currentId.value = null
    newSession()
    appStore.toast.success('已清空全部会话')
  }

  // 复制消息内容
  async function copyMessage(content) {
    await navigator.clipboard.writeText(content)
    appStore.toast.success('已复制到剪贴板')
  }

  return {
    sessions, currentId, currentSession, messages,
    knowledgeMode, scope, scopeId, setScope, resolvedDepartment,
    profile, userId,
    loading,
    init, newSession, switchSession, deleteSession,
    loadProfile,
    sendMessage, regenerate, copyMessage, clearCurrentSession, clearAllSessions,
  }
})
