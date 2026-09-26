// frontend/src/stores/workflow.js
// 工作流模块状态：模板选择、节点执行状态、人工审核、结果
import { defineStore } from 'pinia'
import { ref, reactive } from 'vue'
import { fetchStream } from '@/utils/http.js'
import http from '@/utils/http.js'
import { useAppStore } from './app.js'

export const useWorkflowStore = defineStore('workflow', () => {
  const appStore = useAppStore()

  // ── 模板列表（默认值保证始终可见）────────────────────────
  const templates = ref([
    {
      id: 'weekly_report', title: '周报生成', icon: '📊',
      desc: '输入本周工作要点，自动提炼亮点、识别风险，生成规范周报',
      inputLabel: '本周工作要点', inputPlaceholder: '请简单描述本周完成的主要工作，一条一行...',
      extraField: { key: 'dept', label: '部门名称', placeholder: '如：前端研发组' },
      nodes: [
        { id: 'extract_highlights', label: '提炼工作亮点' },
        { id: 'identify_risks',     label: '识别风险阻塞' },
        { id: 'human_review',       label: '人工审核', isHuman: true },
        { id: 'generate_report',    label: '生成周报' },
      ],
      resultKey: 'report',
    },
    {
      id: 'meeting_minutes', title: '会议纪要', icon: '📝',
      desc: '粘贴会议原始记录，自动提取结论和 Action Items，生成正式纪要',
      inputLabel: '会议原始记录', inputPlaceholder: '粘贴会议记录，包括讨论内容、发言摘要等...',
      extraField: { key: 'meetingTitle', label: '会议名称', placeholder: '如：产品周会 2024-03' },
      nodes: [
        { id: 'extract_attendees',   label: '提取参会人与议题' },
        { id: 'extract_conclusions', label: '提取会议结论' },
        { id: 'extract_actions',     label: '整理 Action Items' },
        { id: 'human_review',        label: '人工审核', isHuman: true },
        { id: 'generate_minutes',    label: '生成纪要' },
      ],
      resultKey: 'minutes',
    },
    {
      id: 'email_polish', title: '邮件润色', icon: '✉️',
      desc: '输入邮件草稿，AI 分析语气和问题，润色成正式邮件',
      inputLabel: '邮件草稿', inputPlaceholder: '粘贴你的邮件草稿...',
      extraField: { key: 'recipient', label: '收件人/场景', placeholder: '如：客户、上级、合作方' },
      nodes: [
        { id: 'analyze_intent', label: '分析写作意图' },
        { id: 'check_issues',   label: '检查问题' },
        { id: 'human_review',   label: '人工审核', isHuman: true },
        { id: 'polish_email',   label: '生成润色版本' },
      ],
      resultKey: 'polished',
    },
    {
      id: 'prd_skeleton', title: 'PRD 骨架', icon: '📋',
      desc: '输入需求描述，自动提取功能点和约束，生成结构化 PRD 文档',
      inputLabel: '需求描述', inputPlaceholder: '用自然语言描述你的产品需求...',
      nodes: [
        { id: 'extract_features',     label: '提取功能点' },
        { id: 'identify_constraints', label: '识别约束条件' },
        { id: 'human_review',         label: '人工审核', isHuman: true },
        { id: 'generate_prd',         label: '生成 PRD' },
      ],
      resultKey: 'prd',
    },
  ])

  async function loadTemplates() {
    try {
      const data = await http.get('/workflow/templates')
      if (data.templates?.length) templates.value = data.templates
    } catch {}
  }

  // ── 当前工作流运行状态 ─────────────────────────────────────
  // selectedTemplate：当前选择的模板 id
  const selectedTemplate = ref('')
  // nodeStates：{ [nodeId]: 'idle' | 'running' | 'done' | 'waiting' }
  const nodeStates  = reactive({})
  // nodeOutputs：{ [nodeId]: '节点输出预览文本' }
  const nodeOutputs = reactive({})
  // running：工作流是否正在执行
  const running  = ref(false)
  // paused：是否暂停在 human_review
  const paused   = ref(false)
  // currentThreadId：当前工作流的线程 ID（恢复时用）
  const currentThreadId = ref('')
  // intermediates：暂停时的中间产物（供人工审核查看）
  const intermediates = ref([])
  // result：最终生成的内容
  const result   = ref('')
  // streamBuffer：最后一个节点流式输出的累积内容
  const streamBuffer = ref('')

  // ── 重置状态 ────────────────────────────────────────────────
  function reset() {
    const templateMeta = templates.value.find(t => t.id === selectedTemplate.value)
    if (templateMeta) {
      templateMeta.nodes.forEach(n => {
        nodeStates[n.id]  = 'idle'
        nodeOutputs[n.id] = ''
      })
    }
    running.value       = false
    paused.value        = false
    currentThreadId.value = ''
    intermediates.value = []
    result.value        = ''
    streamBuffer.value  = ''
  }

  function selectTemplate(id) {
    selectedTemplate.value = id
    reset()
  }

  // ── 启动工作流 ─────────────────────────────────────────────
  async function startWorkflow(input) {
    if (running.value) return
    // ⚠️ 顺序不能反：reset() 内部会把 running 置回 false，
    // 先置 true 再 reset 等于没置 —— 表现就是"点开始执行后界面一直停在输入表单上"，
    // 用户既看不到进度也看不到转圈（真实踩过）。
    reset()
    running.value = true

    await fetchStream(
      '/api/workflow/start/stream',
      { workflowId: selectedTemplate.value, input },
      {
        onEvent: (event, data) => {
          if (event === 'start') {
            currentThreadId.value = data.threadId
          }

          // 流式 token：结果节点的进正文面板，其它节点的贴在对应流程卡片上
          // （这样"流程走到哪一步、正在生成什么"都能实时看见）
          if (event === 'token') {
            appendToken(data)
            return
          }

          if (event === 'node_start') {
            nodeStates[data.nodeId] = 'running'
          }

          if (event === 'node_done') {
            nodeStates[data.nodeId] = 'done'
            // 实时流出来的内容比后端给的 80 字预览完整，别用预览覆盖掉
            if (data.preview && !nodeOutputs[data.nodeId]) nodeOutputs[data.nodeId] = data.preview
          }

          if (event === 'paused') {
            // 到达 human_review，工作流暂停
            currentThreadId.value   = data.threadId
            intermediates.value     = data.intermediates || []
            paused.value            = true
            running.value           = false
            // 把 human_review 节点标记为 waiting
            nodeStates['human_review'] = 'waiting'
          }

          if (event === 'completed') {
            result.value  = data.result
            running.value = false
          }
        },
        onDone: () => {
          running.value = false
        },
        onError: (err) => {
          running.value = false
          appStore.toast.error(err.message || '工作流执行失败')
        },
      }
    )
  }

  // 单个节点的预览不无限增长，超过就截断（流程图卡片只用于"看进度"，不用于读全文）
  const NODE_PREVIEW_LIMIT = 240
  function appendToken(data) {
    if (!data || !data.token) return
    if (data.isResult) {
      streamBuffer.value += data.token
      return
    }
    const id = data.nodeId
    if (!id) return
    const prev = nodeOutputs[id] || ''
    if (prev.length < NODE_PREVIEW_LIMIT) {
      nodeOutputs[id] = (prev + data.token).slice(0, NODE_PREVIEW_LIMIT)
    }
  }

  // ── 恢复工作流（注入人工反馈后继续）──────────────────────
  async function resumeWorkflow(feedback = '') {
    if (!currentThreadId.value || running.value) return
    running.value = true
    paused.value  = false
    nodeStates['human_review'] = 'done'
    streamBuffer.value = ''

    await fetchStream(
      '/api/workflow/resume/stream',
      { threadId: currentThreadId.value, feedback },
      {
        // 这里刻意不注册 onToken：工作流的 token 要按 nodeId 分流（正文 vs 节点卡片），
        // 统一走 onEvent 最简单（onToken 只给 token 文本，拿不到 nodeId）
        onEvent: (event, data) => {
          if (event === 'token') {
            appendToken(data)
            return
          }
          if (event === 'node_start') {
            nodeStates[data.nodeId] = 'running'
          }
          if (event === 'node_done') {
            nodeStates[data.nodeId] = 'done'
          }
          if (event === 'completed') {
            result.value       = data.result || streamBuffer.value
            streamBuffer.value = ''
            running.value      = false
          }
        },
        onDone: () => {
          if (streamBuffer.value && !result.value) {
            result.value = streamBuffer.value
          }
          running.value = false
        },
        onError: (err) => {
          running.value = false
          appStore.toast.error(err.message || '恢复失败')
        },
      }
    )
  }

  return {
    templates, selectedTemplate,
    nodeStates, nodeOutputs, running, paused,
    currentThreadId, intermediates, result, streamBuffer,
    loadTemplates, selectTemplate, startWorkflow, resumeWorkflow, reset,
  }
})
