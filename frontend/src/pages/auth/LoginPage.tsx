import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Form, Input, Button, message } from 'antd'
import { MailOutlined, LockOutlined } from '@ant-design/icons'
import { motion } from 'framer-motion'
import { useAuthStore } from '@/stores/authStore'

interface LoginForm {
  email: string
  password: string
}

const LoginPage = () => {
  const navigate = useNavigate()
  const { login, isLoading } = useAuthStore()
  const [form] = Form.useForm()
  const [error, setError] = useState('')
  
  const onFinish = async (values: LoginForm) => {
    setError('')
    try {
      await login(values.email, values.password)
      message.success('登录成功')
      navigate('/dashboard')
    } catch (err: unknown) {
      const errorMessage = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '登录失败，请重试'
      setError(errorMessage)
    }
  }
  
  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      {/* 背景装饰 */}
      <div className="fixed inset-0 overflow-hidden pointer-events-none">
        <div className="absolute -top-1/2 -right-1/2 w-full h-full bg-gradient-to-br from-blue-500/20 to-purple-500/20 rounded-full blur-3xl" />
        <div className="absolute -bottom-1/2 -left-1/2 w-full h-full bg-gradient-to-tr from-cyan-500/20 to-blue-500/20 rounded-full blur-3xl" />
      </div>
      
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
        className="w-full max-w-md"
      >
        {/* Logo */}
        <div className="text-center mb-8">
          <motion.div
            initial={{ scale: 0.8 }}
            animate={{ scale: 1 }}
            transition={{ delay: 0.2, type: 'spring' }}
            className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-br from-blue-500 to-blue-600 shadow-lg shadow-blue-500/30 mb-4"
          >
            <span className="text-white font-bold text-2xl">AI</span>
          </motion.div>
          <h1 className="text-2xl font-bold text-white mb-2">
            欢迎回来
          </h1>
          <p className="text-gray-400">
            登录到 AI 科研助手平台
          </p>
        </div>
        
        {/* 登录表单 */}
        <div className="glass-dark p-8">
          <Form
            form={form}
            onFinish={onFinish}
            layout="vertical"
            size="large"
          >
            <Form.Item
              name="email"
              rules={[
                { required: true, message: '请输入邮箱' },
                { type: 'email', message: '请输入有效的邮箱地址' },
              ]}
            >
              <Input
                prefix={<MailOutlined className="text-gray-500" />}
                placeholder="邮箱地址"
              />
            </Form.Item>
            
            <Form.Item
              name="password"
              rules={[{ required: true, message: '请输入密码' }]}
            >
              <Input.Password
                prefix={<LockOutlined className="text-gray-500" />}
                placeholder="密码"
              />
            </Form.Item>
            
            {error && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm"
              >
                {error}
              </motion.div>
            )}
            
            <Form.Item className="mb-4">
              <Button
                type="primary"
                htmlType="submit"
                loading={isLoading}
                className="w-full h-12 text-base font-medium"
              >
                登录
              </Button>
            </Form.Item>
          </Form>
          
          <div className="text-center text-gray-400">
            还没有账号？{' '}
            <Link to="/register" className="text-blue-400 hover:text-blue-300">
              立即注册
            </Link>
          </div>
        </div>
        
        {/* 底部装饰 */}
        <div className="mt-8 text-center text-gray-500 text-sm">
          <p>支持多种 AI 模型：DeepSeek · OpenAI · 通义千问</p>
        </div>
      </motion.div>
    </div>
  )
}

export default LoginPage
