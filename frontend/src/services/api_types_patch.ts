import type { ChunkingStrategy, ChunkLevel } from './api'


export interface ChunkingConfig {
  strategy: ChunkingStrategy
  use_token_based: boolean
  base_chunk_tokens: number
  overlap_tokens: number
  min_semantic_tokens: number
  max_semantic_tokens: number
  base_chunk_size: number
  chunk_overlap: number
  semantic_threshold: number
  min_semantic_chunk: number
  max_semantic_chunk: number
  enable_hierarchical: boolean
  hierarchy_levels: ChunkLevel[]
  detect_academic_structure: boolean
  preserve_citations: boolean
  breakpoint_percentile: number
}

export interface ChunkingConfigResponse extends ChunkingConfig {
  id?: number
  user_id?: number
  name?: string
  is_default: boolean
  created_at?: string
  updated_at?: string
}

export interface PresetDescription {
  name: string
  description: string
  strategy: string
  recommended_for: string[]
}

export interface ChunkMetadata {
  level: ChunkLevel
  section_type?: string
  section_title?: string
  parent_id?: string
  child_ids: string[]
  has_citations: boolean
  position_ratio: number
  keywords: string[]
  token_count?: number
}

export interface SmartChunk {
  id: string
  content: string
  start_char: number
  end_char: number
  metadata: ChunkMetadata
}

export interface ChunkingStats {
  total_chunks: number
  total_chars: number
  total_tokens?: number
  avg_chunk_size: number
  min_chunk_size: number
  max_chunk_size: number
  avg_chunk_tokens?: number
  min_chunk_tokens?: number
  max_chunk_tokens?: number
  chunks_with_citations: number
}

export interface ChunkingResult {
  strategy: string
  chunks: SmartChunk[]
  hierarchy?: Record<string, Array<Record<string, unknown>>>
  metadata: Record<string, unknown>
  stats: ChunkingStats
}

export interface DocumentAnalysis {
  is_academic: boolean
  detected_sections: Array<{
    title: string
    type: string
    start: number
    end: number
    length: number
  }>
  has_citations: boolean
  recommended_strategy: string
  recommended_reason: string
  document_stats: {
    total_chars: number
    total_tokens?: number
    total_sentences: number
    total_paragraphs: number
    avg_sentence_length: number
    section_count: number
  }
  estimated_chunks?: number
  language?: string
}

