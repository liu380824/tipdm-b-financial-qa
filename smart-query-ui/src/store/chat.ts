import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { createWelcomeMessage } from '../data/ui'
import type { ChatMessage } from '../types'

export interface Conversation {
  id: string
  title: string
  messages: ChatMessage[]
}

const uid = () => `${Date.now()}-${Math.random().toString(36).slice(2, 6)}`

const makeConv = (): Conversation => ({
  id: uid(),
  title: '新对话',
  messages: [createWelcomeMessage()]
})

export const useChatStore = defineStore('chat', () => {
  const conversations = ref<Conversation[]>([makeConv()])
  const activeId = ref(conversations.value[0].id)

  const activeConv = computed(
    () => conversations.value.find(c => c.id === activeId.value) ?? conversations.value[0]
  )

  const newConversation = () => {
    const c = makeConv()
    conversations.value.unshift(c)
    activeId.value = c.id
  }

  const switchConversation = (id: string) => {
    activeId.value = id
  }

  const deleteConversation = (id: string) => {
    const i = conversations.value.findIndex(c => c.id === id)
    if (i < 0) return
    conversations.value.splice(i, 1)
    if (!conversations.value.length) newConversation()
    else if (activeId.value === id)
      activeId.value = conversations.value[Math.max(0, i - 1)].id
  }

  return { conversations, activeId, activeConv, newConversation, switchConversation, deleteConversation }
})
