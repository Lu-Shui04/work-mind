// frontend/src/stores/monitor.js
// 用量看板的全局状态：数据**全部来自后端** /api/monitor/stats（PostgreSQL 聚合）。
//
// 以前这里是个"前端自己算"的计数器：recordCall() 在浏览器里按 token 累加费用，
// 刷新页面就归零、只统计对话、别的模块（知识库/工作流/Agent/ERP）一概不算；
// 顶部那条"今日用量已达 …，请注意控制"的预警读的就是它，
// 所以**从来没出现过**——看板和后端各算各的，等于这个模块跟系统是断开的。
//
// 现在统一成一个来源：后端每次模型调用落一行 usage_calls，前端只负责读和展示。
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import http from '@/utils/http.js'

export const useMonitorStore = defineStore('monitor', () => {
  const overview = ref({})
  const latency = ref({})
  const byFeature = ref([])
  const last7Days = ref([])
  const recentCalls = ref([])
  const cacheStats = ref({})
  const loaded = ref(false)
  const error = ref('')

  const dailyBudget = computed(() => overview.value?.dailyBudget ?? 50)
  const todaySpend = computed(() => overview.value?.costCNYToday ?? 0)

  // 超过日预算 80% 时预警（顶部横幅用）
  const budgetWarning = computed(() => {
    const pct = overview.value?.budgetUsedPct ?? 0
    if (pct < 80) return null
    return '¥' + todaySpend.value.toFixed(2) + ' / ¥' + dailyBudget.value
  })

  function apply(d) {
    if (!d || typeof d !== 'object') return
    overview.value = d.overview || {}
    latency.value = d.latency || {}
    byFeature.value = d.byFeature || []
    last7Days.value = d.last7Days || []
    recentCalls.value = d.recentCalls || []
    cacheStats.value = d.cacheStats || {}
    loaded.value = true
    error.value = ''
  }

  async function refresh() {
    try {
      // silent：轮询失败不要每 15 秒弹一次 toast（后端没起来时尤其吵）
      const d = await http.get('/monitor/stats', { silent: true })
      apply(d)
      return d
    } catch (err) {
      error.value = err?.message || '看板数据加载失败'
      return null
    }
  }

  // 轮询：看板要"跟着系统走"，跑完一次对话回来就能看到新数字
  let timer = null
  function start(intervalMs = 15000) {
    if (timer) return
    refresh()
    timer = setInterval(refresh, intervalMs)
  }
  function stop() {
    if (timer) clearInterval(timer)
    timer = null
  }

  return {
    overview, latency, byFeature, last7Days, recentCalls, cacheStats,
    loaded, error, dailyBudget, todaySpend, budgetWarning,
    apply, refresh, start, stop,
  }
})
