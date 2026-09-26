// frontend/src/stores/prompt.js
// Prompt 调试模块状态：单次测试、A/B 对比、模板管理
import { defineStore } from 'pinia'
import { ref, reactive } from 'vue'
import { fetchStream } from '@/utils/http.js'
import http from '@/utils/http.js'
import { useAppStore } from './app.js'

export const usePromptStore = defineStore('prompt', () => {
  const appStore = useAppStore()

  // ── 单次测试状态 ────────────────────────────────────────────
  const testConfig = reactive({
    systemPrompt: '',
    userMessage:  '',
    temperature:  0.7,
    maxTokens:    1000,
  })

  const testResult = reactive({
    content:      '',
    streaming:    false,
    latencyMs:    0,
    inputTokens:  0,
    outputTokens: 0,
    totalTokens:  0,
    costCNY:      0,
  })

  const testing = ref(false)

  async function runTest() {
    if (!testConfig.userMessage.trim() || testing.value) return
    testing.value      = true
    testResult.content  = ''
    testResult.streaming = true

    const startMs = Date.now()

    await fetchStream(
      '/api/prompt/test/stream',
      {
        systemPrompt: testConfig.systemPrompt,
        userMessage:  testConfig.userMessage,
        temperature:  testConfig.temperature,
        maxTokens:    testConfig.maxTokens,
      },
      {
        onToken: (token) => { testResult.content += token },
        onEvent: (event, data) => {
          if (event === 'done') {
            testResult.streaming    = false
            testResult.latencyMs    = data.latencyMs || (Date.now() - startMs)
            testResult.inputTokens  = data.inputTokens  || 0
            testResult.outputTokens = data.outputTokens || 0
            testResult.totalTokens  = data.totalTokens  || 0
            testResult.costCNY      = data.costCNY || 0
          }
        },
        onDone: () => {
          testResult.streaming = false
          testing.value = false
        },
        onError: (err) => {
          testResult.streaming = false
          testing.value = false
          appStore.toast.error(err.message || '测试失败')
        },
      }
    )

    testing.value = false
  }

  // ── A/B 测试状态 ────────────────────────────────────────────
  // 预置一组默认内容，打开页面直接点「开始对比」就能出结果 ——
  // 面试官翻作品集时不用先自己想问题、也不用先写 prompt。
  const AB_SAMPLE = {
    question:      '学习一个新知识最快的方法是什么',
    systemPromptA: '输出格式：\n1.xxx\n2.xxx\n3.xxx',
    systemPromptB: '输出格式：\n大白话解释',
  }
  const abConfig = reactive({
    question:      AB_SAMPLE.question,
    systemPromptA: AB_SAMPLE.systemPromptA,
    systemPromptB: AB_SAMPLE.systemPromptB,
    temperature:   0,
    maxTokens:     800,
  })

  const abResult = reactive({
    answerA:    '',
    answerB:    '',
    evaluation: null,  // { scoreA, scoreB, winner, reason }
  })

  const abTesting = ref(false)
  // 生成完了、正在跑评分（评分要读完整答案，是 A/B 里最后一段等待）
  const abScoring = ref(false)

  async function runAbTest() {
    if (!abConfig.question.trim() || abTesting.value) return
    abTesting.value = true
    abResult.answerA    = ''
    abResult.answerB    = ''
    abResult.evaluation = null

    // 流式：两个变体并行生成，token 各自追加到自己那一列（以前是等 5 次调用全跑完才出结果）
    await fetchStream(
      '/api/prompt/ab-test/stream',
      {
        question:      abConfig.question,
        systemPromptA: abConfig.systemPromptA,
        systemPromptB: abConfig.systemPromptB,
        temperature:   abConfig.temperature,
        maxTokens:     abConfig.maxTokens,
      },
      {
        onEvent: (event, data) => {
          if (event === 'token') {
            if (data.variant === 'a') abResult.answerA += data.token
            else abResult.answerB += data.token
          }
          if (event === 'variant_error') {
            appStore.toast.error(`变体 ${data.variant.toUpperCase()} 生成失败：${data.message}`)
          }
          if (event === 'scoring') {
            abScoring.value = true
          }
        },
        onDone: (data) => {
          // done 事件带着最终的答案与评分
          abResult.answerA    = data?.answerA || abResult.answerA
          abResult.answerB    = data?.answerB || abResult.answerB
          abResult.evaluation = data?.evaluation || null
          abTesting.value = false
          abScoring.value = false
        },
        onError: (err) => {
          abTesting.value = false
          abScoring.value = false
          appStore.toast.error(err.message || 'A/B 测试失败，请重试')
        },
      },
    )
  }

  // ── 模板管理 ────────────────────────────────────────────────
  const templates    = ref([])
  const editingId    = ref('')   // 正在编辑的模板 ID（空=新建）

  async function loadTemplates() {
    try {
      const data = await http.get('/prompt/templates')
      templates.value = data.templates
    } catch {}
  }

  // 把某个模板加载到测试区
  function applyTemplate(template) {
    testConfig.systemPrompt = template.systemPrompt
    appStore.toast.success(`已加载模板「${template.name}」`)
  }

  // 把 A 或 B 区的 Prompt 加载到模板
  function applyAbTemplate(side, template) {
    if (side === 'A') abConfig.systemPromptA = template.systemPrompt
    else              abConfig.systemPromptB = template.systemPrompt
    appStore.toast.success(`已将「${template.name}」加载到 ${side} 区`)
  }

  async function saveTemplate(form) {
    try {
      const url = editingId.value
        ? `/prompt/templates/${editingId.value}`
        : '/prompt/templates'
      const method = editingId.value ? 'put' : 'post'
      await http[method](url, form)
      await loadTemplates()
      appStore.toast.success(editingId.value ? '模板已更新' : '模板已保存')
      editingId.value = ''
    } catch (err) {
      appStore.toast.error('保存失败')
    }
  }

  async function deleteTemplate(id) {
    try {
      await http.delete(`/prompt/templates/${id}`)
      await loadTemplates()
      appStore.toast.success('模板已删除')
    } catch (err) {
      appStore.toast.error(err.response?.data?.error?.message || '删除失败')
    }
  }

  // 把当前测试的 system prompt 快速另存为模板
  async function saveCurrentAsTemplate(name) {
    if (!testConfig.systemPrompt.trim()) {
      appStore.toast.warning('System Prompt 为空，无法保存')
      return
    }
    await saveTemplate({ name, systemPrompt: testConfig.systemPrompt })
  }

  return {
    testConfig, testResult, testing,
    abConfig, abResult, abTesting, abScoring,
    templates, editingId,
    runTest, runAbTest,
    loadTemplates, applyTemplate, applyAbTemplate,
    saveTemplate, deleteTemplate, saveCurrentAsTemplate,
  }
})
