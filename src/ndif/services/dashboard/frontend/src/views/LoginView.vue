<script setup lang="ts">
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ApiError } from '@/api'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const error = ref<string | null>(null)
const submitting = ref(false)

async function submit() {
  error.value = null
  submitting.value = true
  try {
    await auth.login(username.value, password.value)
    const next = (route.query.next as string) || '/monitor'
    router.push(next)
  } catch (e) {
    error.value =
      e instanceof ApiError && e.status === 401
        ? 'Invalid username or password'
        : 'Login failed'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-screen">
    <div class="login-card">
      <div class="logo"><img src="/ndif-logo.png" class="logo-mark" alt="" />NDIF</div>
      <p class="muted login-subtitle">SIGN IN</p>

      <form @submit.prevent="submit">
        <label class="field">
          USER
          <input
            v-model="username"
            autocomplete="username"
            autofocus
            required
          />
        </label>
        <label class="field">
          PASS
          <input
            v-model="password"
            type="password"
            autocomplete="current-password"
            required
          />
        </label>
        <p v-if="error" class="login-error">{{ error }}</p>
        <button class="btn primary login-submit" :disabled="submitting">
          {{ submitting ? '...' : 'ENTER' }}
        </button>
      </form>
    </div>
  </div>
</template>

<style scoped>
.login-screen {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 2rem;
}
.login-card {
  width: min(360px, 92vw);
  background: var(--surface);
  border: 1px solid var(--border);
  padding: 2rem 1.75rem;
}
.login-card .logo {
  font-size: 2.4rem;
}
.login-subtitle {
  margin: 0.25rem 0 1.5rem;
  letter-spacing: 0.18em;
  font-size: 0.75rem;
}
form {
  display: grid;
  gap: 0.9rem;
}
.login-error {
  color: var(--red);
  font-size: 0.8rem;
  letter-spacing: 0.05em;
}
.login-submit {
  margin-top: 0.5rem;
}
</style>
