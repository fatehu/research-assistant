import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import './index.css'
import './styles/design-system.css'

// 2026 Design System Theme
const theme = {
  token: {
    colorPrimary: '#4A9EE8',
    colorPrimaryHover: '#64B5F6',
    colorSuccess: '#34D399',
    colorWarning: '#FBBF24',
    colorError: '#F87171',
    colorText: 'rgba(255, 255, 255, 0.93)',
    colorTextSecondary: 'rgba(255, 255, 255, 0.68)',
    colorTextTertiary: 'rgba(255, 255, 255, 0.48)',
    colorBgBase: '#0f1318',
    colorBgContainer: '#161b22',
    colorBgElevated: '#1e252e',
    colorBorder: 'rgba(255, 255, 255, 0.1)',
    borderRadius: 12,
    borderRadiusLG: 16,
    borderRadiusSM: 8,
    fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
  },
  components: {
    Button: { borderRadius: 10 },
    Input: { borderRadius: 10 },
    Select: { borderRadius: 10 },
    Card: { borderRadius: 16 },
    Modal: { borderRadius: 20 },
    Table: { headerBg: '#0f1318', rowHoverBg: '#242e3d' },
    Dropdown: { borderRadius: 12 },
    Tag: { borderRadius: 20 },
  },
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={theme}>
      <App />
    </ConfigProvider>
  </React.StrictMode>,
)
