<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { api, ApiError } from '@/api'
import DeploymentCard, { type Deployment } from '@/components/deployments/DeploymentCard.vue'
import DeployModal, {
  type DeployForm
} from '@/components/deployments/DeployModal.vue'
import { useCache } from '@/composables/useCache'

interface StatusResponse {
  deployments: Record<string, Deployment>
  cluster?: unknown
}

const loading = ref(true)
const error = ref<string | null>(null)
const deployments = ref<Deployment[]>([])

type LevelFilter = 'ALL' | 'HOT' | 'WARM' | 'COLD' | 'PINNED'
const levelFilter = ref<LevelFilter>('ALL')
const search = ref('')
const sortBy = ref<'level' | 'repo_id' | 'n_params'>('level')

const deployModalOpen = ref(false)
const deployModalInitial = ref<Partial<DeployForm> | undefined>(undefined)
const deploying = ref(false)
const deployError = ref<string | null>(null)
const { cache, refresh: loadCache } = useCache()

// In-flight deploys, rendered as placeholder cards above the server list
// until a real deployment with matching (repo_id, revision) shows up. The
// key is `${repo_id}|${revision || ''}`.
interface PendingDeploy {
  key: string
  repo_id: string
  revision: string | null
  startedAt: number
}
const pending = ref<PendingDeploy[]>([])
const PENDING_TTL_MS = 5 * 60 * 1000

function pendingKey(repo_id: string, revision: string | null | undefined): string {
  return `${repo_id}|${revision || ''}`
}

// model_key -> "restart" | "evict" while a per-card action is in flight.
const cardBusy = ref<Record<string, string>>({})
const toast = ref<{ kind: 'ok' | 'err'; msg: string } | null>(null)

function showToast(kind: 'ok' | 'err', msg: string, ttl = 4000) {
  toast.value = { kind, msg }
  setTimeout(() => {
    toast.value = null
  }, ttl)
}

async function load() {
  loading.value = true
  try {
    const data = await api.get<StatusResponse>('/api/status')
    const arr = Object.values(data.deployments || {}).filter((d) => d.repo_id || d.model_key)
    deployments.value = arr

    // Drop pending placeholders only when the deployment is *visibly active*
    // (HOT or WARM) — keeping them for COLD entries means a deploy that
    // bounced off the controller as "already deployed" but never promoted to
    // HOT remains visible as a still-deploying card until TTL or until the
    // user retries. Without this, a no-op deploy silently looks like success.
    const now = Date.now()
    const live = new Set(
      arr
        .filter(
          (d) =>
            d.repo_id &&
            (d.deployment_level === 'HOT' || d.deployment_level === 'WARM')
        )
        .map((d) => pendingKey(d.repo_id as string, d.revision ?? null))
    )
    pending.value = pending.value.filter(
      (p) => !live.has(p.key) && now - p.startedAt < PENDING_TTL_MS
    )

    error.value = null
  } catch (e) {
    error.value =
      e instanceof ApiError
        ? typeof e.detail === 'string'
          ? e.detail
          : e.message
        : (e as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(load)

const counts = computed(() => {
  const out = { all: deployments.value.length, hot: 0, warm: 0, cold: 0, pinned: 0 }
  for (const d of deployments.value) {
    if (d.deployment_level === 'HOT') out.hot++
    else if (d.deployment_level === 'WARM') out.warm++
    else if (d.deployment_level === 'COLD') out.cold++
    if (d.pinned) out.pinned++
  }
  return out
})

const filtered = computed(() => {
  let rows = deployments.value.slice()

  if (levelFilter.value === 'PINNED') {
    rows = rows.filter((d) => d.pinned)
  } else if (levelFilter.value !== 'ALL') {
    rows = rows.filter((d) => d.deployment_level === levelFilter.value)
  }

  if (search.value.trim()) {
    const q = search.value.toLowerCase().trim()
    rows = rows.filter((d) => (d.repo_id || d.model_key).toLowerCase().includes(q))
  }

  if (sortBy.value === 'level') {
    const order = ['HOT', 'WARM', 'COLD']
    rows.sort((a, b) => {
      const al = order.indexOf(a.deployment_level || ''),
        bl = order.indexOf(b.deployment_level || '')
      const ai = al === -1 ? order.length : al
      const bi = bl === -1 ? order.length : bl
      if (ai !== bi) return ai - bi
      const ad = a.pinned ? 1 : 0,
        bd = b.pinned ? 1 : 0
      if (ad !== bd) return bd - ad
      return (a.repo_id || a.model_key).localeCompare(b.repo_id || b.model_key)
    })
  } else if (sortBy.value === 'repo_id') {
    rows.sort((a, b) =>
      (a.repo_id || a.model_key).localeCompare(b.repo_id || b.model_key)
    )
  } else if (sortBy.value === 'n_params') {
    rows.sort((a, b) => (b.n_params || 0) - (a.n_params || 0))
  }

  return rows
})

async function onDeploy(form: DeployForm) {
  deployError.value = null
  deploying.value = true

  // Optimistic placeholder — close the modal immediately and show a
  // pulsing card so the admin gets feedback before the controller responds
  // (deploy can take 10s+ for a model that's never been loaded).
  const placeholder: PendingDeploy = {
    key: pendingKey(form.checkpoint, form.revision),
    repo_id: form.checkpoint,
    revision: form.revision,
    startedAt: Date.now()
  }
  pending.value = [placeholder, ...pending.value.filter((p) => p.key !== placeholder.key)]
  deployModalOpen.value = false

  try {
    const resp = await api.post<any>('/api/deployments/deploy', form)
    // Inspect the structured result so we can surface partial failures the
    // controller didn't 500 over (e.g. CANT_ACCOMMODATE, FULL).
    const failed: string[] = []
    if (resp && Array.isArray(resp.deployments)) {
      for (const d of resp.deployments) {
        if (d.error) failed.push(`${d.checkpoint || d.model_key}: ${d.error}`)
      }
    }
    if (failed.length) {
      showToast('err', `Deploy issues — ${failed.join('; ')}`)
      pending.value = pending.value.filter((p) => p.key !== placeholder.key)
    } else {
      showToast(
        'ok',
        form.pinned
          ? `Deploying ${form.checkpoint} (pinned)…`
          : `Deploying ${form.checkpoint}…`
      )
    }
    await load()
    // Backend only writes the cache for entries whose deploy succeeded,
    // so refresh after every deploy attempt to pick up the new values.
    loadCache()
  } catch (e) {
    // Drop the placeholder on hard failure so the user isn't staring at
    // a permanent pulse for a deploy that never happened.
    pending.value = pending.value.filter((p) => p.key !== placeholder.key)
    deployError.value =
      e instanceof ApiError
        ? typeof e.detail === 'string'
          ? e.detail
          : e.message
        : (e as Error).message
    deployModalOpen.value = true
    showToast('err', `Deploy failed: ${deployError.value}`)
  } finally {
    deploying.value = false
  }
}

function openDeployModal(initial?: Partial<DeployForm>) {
  deployModalInitial.value = initial
  deployError.value = null
  deployModalOpen.value = true
}

function onCardDeploy(d: Deployment) {
  // WARM has a kebab → Deploy that should redeploy the existing model_key
  // as-is, no modal: every option the deploy modal would surface (actor_class,
  // envoy_class, pinned…) is already pinned by the existing deployment.
  // COLD keeps the modal so the user can override those before deploying.
  if (d.deployment_level === 'WARM') {
    onWarmDeploy(d)
  } else {
    openDeployModal({
      checkpoint: d.repo_id || '',
      revision: d.revision ?? null
    })
  }
}

async function onWarmDeploy(d: Deployment) {
  cardBusy.value = { ...cardBusy.value, [d.model_key]: 'deploy' }
  try {
    // model_key short-circuits cli/lib/deploy's get_model_key canonicalize.
    await api.post('/api/deployments/deploy', {
      specs: [
        {
          checkpoint: d.repo_id || d.model_key,
          revision: d.revision ?? null,
          model_key: d.model_key
        }
      ]
    })
    showToast('ok', `Redeploying ${d.repo_id || d.model_key}…`)
    await load()
  } catch (e) {
    showToast('err', `Redeploy failed: ${(e as Error).message}`)
  } finally {
    const next = { ...cardBusy.value }
    delete next[d.model_key]
    cardBusy.value = next
  }
}

async function onRestart(d: Deployment) {
  cardBusy.value = { ...cardBusy.value, [d.model_key]: 'restart' }
  try {
    // Backend now blocks until wait_for_model_ready and reports the
    // outcome via status: "restarted" | "timeout" | "error". Surface
    // those distinctly instead of a blanket "Restarted" toast.
    const res = await api.post<any>('/api/deployments/restart', {
      model_key: d.model_key,
      checkpoint: d.repo_id,
      revision: d.revision
    })
    const name = d.repo_id || d.model_key
    if (res?.status === 'restarted') {
      showToast('ok', `Restarted ${name}`)
    } else if (res?.status === 'timeout') {
      showToast('err', `Restart of ${name} timed out — actor still cold-loading`)
    } else {
      showToast('err', `Restart of ${name} failed: ${res?.error || 'unknown error'}`)
    }
    await load()
  } catch (e) {
    showToast('err', `Restart failed: ${(e as Error).message}`)
  } finally {
    const next = { ...cardBusy.value }
    delete next[d.model_key]
    cardBusy.value = next
  }
}

async function onEvict(d: Deployment) {
  cardBusy.value = { ...cardBusy.value, [d.model_key]: 'evict' }
  try {
    await api.post('/api/deployments/evict', {
      model_key: d.model_key,
      checkpoint: d.repo_id,
      revision: d.revision
    })
    showToast('ok', `Evicted ${d.repo_id || d.model_key}`)
    await load()
  } catch (e) {
    showToast('err', `Evict failed: ${(e as Error).message}`)
  } finally {
    const next = { ...cardBusy.value }
    delete next[d.model_key]
    cardBusy.value = next
  }
}
</script>

<template>
  <section class="deployments">
    <div class="row head">
      <h1 class="page-title">Deployments</h1>
      <div class="row">
        <button class="btn" @click="load" :disabled="loading">Refresh</button>
        <button class="btn primary" @click="openDeployModal()">+ Deploy</button>
      </div>
    </div>

    <div class="row toolbar">
      <div class="row chips">
        <button
          v-for="(opt, idx) in ([
            { key: 'ALL', label: 'All', count: counts.all, dot: null },
            { key: 'HOT', label: 'Hot', count: counts.hot, dot: 'hot' },
            { key: 'WARM', label: 'Warm', count: counts.warm, dot: 'warm' },
            { key: 'COLD', label: 'Cold', count: counts.cold, dot: 'cold' },
            { key: 'PINNED', label: 'Pinned', count: counts.pinned, dot: 'pinned' }
          ] as const)"
          :key="idx"
          :class="['chip', levelFilter === opt.key ? 'active ' + (opt.dot ?? '') : '']"
          @click="levelFilter = opt.key"
        >
          <span v-if="opt.dot" :class="['chip-dot', opt.dot]"></span>
          {{ opt.label }}
          <span class="chip-count">{{ opt.count }}</span>
        </button>
      </div>

      <div class="row right">
        <input
          v-model="search"
          class="search-input"
          placeholder="Search models..."
        />
        <select v-model="sortBy" class="sort-select">
          <option value="level">Sort: Status</option>
          <option value="repo_id">Sort: Name</option>
          <option value="n_params">Sort: Params</option>
        </select>
      </div>
    </div>

    <div v-if="error" class="card error-card">{{ error }}</div>

    <div v-if="loading && !deployments.length" class="card muted center">Loading...</div>

    <div v-else-if="filtered.length === 0" class="card muted center">
      No deployments match your filters.
    </div>

    <div v-else class="grid cards">
      <DeploymentCard
        v-for="p in pending"
        :key="'pending:' + p.key"
        :deployment="{ model_key: 'pending:' + p.key, repo_id: p.repo_id, revision: p.revision, pending: true }"
      />
      <DeploymentCard
        v-for="d in filtered"
        :key="d.model_key"
        :deployment="d"
        :busy="!!cardBusy[d.model_key]"
        @restart="onRestart"
        @evict="onEvict"
        @deploy="onCardDeploy"
      />
    </div>

    <DeployModal
      v-if="deployModalOpen"
      :initial="deployModalInitial"
      :saving="deploying"
      :error="deployError"
      :cache="cache"
      @submit="onDeploy"
      @close="deployModalOpen = false"
    />

    <div v-if="toast" :class="['toast', toast.kind]">{{ toast.msg }}</div>
  </section>
</template>

<style scoped>
.deployments {
  display: grid;
  gap: 1rem;
}
.head {
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
}
.page-title {
  font-family: 'VT323', monospace;
  font-weight: normal;
  font-size: 1.6rem;
  letter-spacing: 0.06em;
}

.toolbar {
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.75rem;
}
.chips {
  display: flex;
  gap: 0.35rem;
  flex-wrap: wrap;
}
.chip {
  appearance: none;
  background: transparent;
  border: 1px solid var(--border);
  color: var(--muted);
  padding: 0.35rem 0.75rem;
  font-family: 'Space Mono', monospace;
  font-size: 0.75rem;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
}
.chip:hover { color: var(--text); }
.chip.active { color: var(--text); border-color: var(--text); }
.chip.active.hot { color: var(--green); border-color: var(--green); }
.chip.active.warm { color: var(--amber); border-color: var(--amber); }
.chip.active.cold { color: var(--blue); border-color: var(--blue); }
.chip.active.pinned { color: var(--accent); border-color: var(--accent); }
.chip-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}
.chip-dot.hot { background: var(--green); }
.chip-dot.warm { background: var(--amber); }
.chip-dot.cold { background: var(--blue); }
.chip-dot.pinned { background: var(--accent); }
.chip-count {
  opacity: 0.6;
  font-size: 0.7rem;
}

.right { gap: 0.5rem; }
.search-input,
.sort-select {
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 0.45rem 0.6rem;
  font-family: 'Space Mono', monospace;
  font-size: 0.78rem;
}
.search-input:focus,
.sort-select:focus {
  outline: none;
  border-color: var(--accent);
}
.search-input { min-width: 220px; }

.cards {
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 0.75rem;
}

.error-card { color: var(--red); }
.center { text-align: center; padding: 2rem 1rem; }

.toast {
  position: fixed;
  bottom: 1.5rem;
  right: 1.5rem;
  background: var(--surface);
  border: 1px solid var(--border);
  padding: 0.6rem 0.9rem;
  font-family: 'Space Mono', monospace;
  font-size: 0.85rem;
  z-index: 200;
  max-width: 60ch;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
}
.toast.ok { border-color: var(--green); color: var(--green); }
.toast.err { border-color: var(--red); color: var(--red); }
</style>
