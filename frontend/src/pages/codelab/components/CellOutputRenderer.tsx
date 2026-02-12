import { ExclamationCircleOutlined } from '@ant-design/icons'
import { CellOutput } from '@/services/api'

/** 渲染单个 Cell 输出（stream / execute_result / display_data / error） */
const CellOutputRenderer = ({ output }: { output: CellOutput }) => {
  if (output.output_type === 'stream') {
    return (
      <pre className="text-sm text-slate-300 font-mono whitespace-pre-wrap bg-slate-900/50 p-3 rounded-lg overflow-x-auto">
        {output.content}
      </pre>
    )
  }

  if (output.output_type === 'execute_result') {
    return (
      <div className="flex items-start gap-3">
        <span className="text-emerald-500 font-mono text-sm mt-0.5">Out:</span>
        <pre className="text-sm text-amber-400 font-mono whitespace-pre-wrap flex-1 overflow-x-auto">
          {output.content}
        </pre>
      </div>
    )
  }

  if (output.output_type === 'display_data' && output.mime_type === 'image/png') {
    return (
      <div className="flex justify-center py-2">
        <img
          src={output.content}
          alt="Plot output"
          className="max-w-full rounded-lg shadow-lg border border-slate-700/50"
          style={{ maxHeight: '500px' }}
        />
      </div>
    )
  }

  if (output.output_type === 'error') {
    const error = output.content as { ename: string; evalue: string; traceback: string[] }
    return (
      <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4">
        <div className="flex items-center gap-2 text-red-400 font-semibold mb-2">
          <ExclamationCircleOutlined />
          <span>{error.ename}: {error.evalue}</span>
        </div>
        {error.traceback && error.traceback.length > 0 && (
          <pre className="text-xs text-red-300/80 font-mono whitespace-pre-wrap overflow-x-auto">
            {error.traceback.join('\n')}
          </pre>
        )}
      </div>
    )
  }

  return null
}

export default CellOutputRenderer
