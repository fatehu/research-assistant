import { useMemo, useState, forwardRef } from 'react'
import { Button, Tooltip, Avatar, message } from 'antd'
import {
  RobotOutlined,
  UserOutlined,
  CopyOutlined,
  SearchOutlined,
  DatabaseOutlined,
  LinkOutlined,
  ThunderboltOutlined,
  DownOutlined,
  UpOutlined,
} from '@ant-design/icons'
import { AnimatePresence, motion } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { SHOW_RAG_METRICS, type Message, type RagMetrics, type ReactStep } from '@/services/api'
import CodeBlock from './CodeBlock'
import ThinkingPanel from './ThinkingPanel'
import HistoryReActPanel from './HistoryReActPanel'

interface MessageBubbleProps {
  msg: Message
  isStreaming?: boolean
  streamingContent?: string
  streamingThought?: string
  isThinking?: boolean
  isHighlighted?: boolean
}

interface KnowledgeEvidenceItem {
  id: string
  sourceLabel: string
  sourcePath: string
  retrievalScore?: string
  compressionScore?: string
  content: string
}

interface RagMetricCardItem {
  key: string
  label: string
  value: string
  icon: React.ReactNode
}

const parseRagMetrics = (value: unknown): RagMetrics | null => {
  if (!value || typeof value !== 'object') {
    return null
  }

  const metrics = value as Partial<RagMetrics>
  if (typeof metrics.knowledge_search_calls !== 'number') {
    return null
  }

  const normalized: RagMetrics = {
    knowledge_search_calls: metrics.knowledge_search_calls,
    source_labels_count: Number(metrics.source_labels_count || 0),
    source_labels: Array.isArray(metrics.source_labels) ? metrics.source_labels : [],
    answer_citation_count: Number(metrics.answer_citation_count || 0),
    citation_required: Boolean(metrics.citation_required),
    citation_valid: Boolean(metrics.citation_valid),
    citation_repair_attempts: Number(metrics.citation_repair_attempts || 0),
    citation_repair_successes: Number(metrics.citation_repair_successes || 0),
    compression_calls: Number(metrics.compression_calls || 0),
    compression_success_chunks: Number(metrics.compression_success_chunks || 0),
    compression_fallback_chunks: Number(metrics.compression_fallback_chunks || 0),
  }

  const ragUsed =
    normalized.knowledge_search_calls > 0 ||
    normalized.source_labels_count > 0 ||
    normalized.answer_citation_count > 0 ||
    normalized.compression_calls > 0 ||
    normalized.citation_repair_attempts > 0 ||
    normalized.citation_repair_successes > 0

  return ragUsed ? normalized : null
}

const normalizeEvidenceContent = (value: string): string =>
  value.replace(/^\[来源\d+\]\s*/i, '').replace(/\s+/g, ' ').trim()

const parseKnowledgeEvidence = (steps: ReactStep[] | undefined): KnowledgeEvidenceItem[] => {
  if (!Array.isArray(steps) || steps.length === 0) {
    return []
  }

  const items: KnowledgeEvidenceItem[] = []
  const seen = new Set<string>()

  for (const step of steps) {
    if (step.type !== 'observation' || step.tool !== 'knowledge_search' || !step.output) {
      continue
    }

    const output = String(step.output || '').trim()
    if (!output) continue

    const blockPattern =
      /\[(来源\d+)\]\s*\(retrieval score ([\d.]+)%\)\s*Source:\s*([^\n]+)\s*Compression score:\s*([\d.]+)\/10\s*Content:\s*([\s\S]*?)(?=\n\[来源\d+\]\s*\(retrieval score|\s*$)/g

    let matched = false
    let match: RegExpExecArray | null
    while ((match = blockPattern.exec(output)) !== null) {
      matched = true
      const sourceLabel = String(match[1] || '').trim()
      const sourcePath = String(match[3] || '').trim()
      const content = normalizeEvidenceContent(String(match[5] || ''))
      if (!content) continue
      const id = `${sourcePath}::${content}`
      if (seen.has(id)) continue
      seen.add(id)
      items.push({
        id,
        sourceLabel,
        sourcePath,
        retrievalScore: String(match[2] || '').trim(),
        compressionScore: String(match[4] || '').trim(),
        content,
      })
    }

    if (!matched) {
      const compact = normalizeEvidenceContent(output.replace(/^Compressed contexts:\s*\d+\s*/i, ''))
      if (!compact) continue
      const id = `raw::${compact}`
      if (seen.has(id)) continue
      seen.add(id)
      items.push({
        id,
        sourceLabel: '检索依据',
        sourcePath: 'knowledge_search',
        content: compact,
      })
    }
  }

  return items
}

/** 消息气泡 - 美化版 */
const MessageBubble = forwardRef<HTMLDivElement, MessageBubbleProps>(
  (
    {
      msg,
      isStreaming = false,
      streamingContent = '',
      isThinking = false,
      isHighlighted = false,
    },
    ref
  ) => {
    const isUser = msg.role === 'user'
    const content = isStreaming ? streamingContent : msg.content
    const thought = isStreaming ? '' : msg.thought
    const reactSteps = isStreaming ? undefined : msg.react_steps
    const [thoughtExpanded, setThoughtExpanded] = useState(false)
    const [ragExpanded, setRagExpanded] = useState(false)
    const [evidenceExpanded, setEvidenceExpanded] = useState(false)
    const ragMetrics = !isStreaming && !isUser ? parseRagMetrics(msg.metadata?.rag_metrics) : null
    const evidenceItems = useMemo(
      () => (!isStreaming && !isUser ? parseKnowledgeEvidence(reactSteps) : []),
      [isStreaming, isUser, reactSteps]
    )
    const ragMetricCards = useMemo<RagMetricCardItem[]>(
      () =>
        ragMetrics
          ? [
              {
                key: 'search',
                label: '检索调用',
                value: String(ragMetrics.knowledge_search_calls),
                icon: <SearchOutlined />,
              },
              {
                key: 'sources',
                label: '来源数',
                value: String(ragMetrics.source_labels_count),
                icon: <DatabaseOutlined />,
              },
              {
                key: 'citations',
                label: '答案引用',
                value: String(ragMetrics.answer_citation_count),
                icon: <LinkOutlined />,
              },
              {
                key: 'compression',
                label: '压缩调用',
                value: String(ragMetrics.compression_calls),
                icon: <ThunderboltOutlined />,
              },
            ]
          : [],
      [ragMetrics]
    )

    const handleCopy = () => {
      navigator.clipboard.writeText(content)
      message.success('已复制到剪贴板')
    }
    const timeLabel = new Date(msg.created_at).toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
    })

    const bubbleColumnClass = isUser
      ? 'flex min-w-0 flex-1 flex-col items-end'
      : 'flex min-w-0 flex-1 flex-col'
    const userBubbleShellClass = 'inline-flex max-w-[min(76%,720px)] flex-col items-end'
    const assistantBubbleWidthClass = 'w-full max-w-[min(100%,860px)]'
    const hasAssistantPrelude =
      !isUser && !isStreaming && ((reactSteps?.length ?? 0) > 0 || Boolean(thought))

    return (
      <motion.div
        ref={ref}
        id={`message-${msg.id}`}
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3, ease: 'easeOut' }}
        className={`flex items-start gap-3.5 ${isUser ? 'flex-row-reverse' : ''} ${
          isHighlighted ? 'relative' : ''
        }`}
      >
        {/* 高亮效果 */}
        {isHighlighted && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="absolute inset-0 -mx-4 -my-2 rounded-xl bg-emerald-500/10 border border-emerald-500/30 pointer-events-none"
            style={{ zIndex: -1 }}
          />
        )}

        {/* 头像 */}
        <div className="flex-shrink-0">
          {isUser ? (
            <Avatar
              size={30}
              icon={<UserOutlined />}
              className="border border-slate-700/60 bg-slate-800 text-white shadow-[0_10px_24px_rgba(2,6,23,0.14)]"
            />
          ) : (
            <div className="flex h-[30px] w-[30px] items-center justify-center rounded-full border border-emerald-400/18 bg-slate-900/72 shadow-[0_10px_24px_rgba(2,6,23,0.14)]">
              <RobotOutlined className="text-sm text-emerald-300" />
            </div>
          )}
        </div>

        {/* 内容区 */}
        <div className={bubbleColumnClass}>
          <div className={`mb-2 flex items-center gap-2 ${isUser ? 'justify-end' : ''}`}>
            <span className={`text-xs font-medium tracking-wide ${isUser ? 'text-slate-300' : 'text-emerald-300'}`}>
              {isUser ? '你' : 'AI 助手'}
            </span>
            <span className="text-xs text-slate-500">{timeLabel}</span>
          </div>

          {SHOW_RAG_METRICS && !isUser && !isStreaming && ragMetrics && (
            <div className="mb-3 flex w-full max-w-[min(100%,860px)] flex-col gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => setRagExpanded(!ragExpanded)}
                  className="inline-flex items-center gap-2 rounded-full border border-white/[0.08] bg-slate-900/75 px-3 py-1.5 text-xs font-medium text-slate-200 transition-colors hover:border-emerald-400/20 hover:text-white"
                >
                  <SearchOutlined className="text-[11px] text-emerald-300" />
                  检索质量
                  {ragExpanded ? <UpOutlined className="text-[10px]" /> : <DownOutlined className="text-[10px]" />}
                </button>
                <span
                  className={`rounded-full border px-2.5 py-1 text-[11px] ${
                    ragMetrics.citation_valid
                      ? 'border-emerald-400/20 bg-emerald-500/10 text-emerald-200'
                      : 'border-amber-400/20 bg-amber-500/10 text-amber-200'
                  }`}
                >
                  {ragMetrics.citation_valid ? '引用有效' : '引用待修正'}
                </span>
                {ragMetricCards.map((item) => (
                  <span
                    key={item.key}
                    className="inline-flex items-center gap-1.5 rounded-full border border-white/[0.08] bg-slate-900/65 px-2.5 py-1 text-[11px] text-slate-300"
                  >
                    <span className="text-emerald-300">{item.icon}</span>
                    {item.label} {item.value}
                  </span>
                ))}
              </div>

              <AnimatePresence initial={false}>
                {ragExpanded && (
                  <motion.div
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    exit={{ opacity: 0, height: 0 }}
                    transition={{ duration: 0.18, ease: 'easeOut' }}
                    className="overflow-hidden"
                  >
                    <div className="rounded-2xl border border-white/[0.08] bg-slate-900/72 px-4 py-3 text-xs text-slate-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
                      <div className="flex flex-wrap gap-2">
                        <span className="rounded-full border border-slate-700/80 bg-slate-950/70 px-2.5 py-1">
                          需引用: {ragMetrics.citation_required ? '是' : '否'}
                        </span>
                        <span className="rounded-full border border-slate-700/80 bg-slate-950/70 px-2.5 py-1">
                          修复成功/尝试: {ragMetrics.citation_repair_successes}/{ragMetrics.citation_repair_attempts}
                        </span>
                        <span className="rounded-full border border-slate-700/80 bg-slate-950/70 px-2.5 py-1">
                          压缩命中/回退: {ragMetrics.compression_success_chunks}/{ragMetrics.compression_fallback_chunks}
                        </span>
                      </div>
                      <div className="mt-3 text-[11px] uppercase tracking-[0.18em] text-slate-500">来源标签</div>
                      <div className="mt-2 text-sm leading-6 text-slate-200">
                        {ragMetrics.source_labels.length > 0 ? ragMetrics.source_labels.join(' · ') : '-'}
                      </div>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )}

          {/* 消息内容 */}
          {isUser ? (
            <div className={userBubbleShellClass}>
              <div className="w-fit max-w-full rounded-2xl rounded-tr-md bg-slate-800 px-5 py-3 text-white shadow-[0_12px_24px_rgba(15,23,42,0.16)]">
                <p className="whitespace-pre-wrap text-base leading-7 text-white">{content}</p>
              </div>
            </div>
          ) : (
            <div className={assistantBubbleWidthClass}>
              <div className="overflow-hidden rounded-[24px] rounded-tl-md border border-white/[0.04] bg-[#13151A] px-6 pt-5 pb-6 shadow-[inset_0_1px_0_rgba(255,255,255,0.03),0_14px_30px_rgba(2,6,23,0.18)]">
                  {hasAssistantPrelude && (
                    <div className="mb-3 space-y-3 border-b border-white/[0.04] pb-3">
                      {reactSteps && reactSteps.length > 0 && (
                        <HistoryReActPanel steps={reactSteps} embedded />
                      )}
                      {thought && (
                        <ThinkingPanel
                          thought={thought}
                          isThinking={false}
                          isExpanded={thoughtExpanded}
                          onToggle={() => setThoughtExpanded(!thoughtExpanded)}
                          embedded
                        />
                      )}
                    </div>
                  )}

                  {content ? (
                    <>
                      <div
                        className="prose prose-invert prose-slate max-w-none
                        [&>*:first-child]:mt-0 [&>*:last-child]:mb-0
                        prose-p:my-3 prose-p:text-base prose-p:leading-8 prose-p:text-slate-100
                        prose-headings:mt-6 prose-headings:mb-3 prose-headings:text-white prose-headings:font-semibold
                        prose-h1:text-xl prose-h2:text-lg prose-h3:text-base
                        prose-pre:my-4 prose-pre:bg-slate-950/90 prose-pre:border prose-pre:border-slate-700/60 prose-pre:rounded-xl
                        prose-code:text-emerald-200 prose-code:bg-white/[0.06] prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded-md prose-code:text-sm prose-code:font-mono
                        prose-strong:text-white prose-strong:font-semibold
                        prose-em:text-slate-200 prose-em:italic
                        prose-a:text-emerald-300 prose-a:no-underline hover:prose-a:text-emerald-200 hover:prose-a:underline
                        prose-blockquote:border-l-4 prose-blockquote:border-slate-600 prose-blockquote:bg-slate-950/60 prose-blockquote:py-2 prose-blockquote:px-4 prose-blockquote:not-italic prose-blockquote:rounded-r-lg
                        prose-hr:border-slate-700 prose-hr:my-6
                        prose-table:border prose-table:border-slate-700 prose-th:bg-slate-800 prose-th:px-3 prose-th:py-2 prose-td:px-3 prose-td:py-2 prose-td:border-t prose-td:border-slate-700
                        text-base leading-7"
                      >
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          components={{
                            code: ({ className, children }) => (
                              <CodeBlock className={className}>{children}</CodeBlock>
                            ),
                            ul: ({ children, ...props }) => (
                              <ul
                                {...props}
                                className="my-3 list-outside list-disc pl-6 text-slate-100 [padding-inline-start:1.5rem]"
                              >
                                {children}
                              </ul>
                            ),
                            ol: ({ children, ...props }) => (
                              <ol
                                {...props}
                                className="my-3 list-outside list-decimal pl-6 text-slate-100 [padding-inline-start:1.5rem]"
                              >
                                {children}
                              </ol>
                            ),
                            li: ({ children, ...props }) => (
                              <li {...props} className="my-1 pl-1 text-slate-100 marker:text-slate-400">
                                {children}
                              </li>
                            ),
                          }}
                        >
                          {content}
                        </ReactMarkdown>
                      </div>

                      {evidenceItems.length > 0 && (
                        <div className="mt-6 overflow-hidden rounded-xl border border-white/[0.05] bg-slate-950/75 shadow-sm">
                          <button
                            type="button"
                            onClick={() => setEvidenceExpanded((value) => !value)}
                            className="flex w-full items-center justify-between gap-3 bg-slate-950/70 px-5 py-3.5 text-left text-slate-200 transition-colors hover:bg-slate-900/80"
                          >
                            <div>
                              <div className="text-sm font-semibold text-slate-100">检索依据</div>
                              <div className="text-xs text-slate-500">
                                来自本轮 `knowledge_search` 的命中片段
                              </div>
                            </div>
                            <div className="flex items-center gap-2">
                              <div className="rounded-full border border-white/[0.08] bg-slate-950/70 px-2.5 py-1 text-[11px] text-slate-300">
                                {evidenceItems.length} 条
                              </div>
                              <span className="flex h-7 w-7 items-center justify-center rounded-full border border-white/[0.08] bg-slate-900/80 text-slate-300">
                                {evidenceExpanded ? <UpOutlined className="text-[11px]" /> : <DownOutlined className="text-[11px]" />}
                              </span>
                            </div>
                          </button>

                          <AnimatePresence initial={false}>
                            {evidenceExpanded && (
                              <motion.div
                                initial={{ opacity: 0, height: 0 }}
                                animate={{ opacity: 1, height: 'auto' }}
                                exit={{ opacity: 0, height: 0 }}
                                transition={{ duration: 0.2, ease: 'easeOut' }}
                                className="overflow-hidden"
                              >
                                <div className="bg-slate-950/75 px-5 pb-4">
                                  <div className="space-y-3 pt-0.5">
                                  {evidenceItems.map((item) => (
                                    <div
                                      key={item.id}
                                      className="rounded-xl border border-slate-700/70 bg-slate-950/70 px-3.5 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]"
                                    >
                                      <div className="flex flex-wrap items-center gap-2 text-xs">
                                        <span className="rounded-full border border-emerald-400/20 bg-emerald-500/10 px-2 py-0.5 font-medium text-emerald-200">
                                          {item.sourceLabel}
                                        </span>
                                        {item.retrievalScore ? (
                                          <span className="rounded-full border border-slate-600/70 bg-slate-800 px-2 py-0.5 text-slate-300">
                                            检索 {item.retrievalScore}%
                                          </span>
                                        ) : null}
                                        {item.compressionScore ? (
                                          <span className="rounded-full border border-slate-600/70 bg-slate-800 px-2 py-0.5 text-slate-300">
                                            片段相关度 {item.compressionScore}/10
                                          </span>
                                        ) : null}
                                      </div>

                                      <Tooltip title={item.sourcePath}>
                                        <div className="mt-2 truncate text-xs text-slate-500">
                                          {item.sourcePath}
                                        </div>
                                      </Tooltip>
                                      <div className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-100">
                                        {item.content}
                                      </div>
                                    </div>
                                  ))}
                                  </div>
                                </div>
                              </motion.div>
                            )}
                          </AnimatePresence>
                        </div>
                      )}

                      {/* 流式输出光标 */}
                      {isStreaming && (
                        <span className="inline-block w-2 h-5 bg-emerald-400 animate-pulse ml-1 -mb-1 rounded-sm" />
                      )}
                    </>
                  ) : isStreaming ? (
                    <div className="flex items-center gap-3 py-2">
                      <div className="flex gap-1">
                        <span
                          className="w-2 h-2 bg-emerald-400 rounded-full animate-bounce"
                          style={{ animationDelay: '0ms' }}
                        />
                        <span
                          className="w-2 h-2 bg-teal-400 rounded-full animate-bounce"
                          style={{ animationDelay: '150ms' }}
                        />
                        <span
                          className="w-2 h-2 bg-cyan-400 rounded-full animate-bounce"
                          style={{ animationDelay: '300ms' }}
                        />
                      </div>
                      <span className="text-sm text-slate-400">
                        {isThinking ? '正在思考...' : '正在生成回答...'}
                      </span>
                    </div>
                  ) : null}

                  {/* 操作栏 */}
                      {!isStreaming && content && (
                    <div className="mt-5 flex items-center gap-3 border-t border-white/[0.04] pt-4">
                      <Tooltip title="复制内容">
                        <Button
                          type="text"
                          size="small"
                          icon={<CopyOutlined />}
                          onClick={handleCopy}
                          className="rounded-lg text-slate-400 transition-all hover:bg-white/[0.04] hover:text-emerald-300"
                        >
                          复制
                        </Button>
                      </Tooltip>

                      <div className="flex-1" />
                      <span className="text-xs text-slate-600">AI 生成内容，仅供参考</span>
                    </div>
                  )}
              </div>
            </div>
          )}
        </div>
      </motion.div>
    )
  }
)

MessageBubble.displayName = 'MessageBubble'

export default MessageBubble
