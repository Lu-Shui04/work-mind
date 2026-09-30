<!-- frontend/src/views/ChatView.vue -->
<!-- 对话页面：三栏布局 = 会话列表 | 消息区 | 用户画像 -->
<template>
  <div class="chat-view">
    <!-- 左：会话列表 -->
    <SessionSidebar />

    <!-- 中：消息区域 -->
    <div class="chat-main">
      <!-- 检索范围预设 + 会话操作 -->
      <div class="chat-toolbar">
        <ScopeSelector />
        <button class="clear-btn" :disabled="!chatStore.messages.length" @click="clearSession"
                title="清空当前会话（同时清除服务端上下文）">
          🧹 清空会话
        </button>
      </div>

      <!-- 消息列表 -->
      <div class="message-list" ref="listEl">
        <!-- 空状态 -->
        <div v-if="!chatStore.messages.length" class="empty-state">
          <el-icon class="role-icon"><component :is="roleIcon" /></el-icon>
          <div class="title">智能助手</div>
          <div class="desc">
            回答优先依据企业知识库，并标注来源；知识库里没有的内容会明确说明，不会用通用知识顶上。
            <br />检索不到时可以在输入框下方切「知识库：关闭」，或在上面收窄/放宽检索范围。
          </div>
          <!-- 快捷问题 -->
          <div class="quick-questions">
            <button
              v-for="q in quickQuestions"
              :key="q"
              class="quick-btn"
              @click="sendQuick(q)"
            >
              {{ q }}
            </button>
          </div>
        </div>

        <!-- 消息列表 -->
        <MessageBubble
          v-for="msg in chatStore.messages"
          :key="msg.id"
          :message="msg"
        />

        <!-- 底部锚点，用于滚动到底 -->
        <div ref="bottomEl" />
      </div>

      <!-- 输入区 -->
      <ChatInput />
    </div>

    <!-- 右：用户画像（可折叠） -->
    <ProfilePanel v-if="showProfile" />

    <!-- 折叠/展开画像按钮 -->
    <button class="profile-toggle" @click="showProfile = !showProfile" :title="showProfile ? '收起画像' : '展开画像'">
      {{ showProfile ? '›' : '‹' }}
    </button>
  </div>
</template>

<script setup>
import { ref, watch, onMounted, nextTick } from 'vue'
import { useChatStore } from '@/stores/chat.js'
import SessionSidebar from '@/components/chat/SessionSidebar.vue'
import ScopeSelector from '@/components/chat/ScopeSelector.vue'
import MessageBubble from '@/components/chat/MessageBubble.vue'
import ChatInput from '@/components/chat/ChatInput.vue'
import ProfilePanel from '@/components/chat/ProfilePanel.vue'

const chatStore  = useChatStore()
const listEl     = ref(null)
const bottomEl   = ref(null)
const showProfile = ref(true)

const roleIcon = 'ChatDotRound'

// 快捷问题：围绕"员工日常要问公司制度"这条主线，让第一次点进来的人立刻看到差异。
// 三条都对应知识库里真实存在的制度（年假与休假政策 / 差旅与报销管理制度 / 员工手册），
// 点一下就能看到"带引用作答"是什么样；问库里没有的东西会明确说没查到。
const quickQuestions = [
  '年假有多少天？',
  '出差住宿标准是多少？',
  '报销单要在多久内提交？',
]

// 检索范围的反馈由 ScopeSelector 自己显示（选中态 + 右侧那句说明），
// 不再额外弹 toast —— 范围只影响之后的消息，弹窗反而是噪音。

function sendQuick(q) {
  chatStore.sendMessage(q)
}

// 清空当前会话：本地消息 + 服务端历史一起清，避免"清空了但模型还记得"
async function clearSession() {
  if (!chatStore.messages.length) return
  if (!confirm('清空当前会话的全部消息？服务端上下文也会一并清除。')) return
  await chatStore.clearCurrentSession()
}

// 新消息到来时自动滚到底部
watch(
  () => chatStore.messages.length,
  async () => {
    await nextTick()
    bottomEl.value?.scrollIntoView({ behavior: 'smooth' })
  }
)

// 流式 token 追加时也滚底
watch(
  () => {
    const msgs = chatStore.messages
    return msgs[msgs.length - 1]?.content
  },
  async () => {
    await nextTick()
    if (chatStore.loading) {
      bottomEl.value?.scrollIntoView({ behavior: 'instant' })
    }
  }
)

onMounted(() => {
  chatStore.init()
  // 角色预设已合并成一个助手，不再需要拉取角色列表
  chatStore.loadProfile()
})
</script>

<style scoped>
.chat-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--space-md); padding-right: var(--space-md);
}
.clear-btn {
  border: 1px solid var(--color-border); background: var(--color-surface);
  color: var(--color-text-sub); font-size: 12px; cursor: pointer;
  padding: 4px 10px; border-radius: var(--radius-md); transition: var(--transition);
}
.clear-btn:hover:not(:disabled) { border-color: var(--color-danger); color: var(--color-danger); }
.clear-btn:disabled { opacity: .45; cursor: not-allowed; }

.chat-view {
  display: flex;
  height: 100%;
  overflow: hidden;
  background: var(--color-bg);
  position: relative;
}

.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}

.message-list {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-lg) var(--space-xl);
  display: flex;
  flex-direction: column;
  gap: var(--space-md);
}

/* 空状态 */
.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-sm);
  padding: var(--space-2xl);
  color: var(--color-text-muted);
  text-align: center;
}

.role-icon { font-size: 48px; color: var(--color-primary); margin-bottom: var(--space-md); }
.empty-state .title { font-size: 18px; font-weight: 600; color: var(--color-text); }
.empty-state .desc  { font-size: 13px; color: var(--color-text-sub); margin-bottom: var(--space-lg); }

.quick-questions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  justify-content: center;
  max-width: 560px;
}

.quick-btn {
  padding: 8px 16px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  font-size: 12px;
  color: var(--color-text-sub);
  cursor: pointer;
  transition: all var(--transition);
}
.quick-btn:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: var(--color-primary-bg);
}

/* 画像折叠按钮 */
.profile-toggle {
  position: absolute;
  right: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 18px;
  height: 48px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-right: none;
  border-radius: var(--radius-sm) 0 0 var(--radius-sm);
  color: var(--color-text-muted);
  font-size: 12px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10;
  transition: all var(--transition);
}
.profile-toggle:hover { color: var(--color-primary); }
</style>
