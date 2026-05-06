// Shared cache state for the deploy/schedule autocomplete dropdowns.
//
// Both DeploymentsView and ScheduleView fetch /api/cache on mount and
// re-fetch after any operation that may have populated it (a successful
// ad-hoc deploy, a schedule write that triggers reconcile). They were
// each carrying their own ref + loadCache() before — same code in two
// places. This composable owns the state and the fetch.

import { onMounted, ref } from 'vue'
import { api } from '@/api'
import type { CacheValues } from '@/deploy'

const EMPTY: CacheValues = { repo_id: [], actor_class: [], envoy_class: [] }

export function useCache() {
  const cache = ref<CacheValues>({ ...EMPTY })

  async function refresh() {
    try {
      cache.value = await api.get<CacheValues>('/api/cache')
    } catch {
      // Non-fatal — autocomplete is a convenience, not required for
      // any deploy/schedule action to succeed.
    }
  }

  onMounted(refresh)

  return { cache, refresh }
}
