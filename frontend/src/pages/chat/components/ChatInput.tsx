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
    <div className="border-t border-slate-800/50 bg-slate-900/90 backdrop-blur-xl">
      <div className="max-w-3xl mx-auto p-4">
        <div className="relative flex items-end gap-3">
          <div className="flex-1 relative">
            <TextArea
              value={inputValue}
              onChange={(e) => onInputChange(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="输入问题，按 Enter 发送..."
              autoSize={{ minRows: 1, maxRows: 6 }}
              className="text-base bg-slate-800/80 border-slate-700/50 rounded-xl resize-none 
                focus:border-emerald-500/50 focus:ring-1 focus:ring-emerald-500/20
                placeholder:text-slate-500"
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
              className="bg-red-500 hover:bg-red-600 border-none rounded-xl h-10 px-5
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
              className="bg-emerald-500 hover:bg-emerald-600 border-none rounded-xl h-10 px-5
                shadow-lg shadow-emerald-500/20 disabled:opacity-50"
            >
              发送
            </Button>
          )}
        </div>

        {/* 底部信息 */}
        <div className="flex items-center justify-between mt-3 text-xs text-slate-500">
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
