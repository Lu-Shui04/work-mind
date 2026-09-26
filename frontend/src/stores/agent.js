// frontend/src/stores/agent.js
// Agent 模块状态：任务历史、工具调用步骤、执行状态
import { defineStore } from 'pinia'
import { ref, reactive } from 'vue'
import { fetchStream } from '@/utils/http.js'
import http from '@/utils/http.js'
import { useAppStore } from './app.js'

export const useAgentStore = defineStore('agent', () => {
  const appStore = useAppStore()

  // ── 工具列表（从后端加载，默认值保证始终可见）──────────────
  const toolList = ref([
    { name: 'web_search',   label: '联网搜索', description: '搜索最新技术资讯和信息' },
    { name: 'read_doc',     label: '文档检索', description: '从公司知识库检索文档' },
    { name: 'calculate',    label: '数学计算', description: '金额、工期等数学计算' },
    { name: 'get_date',     label: '日期查询', description: '日期查询和工作日计算' },
    { name: 'write_report', label: '生成报告', description: '生成并保存分析报告' },
    { name: 'send_notify',  label: '发送通知', description: '发送通知给相关人员' },
  ])
  const examples = ref([
    { title: '技术调研', task: '对比 Vue3 和 React 2024年的最新状态，分别查询它们的最新版本和主要特性，生成一份技术选型报告' },
    { title: '费用计算', task: '我出差3天，酒店每晚580元，机票往返1200元，餐费每天150元，帮我计算总报销金额，并查询一下公司差旅报销标准' },
    { title: '工期计算', task: '项目计划从2024年3月1日开始，需要45个工作日完成，帮我计算预计完成日期，并生成一份项目时间轴摘要' },
    { title: '知识查询', task: '从知识库查询公司的年假政策，计算一下我今年还剩多少年假（假设今年已用6天，总共15天），并发送结果通知给HR' },
  ])

  async function loadMeta() {
    try {
      const [toolsRes, examplesRes] = await Promise.all([
        http.get('/agent/tools'),
        http.get('/agent/examples'),
      ])
      if (toolsRes.tools?.length)     toolList.value = toolsRes.tools
      if (examplesRes.examples?.length) examples.value = examplesRes.examples
    } catch {}
  }

  // ── 任务执行历史 ───────────────────────────────────────────
  // 每个任务是一条记录：{ id, task, steps, answer, status, startTime, duration }
  const tasks    = ref([])
  const running  = ref(false)

  // ── 会话 id：Agent 的上下文记忆是按会话存的 ──────────────────
  // 后端会用「摘要 + 最近 10 轮」记住这个会话里之前做过什么任务，
  // 所以下一次任务可以说"再算一下刚才那个"。
  // 持久化到 localStorage：刷新页面不该把记忆切断。
  const SESSION_KEY = 'workmind.agent.session'
  const newSessionId = () =>
    'agent-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
  const sessionId = ref(localStorage.getItem(SESSION_KEY) || newSessionId())
  localStorage.setItem(SESSION_KEY, sessionId.value)

  // 当前正在执行的任务状态（实时更新）
  const currentTask = ref(null)

  let taskId = 0

  // ── 执行任务 ───────────────────────────────────────────────
  async function runTask(taskText, options = {}) {
    if (!taskText.trim() || running.value) return

    running.value = true
    const id = ++taskId
    const startTime = Date.now()

    // 创建任务记录（先加进列表，实时更新）
    // 必须用 reactive() 包裹：push 进数组后 Vue 存的是原始对象，
    // 直接用闭包里的裸对象改字段不会触发渲染（流式 token 就不会逐字出现）。
    const task = reactive({
      id,
      task:      taskText,
      steps:     [],         // 工具调用步骤数组
      answer:    '',         // 最终回答
      // 过程说明：调用工具之前模型吐出来的话（"我先查一下…"）。
      // 不单独存的话会被当成回答拼进最终结果，出现 "…report.Let me get more…" 这种串行文本。
      narration: '',
      status:    'running',  // running | done | error
      // 是否因为"步数用尽"被强制收尾（界面上要提示回答可能不完整）
      maxStepsReached: false,
      // 意图路由与知识库引用（Agent 已与知识库打通）
      intent:    null,
      sources:   [],
      // 召回诊断：没命中时说明原因（库空 / 被权限过滤 / 分数低于阈值）
      recall:    null,
      startTime: new Date().toISOString(),
      duration:  0,
    })

    // 新任务追加在**末尾**：任务列表是"按时间往下读"的执行流水，
    // 新的在上面会让刚发起的那次任务顶到最上面，得回头往下找历史，
    // 也不符合"输入框在左上、结果往下滚"的阅读顺序。
    tasks.value.push(task)
    currentTask.value = task

    await fetchStream(
      '/api/agent/run',
      // useKnowledge: undefined=自动（后端默认"召回优先"）/ true=强制检索 / false=关闭
      // sessionId: 后端按会话保存记忆（摘要 + 最近 10 轮），下次任务能接着说
      { task: taskText, useKnowledge: options.useKnowledge, sessionId: sessionId.value },
      {
        onToken: (token) => {
          task.answer += token
        },

        onEvent: (event, data) => {
          if (event === 'start') {
            task.status = 'running'
          }

          // LangGraph 意图分类结果：knowledge / tool / chat
          if (event === 'intent') {
            task.intent = data
          }

          // 知识库引用（后端已按当前身份过滤）
          if (event === 'sources') {
            task.sources  = data.sources || []
            task.recall   = data.recall || null
          }

          // 后端的权威判定：刚刚流式吐出来的内容其实是"过程说明"（这条模型消息紧接着要调工具），
          // 不是最终回答 —— 挪到 narration，避免它冒充答案。
          // 为什么不能只靠下面的 tool_call 事件：步数用尽时会强制走收尾分支，
          // 那一步的 tool_calls 不会执行，也就不会再有 tool_call 事件（真实踩过：
          // 用户看到的"最终回答"是模型一句 "I have enough information now..."）。
          if (event === 'answer_reset') {
            const text = task.answer.trim()
            if (text) {
              task.narration = task.narration ? task.narration + '\n' + text : text
              task.answer = ''
            }
          }

          // 工具被调用：记录步骤
          if (event === 'tool_call') {
            // 走到"要调工具"这一步，说明前面已经吐出来的内容只是过程说明，不是最终回答。
            // 把它挪到 narration，避免和最终回答拼在一起。
            if (task.answer.trim()) {
              task.narration = task.narration ? task.narration + '\n' + task.answer.trim() : task.answer.trim()
              task.answer = ''
            }
            task.steps.push({
              id:       task.steps.length + 1,
              toolName: data.toolName,
              label:    data.label,
              args:     data.args,
              result:   null,
              status:   'running',   // running | done
              startMs:  Date.now(),
            })
          }

          // 工具执行完毕：更新最后一个 running 步骤
          if (event === 'tool_result') {
            const step = [...task.steps].reverse().find(s => s.toolName === data.toolName && s.status === 'running')
            if (step) {
              step.result    = data.resultText
              step.status    = 'done'
              step.durationMs = Date.now() - step.startMs
            }
          }

          if (event === 'done') {
            task.status   = 'done'
            task.duration = Date.now() - startTime
            // 步数用尽时后端会强制收尾，回答是基于已有信息的总结，要在界面上说明
            task.maxStepsReached = !!data.maxStepsReached
            currentTask.value = null
          }

          if (event === 'error') {
            task.status = 'error'
            task.answer = task.answer || data.message || '任务执行失败'
            currentTask.value = null
            appStore.toast.error(data.message || '执行出错')
          }
        },

        onDone: () => {
          task.status   = 'done'
          task.duration = Date.now() - startTime
          currentTask.value = null
        },

        onError: (err) => {
          task.status = 'error'
          task.answer = task.answer || '网络错误，请重试'
          currentTask.value = null
          appStore.toast.error(err.message)
        },
      }
    )

    running.value = false
  }

  async function clearTasks() {
    tasks.value = []
    currentTask.value = null
    // 清空任务列表 = 开一段新对话：顺手把后端的会话记忆也清掉，
    // 否则下一个任务还会"记得"清空之前聊过的东西
    try { await http.delete('/chat/sessions/' + sessionId.value) } catch { /* 清不掉不影响使用 */ }
    sessionId.value = newSessionId()
    localStorage.setItem(SESSION_KEY, sessionId.value)
  }

  return {
    toolList, examples, sessionId,
    tasks, running, currentTask,
    loadMeta, runTask, clearTasks,
  }
})
