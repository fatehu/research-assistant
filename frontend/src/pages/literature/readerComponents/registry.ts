import { readerComponentSchemas, type ReaderRegisteredComponentName } from './schemas'
import type { ReaderComponentRegistryEntry } from './types'

const registryEntries: ReaderComponentRegistryEntry[] = (
  Object.keys(readerComponentSchemas) as ReaderRegisteredComponentName[]
).map((name) => ({
  name,
  schema: readerComponentSchemas[name],
}))

export const readerComponentRegistry = new Map<string, ReaderComponentRegistryEntry>(
  registryEntries.map((entry) => [entry.name, entry]),
)

export function isRegisteredReaderComponent(componentType: string): boolean {
  return readerComponentRegistry.has(String(componentType || '').trim())
}

export function validateReaderComponentProps(
  componentType: string,
  props: unknown,
): { ok: true; props: Record<string, unknown> } | { ok: false; error: string } {
  const key = String(componentType || '').trim()
  const entry = readerComponentRegistry.get(key)
  if (!entry) {
    return { ok: false, error: `component_not_registered:${key}` }
  }
  const parsed = entry.schema.safeParse(props && typeof props === 'object' ? props : {})
  if (!parsed.success) {
    const issue = parsed.error.issues[0]
    return {
      ok: false,
      error: `component_props_invalid:${key}:${issue?.path?.join('.') || 'root'}:${issue?.message || 'invalid'}`,
    }
  }
  return { ok: true, props: parsed.data as Record<string, unknown> }
}

