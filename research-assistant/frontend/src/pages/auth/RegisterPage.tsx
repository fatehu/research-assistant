import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Form, Input, Button, message } from 'antd'
import { MailOutlined, LockOutlined, UserOutlined, IdcardOutlined } from '@ant-design/icons'
import { motion } from 'framer-motion'
import { useAuthStore } from '@/stores/authStore'

interface RegisterForm {
  email: string
  username: string
  fullName?: string
  password: string
  confirmPassword: string
}

const RegisterPage = () => {
  const navigate = useNavigate()
  const { register, isLoading } = useAuthStore()
  const [form] = Form.useForm()
  const [error, setError] = useState('')
  
  const onFinish = async (values: RegisterForm) => {
    setError('')
    try {
      await register(values.email, values.username, values.password, values.fullName)
      message.success('注册成功')
      navigate('/dashboard')
    } catch (err: unknown) {
      const errorMessage = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '注册失败，请重试'
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
            创建账号
          </h1>
          <p className="text-gray-400">
            加入 AI 科研助手平台
          </p>
        </div>
        
        {/* 注册表单 */}
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
              name="username"
              rules={[
                { required: true, message: '请输入用户名' },
                { min: 3, message: '用户名至少3个字符' },
              ]}
            >
              <Input
                prefix={<UserOutlined className="text-gray-500" />}
                placeholder="用户名"
              />
            </Form.Item>
            
            <Form.Item name="fullName">
              <Input
                prefix={<IdcardOutlined className="text-gray-500" />}
                placeholder="姓名（可选）"
              />
            </Form.Item>
            
            <Form.Item
              name="password"
              rules={[
                { required: true, message: '请输入密码' },
                { min: 6, message: '密码至少6个字符' },
              ]}
            >
              <Input.Password
                prefix={<LockOutlined className="text-gray-500" />}
                placeholder="密码"
              />
            </Form.Item>
            
            <Form.Item
              name="confirmPassword"
              dependencies={['password']}
              rules={[
                { required: true, message: '请确认密码' },
                ({ getFieldValue }) => ({
                  validator(_, value) {
                    if (!value || getFieldValue('password') === value) {
                      return Promise.resolve()
                    }
                    return Promise.reject(new Error('两次输入的密码不一致'))
                  },
                }),
              ]}
            >
              <Input.Password
                prefix={<LockOutlined className="text-gray-500" />}
                placeholder="确认密码"
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
                注册
              </Button>
            </Form.Item>
          </Form>
          
          <div className="text-center text-gray-400">
            已有账号？{' '}
            <Link to="/login" className="text-blue-400 hover:text-blue-300">
              立即登录
            </Link>
          </div>
        </div>
      </motion.div>
    </div>
  )
}

export default RegisterPage
