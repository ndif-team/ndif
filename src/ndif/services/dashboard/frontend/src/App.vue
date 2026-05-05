<script setup lang="ts">
import { onMounted, computed } from 'vue'
import { useRoute, useRouter, RouterLink, RouterView } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const showChrome = computed(() => route.name !== 'login')

onMounted(async () => {
  if (!auth.checked) await auth.refresh()
})

async function logout() {
  await auth.logout()
  router.push({ name: 'login' })
}

function toggleTheme() {
  const html = document.documentElement
  const next = html.dataset.theme === 'dark' ? 'light' : 'dark'
  html.dataset.theme = next
  localStorage.setItem('theme', next)
}

const stored = localStorage.getItem('theme')
if (stored === 'light' || stored === 'dark') {
  document.documentElement.dataset.theme = stored
}
</script>

<template>
  <div class="scanlines"></div>
  <div class="noise"></div>

  <div class="wrap">
    <header v-if="showChrome" class="app-header">
      <div class="row">
        <span class="logo"><img src="/ndif-logo.png" class="logo-mark" alt="" />NDIF</span>
        <nav class="tabs">
          <RouterLink to="/monitor">Monitor</RouterLink>
          <RouterLink to="/deployments">Deployments</RouterLink>
          <RouterLink to="/schedule">Schedule</RouterLink>
        </nav>
      </div>
      <div class="row">
        <span v-if="auth.username" class="muted">{{ auth.username }}</span>
        <button class="btn" @click="toggleTheme">Theme</button>
        <button class="btn" @click="logout" v-if="auth.username">Logout</button>
      </div>
    </header>

    <RouterView />
  </div>
</template>
