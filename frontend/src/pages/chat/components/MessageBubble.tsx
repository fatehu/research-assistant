import { useState, forwardRef } from 'react'
import { Button, Tooltip, Avatar, message } from 'antd'
import {
  RobotOutlined,
  UserOutlined,
  CopyOutlined,
} from '@ant-design/icons'
import { motion } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { SHOW_RAG_METRICS, type Message, type RagMetrics } from '@/services/api'
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
    const ragMetrics = !isStreaming && !isUser ? parseRagMetrics(msg.metadata?.rag_metrics) : null

    const handleCopy = () => {
      navigator.clipboard.writeText(content)
      message.success('已复制到剪贴板')
    }

    return (
      <motion.div
        ref={ref}
        id={`message-${msg.id}`}
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3, ease: 'easeOut' }}
        className={`flex gap-4 ${isUser ? 'flex-row-reverse' : ''} ${
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
              size={40}
              icon={<UserOutlined />}
              className="bg-gradient-to-br from-blue-500 to-indigo-600 shadow-lg shadow-blue-500/30"
            />
          ) : (
            <div className="w-10 h-10 rounded-full bg-gradient-to-br from-emerald-500 via-teal-500 to-cyan-500 flex items-center justify-center shadow-lg shadow-emerald-500/30">
              <RobotOutlined className="text-white text-lg" />
            </div>
          )}
        </div>

        {/* 内容区 */}
        <div className={`flex-1 max-w-[85%] ${isUser ? 'flex flex-col items-end' : ''}`}>
          {/* 角色标签 */}
          <div className={`flex items-center gap-2 mb-2 ${isUser ? 'flex-row-reverse' : ''}`}>
            <span className={`text-sm font-medium ${isUser ? 'text-blue-400' : 'text-emerald-400'}`}>
              {isUser ? '你' : 'AI 助手'}
            </span>
            <span className="text-xs text-slate-500">
              {new Date(msg.created_at).toLocaleTimeString('zh-CN', {
                hour: '2-digit',
                minute: '2-digit',
              })}
            </span>
          </div>

          {/* ReAct 推理过程面板 (历史 AI 消息) */}
          {!isUser && !isStreaming && reactSteps && reactSteps.length > 0 && (
            <HistoryReActPanel steps={reactSteps} />
          )}

          {/* 最终思考面板 */}
          {!isUser && !isStreaming && thought && (
            <ThinkingPanel
              thought={thought}
              isThinking={false}
              isExpanded={thoughtExpanded}
              onToggle={() => setThoughtExpanded(!thoughtExpanded)}
            />
          )}

          {SHOW_RAG_METRICS && !isUser && !isStreaming && ragMetrics && (
            <div className="relative mb-3 rounded-xl overflow-hidden border border-cyan-500/25 bg-slate-900/80 backdrop-blur-sm">
              <div className="absolute inset-0 bg-gradient-to-r from-cyan-500/10 via-blue-500/5 to-emerald-500/10 pointer-events-none" />
              <button
                type="button"
                onClick={() => setRagExpanded(!ragExpanded)}
                className="relative z-10 w-full flex items-center justify-between px-3 py-2 text-left bg-transparent border-0 outline-none appearance-none hover:bg-white/5 transition-colors"
              >
                <span className="text-xs font-medium text-cyan-100 tracking-wide">RAG质量</span>
                <span
                  className={`text-[11px] px-2 py-0.5 rounded-full border ${
                    ragMetrics.citation_valid
                      ? 'text-emerald-300 border-emerald-400/30 bg-emerald-500/10'
                      : 'text-amber-300 border-amber-400/30 bg-amber-500/10'
                  }`}
                >
                  {ragMetrics.citation_valid ? '引用有效' : '引用待修正'}
                </span>
              </button>

              {ragExpanded && (
                <div className="relative z-10 px-3 pb-3 pt-2 border-t border-cyan-500/20 text-xs text-slate-200 grid grid-cols-2 gap-2">
                  <div className="rounded-lg border border-slate-700/70 bg-slate-900/70 px-2.5 py-2">检索调用: {ragMetrics.knowledge_search_calls}</div>
                  <div className="rounded-lg border border-slate-700/70 bg-slate-900/70 px-2.5 py-2">来源数: {ragMetrics.source_labels_count}</div>
                  <div className="rounded-lg border border-slate-700/70 bg-slate-900/70 px-2.5 py-2">答案引用: {ragMetrics.answer_citation_count}</div>
                  <div className="rounded-lg border border-slate-700/70 bg-slate-900/70 px-2.5 py-2">需引用: {ragMetrics.citation_required ? '是' : '否'}</div>
                  <div className="rounded-lg border border-slate-700/70 bg-slate-900/70 px-2.5 py-2">压缩调用: {ragMetrics.compression_calls}</div>
                  <div className="rounded-lg border border-slate-700/70 bg-slate-900/70 px-2.5 py-2">
                    压缩命中/回退: {ragMetrics.compression_success_chunks}/{ragMetrics.compression_fallback_chunks}
                  </div>
                  <div className="rounded-lg border border-slate-700/70 bg-slate-900/70 px-2.5 py-2">
                    修复成功/尝试: {ragMetrics.citation_repair_successes}/{ragMetrics.citation_repair_attempts}
                  </div>
                  <div className="col-span-2 rounded-lg border border-slate-700/70 bg-slate-900/70 px-2.5 py-2 break-all">
                    来源标签: {ragMetrics.source_labels.length > 0 ? ragMetrics.source_labels.join(', ') : '-'}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* 消息内容 */}
          {isUser ? (
            // 用户消息 - 简洁风格
            <div className="bg-gradient-to-r from-blue-600 to-indigo-600 text-white px-5 py-3 rounded-2xl rounded-tr-md shadow-lg shadow-blue-500/20">
              <p className="text-[15px] whitespace-pre-wrap leading-relaxed">{content}</p>
            </div>
          ) : (
            // AI消息 - 精美卡片风格
            <div className="relative">
              {/* 渐变边框效果 */}
              <div className="absolute -inset-0.5 bg-gradient-to-r from-emerald-500/20 via-teal-500/20 to-cyan-500/20 rounded-2xl blur-sm" />

              <div className="relative bg-slate-800/90 backdrop-blur-xl border border-slate-700/50 rounded-2xl rounded-tl-md overflow-hidden">
                {/* 顶部渐变装饰 */}
                <div className="h-1 bg-gradient-to-r from-emerald-500 via-teal-500 to-cyan-500" />

                <div className="p-5">
                  {content ? (
                    <>
                      <div
                        className="prose prose-invert prose-slate max-w-none
                        prose-p:my-3 prose-p:leading-relaxed prose-p:text-slate-200
                        prose-headings:mt-6 prose-headings:mb-3 prose-headings:text-white prose-headings:font-semibold
                        prose-h1:text-xl prose-h2:text-lg prose-h3:text-base
                        prose-li:my-1 prose-li:text-slate-200
                        prose-ul:my-3 prose-ol:my-3
                        prose-pre:my-4 prose-pre:bg-slate-900 prose-pre:border prose-pre:border-slate-700/50 prose-pre:rounded-xl
                        prose-code:text-emerald-400 prose-code:bg-slate-900/80 prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded-md prose-code:text-sm prose-code:font-mono
                        prose-strong:text-white prose-strong:font-semibold
                        prose-em:text-slate-300 prose-em:italic
                        prose-a:text-blue-400 prose-a:no-underline hover:prose-a:text-blue-300 hover:prose-a:underline
                        prose-blockquote:border-l-4 prose-blockquote:border-emerald-500/50 prose-blockquote:bg-slate-900/50 prose-blockquote:py-2 prose-blockquote:px-4 prose-blockquote:not-italic prose-blockquote:rounded-r-lg
                        prose-hr:border-slate-700 prose-hr:my-6
                        prose-table:border prose-table:border-slate-700 prose-th:bg-slate-800 prose-th:px-3 prose-th:py-2 prose-td:px-3 prose-td:py-2 prose-td:border-t prose-td:border-slate-700
                        text-[15px] leading-relaxed"
                      >
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          components={{
                            code: ({ className, children }) => (
                              <CodeBlock className={className}>{children}</CodeBlock>
                            ),
                          }}
                        >
                          {content}
                        </ReactMarkdown>
                      </div>

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
                    <div className="flex items-center gap-3 mt-4 pt-4 border-t border-slate-700/50">
                      <Tooltip title="复制内容">
                        <Button
                          type="text"
                          size="small"
                          icon={<CopyOutlined />}
                          onClick={handleCopy}
                          className="text-slate-400 hover:text-emerald-400 hover:bg-emerald-500/10 rounded-lg transition-all"
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
            </div>
          )}
        </div>
      </motion.div>
    )
  }
)

MessageBubble.displayName = 'MessageBubble'

export default MessageBubble
