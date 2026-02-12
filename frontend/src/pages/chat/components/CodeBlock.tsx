import { Button, message } from 'antd'
import { CopyOutlined } from '@ant-design/icons'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'

/** 代码块组件 - 支持语法高亮和复制 */
const CodeBlock = ({ className, children }: { className?: string; children: React.ReactNode }) => {
  const match = /language-(\w+)/.exec(className || '')
  const language = match ? match[1] : ''
  const code = String(children).replace(/\n$/, '')

  const handleCopy = () => {
    navigator.clipboard.writeText(code)
    message.success('代码已复制')
  }

  if (!match) {
    return (
      <code className="bg-slate-800 text-emerald-400 px-1.5 py-0.5 rounded text-sm font-mono">
        {children}
      </code>
    )
  }

  return (
    <div className="relative group my-4 rounded-xl overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 bg-slate-800 border-b border-slate-700">
        <span className="text-xs text-slate-400 font-mono">{language}</span>
        <Button
          type="text"
          size="small"
          icon={<CopyOutlined />}
          onClick={handleCopy}
          className="text-slate-400 hover:text-white opacity-0 group-hover:opacity-100 transition-opacity"
        />
      </div>
      <SyntaxHighlighter
        language={language}
        style={oneDark}
        customStyle={{
          margin: 0,
          borderRadius: 0,
          padding: '1rem',
          fontSize: '0.875rem',
          background: '#1e293b',
        }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  )
}

export default CodeBlock
