import { createRouter, createWebHistory } from 'vue-router'
import AppLayout from '../layouts/AppLayout.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      component: AppLayout,
      children: [
        {
          path: '',
          component: () => import('../views/HomeView.vue'),
          meta: { title: '首页' }
        },
        {
          path: 'assistant',
          component: () => import('../views/AssistantView.vue'),
          meta: { title: '智能对话' }
        },
        {
          path: 'database',
          component: () => import('../views/DatabaseView.vue'),
          meta: { title: '数据库管理' }
        },
        {
          path: 'knowledge',
          component: () => import('../views/KnowledgeView.vue'),
          meta: { title: '知识库管理' }
        },
        {
          path: 'profile',
          component: () => import('../views/ProfileView.vue'),
          meta: { title: '个人中心' }
        },
        {
          path: 'settings',
          component: () => import('../views/SettingsView.vue'),
          meta: { title: '系统设置' }
        }
      ]
    }
  ]
})

export default router
