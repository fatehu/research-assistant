import { z } from 'zod'

const stringArray = z.array(z.string()).default([])
const recordArray = z.array(z.record(z.any())).default([])

export const readerComponentSchemas = {
  PaperHeaderCard: z.object({
    title: z.string().min(1),
    venue: z.string().optional(),
    year: z.union([z.string(), z.number()]).optional(),
    authors: stringArray.optional(),
  }),
  MetadataSidebarCard: z.object({
    items: recordArray.optional(),
  }),
  SectionTOC: z.object({
    items: recordArray.optional(),
  }),
  SectionHeading: z.object({
    text: z.string().min(1),
    level: z.number().optional(),
  }),
  Separator: z.object({
    label: z.string().optional(),
    tone: z.enum(['default', 'muted', 'strong']).optional(),
  }),
  ParagraphProse: z.object({
    text: z.string().min(1),
    paragraphs: z.array(z.any()).optional(),
  }),
  ListBlock: z.object({
    items: stringArray,
  }),
  FigurePanel: z.object({
    caption: z.string().optional(),
    image_url: z.string().optional(),
    source_label: z.string().optional(),
    ai_insight: z.string().optional(),
  }),
  TablePanel: z.object({
    title: z.string().optional(),
    rows: recordArray.optional(),
    ai_insight: z.string().optional(),
  }),
  CitationLinks: z.object({
    links: recordArray.optional(),
  }),
  KeyTakeaways: z.object({
    items: z.array(z.any()).optional(),
  }),
  AnnotationRail: z.object({
    items: stringArray.optional(),
  }),
  QualityBadge: z.object({}),
  QualityPanel: z.object({}),
  InlineQuerySlot: z.object({
    placeholder: z.string().optional(),
  }),
  AnswerCard: z.object({
    question: z.string().min(1),
    answer: z.string().min(1),
    foldable: z.boolean().optional(),
  }),
  CompareInsightsCard: z.object({
    items: recordArray.optional(),
  }),
  PdfSnippetCard: z.object({
    title: z.string().optional(),
    description: z.string().optional(),
    page: z.union([z.string(), z.number()]).optional(),
  }),
  ContextRail: z.object({
    title: z.string().optional(),
    items: z.array(z.any()).optional(),
    default_collapsed: z.boolean().optional(),
  }),
  CitationCard: z.object({
    citation_key: z.string().optional(),
    authors: z.array(z.string()).optional(),
    year: z.union([z.string(), z.number()]).optional(),
    title: z.string().min(1),
    journal: z.string().optional(),
    doi: z.string().optional(),
    abstract_tldr: z.string().optional(),
  }),
  EquationBlock: z.object({
    latex: z.string().min(1),
    label: z.string().optional(),
    description: z.string().optional(),
  }),
  MethodologyCard: z.object({
    title: z.string().optional(),
    steps: z.array(z.string().min(1)).min(1),
    participants: z.string().optional(),
    tools: z.array(z.string()).optional(),
  }),
  CalloutBox: z.object({
    type: z.enum(['info', 'warning', 'success', 'tip']).default('info'),
    title: z.string().optional(),
    content: z.string().min(1),
  }),
  AbstractCard: z.object({
    text: z.string().min(1),
  }),
} as const

export type ReaderRegisteredComponentName = keyof typeof readerComponentSchemas

