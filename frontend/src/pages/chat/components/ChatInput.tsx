import { Input, Button } from 'antd'
import { SendOutlined, StopOutlined } from '@ant-design/icons'

const { TextArea } = Input

interface ChatInputProps {
  inputValue: string
  isSending: boolean
  llmProvider?: string
  onInputChange: (value: string) => void
  onSend: () => void
  onStop: () => void
}

/** 聊天输入区域 */
const ChatInput = ({
  inputValue,
  isSending,
  llmProvider,
  onInputChange,
  onSend,
  onStop,
}: ChatInputProps) => {
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSend()
    }
  }

  return (
    <div className="border-t border-white/[0.06] bg-slate-950/88 backdrop-blur-2xl">
      <div className="mx-auto max-w-[1040px] px-4 py-4 sm:px-6 lg:px-8">
        <div className="relative flex items-end gap-3">
          <div className="relative flex-1 rounded-[24px] border border-slate-700/60 bg-slate-800/78 p-2 shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_14px_28px_rgba(2,6,23,0.2)] transition-all duration-200 focus-within:border-emerald-400/30 focus-within:shadow-[inset_0_1px_0_rgba(255,255,255,0.05),0_0_0_1px_rgba(16,185,129,0.06)]">
            <TextArea
              value={inputValue}
              onChange={(e) => onInputChange(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="输入问题，按 Enter 发送..."
              autoSize={{ minRows: 1, maxRows: 6 }}
              className="!m-0 !rounded-[18px] !border-0 !bg-transparent !px-4 !py-3 !text-base !leading-7 !text-slate-100 !shadow-none resize-none placeholder:!text-slate-500 focus:!shadow-none"
              disabled={isSending}
            />
          </div>
          {isSending ? (
            <Button
              type="primary"
              size="large"
              danger
              icon={<StopOutlined />}
              onClick={onStop}
              className="bg-red-500 hover:bg-red-600 border-none rounded-2xl h-[52px] px-5
                shadow-lg shadow-red-500/20"
            >
              停止
            </Button>
          ) : (
            <Button
              type="primary"
              size="large"
              icon={<SendOutlined />}
              onClick={onSend}
              disabled={!inputValue.trim()}
              className="bg-emerald-500 hover:bg-emerald-600 border-none rounded-2xl h-[52px] px-5
                shadow-lg shadow-emerald-500/20 disabled:opacity-50"
            >
              发送
            </Button>
          )}
        </div>

        {/* 底部信息 */}
        <div className="mt-3 flex items-center justify-between text-xs text-slate-500">
          <span className="flex items-center gap-2">
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                isSending ? 'bg-amber-400' : 'bg-emerald-400'
              } animate-pulse`}
            />
            <span className="text-slate-400">
              {isSending ? '正在生成...' : llmProvider || 'DeepSeek'}
            </span>
          </span>
          <span className="text-slate-600">
            {isSending ? '点击停止按钮可中止生成' : 'Shift + Enter 换行 · Enter 发送'}
          </span>
        </div>
      </div>
    </div>
  )
}

export default ChatInput
