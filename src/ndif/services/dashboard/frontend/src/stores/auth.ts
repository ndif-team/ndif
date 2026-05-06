import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, ApiError } from '@/api'

interface MeResponse {
  username: string
  dev_mode: boolean
}

export const useAuthStore = defineStore('auth', () => {
  const username = ref<string | null>(null)
  const devMode = ref(false)
  const checked = ref(false)

  async function refresh(): Promise<boolean> {
    try {
      const me = await api.get<MeResponse>('/api/auth/me')
      username.value = me.username
      devMode.value = me.dev_mode
      checked.value = true
      return true
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        username.value = null
        checked.value = true
        return false
      }
      throw e
    }
  }

  async function login(u: string, p: string) {
    await api.post<{ username: string }>('/api/auth/login', {
      username: u,
      password: p
    })
    await refresh()
  }

  async function logout() {
    try {
      await api.post('/api/auth/logout')
    } finally {
      username.value = null
    }
  }

  return { username, devMode, checked, refresh, login, logout }
})
