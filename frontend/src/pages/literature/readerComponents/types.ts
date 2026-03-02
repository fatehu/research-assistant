import type { z } from 'zod'

export type ReaderComponentSchema = z.ZodTypeAny

export type ReaderComponentRegistryEntry = {
  name: string
  schema: ReaderComponentSchema
}

