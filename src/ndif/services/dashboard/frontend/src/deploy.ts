// Shared types + constants for the deploy / schedule modals.
//
// Lives outside any `.vue` because Vue's `<script setup>` blocks
// non-type exports — re-defining the constant inline would force every
// view + modal to keep its own copy in sync.

export const DEFAULT_ENVOY_CLASS = 'nnsight.modeling.language.LanguageModel'

export interface CacheValues {
  repo_id: string[]
  actor_class: string[]
  envoy_class: string[]
}
