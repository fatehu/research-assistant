import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  App,
  Alert,
  Button,
  Card,
  Col,
  Divider,
  Form,
  Input,
  List,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Tag,
  Typography,
} from 'antd'
import {
  BellOutlined,
  CheckCircleOutlined,
  CloudServerOutlined,
  KeyOutlined,
  LockOutlined,
  ReloadOutlined,
  RobotOutlined,
  SaveOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import { useAuthStore } from '../../stores/authStore'
import api, {
  mcpApi,
  MCPServerStatusItem,
  MCPServerTemplate,
} from '../../services/api'

const { Title, Text } = Typography
const { Option } = Select

type NotificationPreferences = {
  login_alert: boolean
  invite_notification: boolean
  announcement_notification: boolean
  system_notification: boolean
}

const DEFAULT_NOTIFICATION_PREFERENCES: NotificationPreferences = {
  login_alert: true,
  invite_notification: true,
  announcement_notification: true,
  system_notification: false,
}

const SettingsPage: React.FC = () => {
  const { message } = App.useApp()
  const { user, updateUser } = useAuthStore()
  const [llmForm] = Form.useForm()
  const [passwordForm] = Form.useForm()

  const [savingLLM, setSavingLLM] = useState(false)
  const [changingPassword, setChangingPassword] = useState(false)
  const [savingNotifications, setSavingNotifications] = useState(false)
  const [passwordModalVisible, setPasswordModalVisible] = useState(false)
  const [notificationPreferences, setNotificationPreferences] = useState<NotificationPreferences>(
    DEFAULT_NOTIFICATION_PREFERENCES
  )

  const [mcpConfigText, setMcpConfigText] = useState('')
  const [mcpTemplates, setMcpTemplates] = useState<MCPServerTemplate[]>([])
  const [selectedTemplateId, setSelectedTemplateId] = useState<string>('')
  const [mcpStatus, setMcpStatus] = useState<MCPServerStatusItem[]>([])

  const [mcpLoading, setMcpLoading] = useState(false)
  const [mcpSaving, setMcpSaving] = useState(false)
  const [mcpValidating, setMcpValidating] = useState(false)

  useEffect(() => {
    if (user) {
      llmForm.setFieldsValue({
        preferred_llm_provider: user.preferred_llm_provider || 'openai',
      })
      const rawPreferences = (user.preferences || {}) as Record<string, unknown>
      const rawNotificationPrefs = (rawPreferences.notifications || {}) as Partial<NotificationPreferences>
      setNotificationPreferences({
        ...DEFAULT_NOTIFICATION_PREFERENCES,
        ...rawNotificationPrefs,
      })
    }
  }, [user, llmForm])

  const loadMcpConfig = useCallback(async () => {
    setMcpLoading(true)
    try {
      const data = await mcpApi.getConfig()
      setMcpConfigText(JSON.stringify(data.claude_desktop_config || { mcpServers: {} }, null, 2))
      message.success('已加载当前 MCP 配置')
    } catch (error: any) {
      message.error(error.response?.data?.detail || '加载 MCP 配置失败')
    } finally {
      setMcpLoading(false)
    }
  }, [message])

  const loadMcpTemplates = useCallback(async () => {
    try {
      const data = await mcpApi.getTemplates()
      const templates = data.templates || []
      setMcpTemplates(templates)
      if (templates.length > 0) {
        setSelectedTemplateId(templates[0].id)
      }
    } catch (error) {
      console.error('加载 MCP 模板失败:', error)
    }
  }, [])

  const applyTemplate = () => {
    const template = mcpTemplates.find((item) => item.id === selectedTemplateId)
    if (!template) {
      message.warning('请先选择模板')
      return
    }
    setMcpConfigText(JSON.stringify(template.claude_desktop_config, null, 2))
    message.success(`已应用模板：${template.title}`)
  }

  const handleValidateMcp = async () => {
    if (!mcpConfigText.trim()) {
      message.warning('请先输入 MCP JSON')
      return
    }
    setMcpValidating(true)
    try {
      const result = await mcpApi.validateConfig({ raw_json: mcpConfigText })
      message.success(`校验通过，解析到 ${result.server_count} 个服务`)
    } catch (error: any) {
      message.error(error.response?.data?.detail || 'MCP 校验失败')
    } finally {
      setMcpValidating(false)
    }
  }

  const handleSaveMcp = async () => {
    if (!mcpConfigText.trim()) {
      message.warning('请先输入 MCP JSON')
      return
    }
    setMcpSaving(true)
    try {
      const result = await mcpApi.saveConfig({ raw_json: mcpConfigText })
      message.success(`${result.message}（${result.server_count} 个服务）`)
    } catch (error: any) {
      message.error(error.response?.data?.detail || '保存 MCP 配置失败')
    } finally {
      setMcpSaving(false)
    }
  }

  const handleRefreshMcpStatus = async () => {
    setMcpLoading(true)
    try {
      const data = await mcpApi.refreshStatus(true)
      setMcpStatus(data.servers || [])
      message.success(`探测完成：${data.server_count} 个服务，${data.tool_count} 个工具`)
    } catch (error: any) {
      message.error(error.response?.data?.detail || '探测 MCP 服务失败')
    } finally {
      setMcpLoading(false)
    }
  }

  useEffect(() => {
    loadMcpTemplates()
    loadMcpConfig()
  }, [loadMcpConfig, loadMcpTemplates])

  const mcpReachableCount = useMemo(() => mcpStatus.filter((item) => item.reachable === true).length, [mcpStatus])
  const mcpToolCount = useMemo(() => mcpStatus.reduce((sum, item) => sum + (item.discovered_tools || 0), 0), [mcpStatus])

  const handleSaveLLMSettings = async (values: any) => {
    setSavingLLM(true)
    try {
      const response = await api.put('/api/v1/users/profile', {
        preferred_llm_provider: values.preferred_llm_provider,
        preferences: {
          ...user?.preferences,
          ...values.preferences,
        },
      })
      updateUser(response.data)
      message.success('设置已保存')
    } catch (error: any) {
      message.error(error.response?.data?.detail || '保存设置失败')
    } finally {
      setSavingLLM(false)
    }
  }

  const handleSaveNotificationPreferences = async () => {
    setSavingNotifications(true)
    try {
      const rawPreferences = (user?.preferences || {}) as Record<string, unknown>
      const response = await api.put('/api/v1/users/profile', {
        preferences: {
          ...rawPreferences,
          notifications: notificationPreferences,
        },
      })
      updateUser(response.data)
      message.success('通知偏好已保存')
    } catch (error: any) {
      message.error(error.response?.data?.detail || '保存通知偏好失败')
    } finally {
      setSavingNotifications(false)
    }
  }

  const handleChangePassword = async (values: any) => {
    if (values.new_password !== values.confirm_password) {
      message.error('两次密码输入不一致')
      return
    }

    setChangingPassword(true)
    try {
      await api.put('/api/v1/users/password', {
        old_password: values.current_password,
        new_password: values.new_password,
      })
      message.success('密码已更新')
      setPasswordModalVisible(false)
      passwordForm.resetFields()
    } catch (error: any) {
      message.error(error.response?.data?.detail || '更新密码失败')
    } finally {
      setChangingPassword(false)
    }
  }

  return (
    <div
      style={{
        padding: 24,
        maxWidth: 920,
        margin: '0 auto',
        minHeight: '100%',
      }}
    >
      <Title level={3} style={{ color: '#E8E8E8', marginBottom: 24, display: 'flex', alignItems: 'center', gap: 12 }}>
        <SettingOutlined style={{ color: '#4A90D9' }} />
        设置
      </Title>

      <Card
        title={
          <span style={{ color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
            <RobotOutlined style={{ color: '#52c41a' }} />
            LLM 设置
          </span>
        }
        style={{ backgroundColor: '#161B22', borderColor: '#30363D', borderRadius: 16, marginBottom: 24 }}
        styles={{ header: { borderBottom: '1px solid #30363D' }, body: { padding: 24 } }}
      >
        <Form form={llmForm} layout="vertical" onFinish={handleSaveLLMSettings}>
          <Form.Item
            name="preferred_llm_provider"
            label={<span style={{ color: '#8899A6' }}>默认服务商</span>}
          >
            <Select
              style={{ width: '100%' }}
              styles={{ popup: { root: { backgroundColor: '#161B22', border: '1px solid #30363D' } } }}
            >
              <Option value="deepseek">DeepSeek (deepseek-chat)</Option>
              <Option value="openai">OpenAI (GPT-4o)</Option>
              <Option value="aliyun">Aliyun (qwen-plus)</Option>
              <Option value="ollama">本地 (Ollama)</Option>
            </Select>
          </Form.Item>

          <Alert
            message="提示"
            description="选择默认模型服务商，用于助手相关操作。"
            type="info"
            showIcon
            style={{ backgroundColor: 'rgba(74, 144, 217, 0.1)', border: '1px solid rgba(74, 144, 217, 0.3)', marginBottom: 16 }}
          />

          <Button type="primary" icon={<SaveOutlined />} htmlType="submit" loading={savingLLM} style={{ backgroundColor: '#4A90D9' }}>
            保存设置
          </Button>
        </Form>
      </Card>

      <Card
        title={
          <span style={{ color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
            <CloudServerOutlined style={{ color: '#13c2c2' }} />
            MCP 服务配置
          </span>
        }
        style={{ backgroundColor: '#161B22', borderColor: '#30363D', borderRadius: 16, marginBottom: 24 }}
        styles={{ header: { borderBottom: '1px solid #30363D' }, body: { padding: 24 } }}
      >
        <Alert
          message="MCP 配置指南"
          description="流程：应用模板 -> 校验 JSON -> 保存配置 -> 刷新连通状态。"
          type="info"
          showIcon
          style={{ backgroundColor: 'rgba(19, 194, 194, 0.08)', border: '1px solid rgba(19, 194, 194, 0.35)', marginBottom: 16 }}
        />

        <Row gutter={12} style={{ marginBottom: 12 }}>
          <Col xs={24} md={8}>
            <Card size="small" style={{ background: '#0D1117', borderColor: '#30363D' }}>
              <Statistic title={<span style={{ color: '#8b949e' }}>服务数</span>} value={mcpStatus.length} valueStyle={{ color: '#e6edf3', fontSize: 20 }} />
            </Card>
          </Col>
          <Col xs={24} md={8}>
            <Card size="small" style={{ background: '#0D1117', borderColor: '#30363D' }}>
              <Statistic title={<span style={{ color: '#8b949e' }}>可达数</span>} value={mcpReachableCount} valueStyle={{ color: '#52c41a', fontSize: 20 }} />
            </Card>
          </Col>
          <Col xs={24} md={8}>
            <Card size="small" style={{ background: '#0D1117', borderColor: '#30363D' }}>
              <Statistic title={<span style={{ color: '#8b949e' }}>工具数</span>} value={mcpToolCount} valueStyle={{ color: '#13c2c2', fontSize: 20 }} />
            </Card>
          </Col>
        </Row>

        <Space wrap style={{ marginBottom: 12 }}>
          <Select
            value={selectedTemplateId}
            onChange={setSelectedTemplateId}
            style={{ width: 320 }}
            placeholder="选择 MCP 模板"
            options={mcpTemplates.map((item) => ({ value: item.id, label: item.title }))}
          />
          <Button onClick={applyTemplate}>应用模板</Button>
          <Button icon={<ReloadOutlined />} loading={mcpLoading} onClick={loadMcpConfig}>加载当前配置</Button>
          <Button icon={<CheckCircleOutlined />} loading={mcpValidating} onClick={handleValidateMcp}>校验</Button>
          <Button type="primary" icon={<SaveOutlined />} loading={mcpSaving} onClick={handleSaveMcp}>保存</Button>
          <Button loading={mcpLoading} onClick={handleRefreshMcpStatus}>刷新状态</Button>
        </Space>

        <Input.TextArea
          value={mcpConfigText}
          onChange={(e) => setMcpConfigText(e.target.value)}
          autoSize={{ minRows: 10, maxRows: 18 }}
          placeholder='{"mcpServers": {"exa": {"type": "http", "url": "https://mcp.exa.ai/mcp"}}}'
          style={{
            backgroundColor: '#0D1117',
            borderColor: '#30363D',
            color: '#E8E8E8',
            marginBottom: 12,
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
            fontSize: 12,
          }}
        />

        <Card size="small" style={{ background: '#0D1117', borderColor: '#30363D' }}>
          {mcpStatus.length === 0 ? (
            <Text style={{ color: '#6B8E9F', fontSize: 12 }}>
              暂无状态，点击“刷新状态”探测 MCP 服务。
            </Text>
          ) : (
            <List
              dataSource={mcpStatus}
              renderItem={(item) => (
                <List.Item>
                  <Space direction="vertical" size={2} style={{ width: '100%' }}>
                    <Space>
                      <Tag color={item.reachable ? 'success' : item.reachable === false ? 'error' : 'default'}>
                        {item.reachable ? '可达' : item.reachable === false ? '不可达' : '未知'}
                      </Tag>
                      <Text style={{ color: '#E8E8E8' }}>{item.name}</Text>
                      <Text style={{ color: '#8b949e', fontSize: 12 }}>传输: {item.transport}</Text>
                      <Text style={{ color: '#8b949e', fontSize: 12 }}>工具: {item.discovered_tools}</Text>
                    </Space>
                    {item.last_error && (
                      <Text style={{ color: '#ff7875', fontSize: 12 }}>错误: {item.last_error}</Text>
                    )}
                  </Space>
                </List.Item>
              )}
            />
          )}
        </Card>
      </Card>

      <Card
        title={
          <span style={{ color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
            <LockOutlined style={{ color: '#fa8c16' }} />
            安全设置
          </span>
        }
        style={{ backgroundColor: '#161B22', borderColor: '#30363D', borderRadius: 16, marginBottom: 24 }}
        styles={{ header: { borderBottom: '1px solid #30363D' }, body: { padding: 24 } }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <Text style={{ color: '#E8E8E8', fontSize: 15 }}>修改密码</Text>
            <div>
              <Text style={{ color: '#6B8E9F', fontSize: 13 }}>
                定期修改密码可以提高账户安全性
              </Text>
            </div>
          </div>
          <Button icon={<KeyOutlined />} onClick={() => setPasswordModalVisible(true)} style={{ borderColor: '#30363D' }}>
            修改密码
          </Button>
        </div>

        <Divider style={{ borderColor: '#30363D' }} />

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <Text style={{ color: '#E8E8E8', fontSize: 15 }}>登录通知</Text>
            <div>
              <Text style={{ color: '#6B8E9F', fontSize: 13 }}>
                当账户在新设备上登录时发送邮件通知
              </Text>
            </div>
          </div>
          <Switch
            checked={notificationPreferences.login_alert}
            onChange={(checked) =>
              setNotificationPreferences((prev) => ({ ...prev, login_alert: checked }))
            }
          />
        </div>

        <div style={{ marginTop: 16, display: 'flex', justifyContent: 'flex-end' }}>
          <Button
            type="primary"
            icon={<SaveOutlined />}
            loading={savingNotifications}
            onClick={handleSaveNotificationPreferences}
            style={{ backgroundColor: '#4A90D9', borderColor: '#4A90D9' }}
          >
            保存通知偏好
          </Button>
        </div>
      </Card>

      <Card
        title={
          <span style={{ color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
            <BellOutlined style={{ color: '#eb2f96' }} />
            通知设置
          </span>
        }
        style={{ backgroundColor: '#161B22', borderColor: '#30363D', borderRadius: 16, marginBottom: 24 }}
        styles={{ header: { borderBottom: '1px solid #30363D' }, body: { padding: 24 } }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <div>
            <Text style={{ color: '#E8E8E8', fontSize: 15 }}>邀请通知</Text>
            <div>
              <Text style={{ color: '#6B8E9F', fontSize: 13 }}>
                收到导师邀请或学生申请时通知
              </Text>
            </div>
          </div>
          <Switch
            checked={notificationPreferences.invite_notification}
            onChange={(checked) =>
              setNotificationPreferences((prev) => ({ ...prev, invite_notification: checked }))
            }
          />
        </div>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <div>
            <Text style={{ color: '#E8E8E8', fontSize: 15 }}>公告通知</Text>
            <div>
              <Text style={{ color: '#6B8E9F', fontSize: 13 }}>
                收到新公告时通知
              </Text>
            </div>
          </div>
          <Switch
            checked={notificationPreferences.announcement_notification}
            onChange={(checked) =>
              setNotificationPreferences((prev) => ({ ...prev, announcement_notification: checked }))
            }
          />
        </div>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <Text style={{ color: '#E8E8E8', fontSize: 15 }}>系统通知</Text>
            <div>
              <Text style={{ color: '#6B8E9F', fontSize: 13 }}>
                重大系统更新和维护窗口时通知
              </Text>
            </div>
          </div>
          <Switch
            checked={notificationPreferences.system_notification}
            onChange={(checked) =>
              setNotificationPreferences((prev) => ({ ...prev, system_notification: checked }))
            }
          />
        </div>

        <div style={{ marginTop: 16, display: 'flex', justifyContent: 'flex-end' }}>
          <Button
            type="primary"
            icon={<SaveOutlined />}
            loading={savingNotifications}
            onClick={handleSaveNotificationPreferences}
            style={{ backgroundColor: '#eb2f96', borderColor: '#eb2f96' }}
          >
            保存通知偏好
          </Button>
        </div>
      </Card>

      <Modal
        title={<span style={{ color: '#E8E8E8' }}>修改密码</span>}
        open={passwordModalVisible}
        onCancel={() => {
          setPasswordModalVisible(false)
          passwordForm.resetFields()
        }}
        footer={null}
        styles={{
          content: { backgroundColor: '#161B22', borderColor: '#30363D' },
          header: { backgroundColor: '#161B22', borderBottom: '1px solid #30363D' },
        }}
      >
        <Form form={passwordForm} layout="vertical" onFinish={handleChangePassword}>
          <Form.Item
            name="current_password"
            label={<span style={{ color: '#8899A6' }}>当前密码</span>}
            rules={[{ required: true, message: '请输入当前密码' }]}
          >
            <Input.Password />
          </Form.Item>

          <Form.Item
            name="new_password"
            label={<span style={{ color: '#8899A6' }}>新密码</span>}
            rules={[
              { required: true, message: '请输入新密码' },
              { min: 10, message: '密码长度至少 10 位' },
              {
                pattern: /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9]).+$/,
                message: '密码需包含大小写字母、数字和特殊字符',
              },
            ]}
          >
            <Input.Password />
          </Form.Item>

          <Form.Item
            name="confirm_password"
            label={<span style={{ color: '#8899A6' }}>确认新密码</span>}
            rules={[{ required: true, message: '请输入确认密码' }]}
          >
            <Input.Password />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0 }}>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => setPasswordModalVisible(false)}>取消</Button>
              <Button type="primary" htmlType="submit" loading={changingPassword} style={{ backgroundColor: '#fa8c16', borderColor: '#fa8c16' }}>
                保存密码
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <style>{`
        .ant-select-selector {
          background-color: #0D1117 !important;
          border-color: #30363D !important;
          color: #E8E8E8 !important;
        }
        .ant-select-selection-item {
          color: #E8E8E8 !important;
        }
        .ant-select-dropdown {
          background-color: #161B22 !important;
        }
        .ant-select-item {
          color: #E8E8E8 !important;
        }
        .ant-select-item-option-active {
          background-color: #1C2128 !important;
        }
        .ant-select-item-option-selected {
          background-color: rgba(74, 144, 217, 0.2) !important;
        }
        .ant-input-password input {
          color: #E8E8E8 !important;
        }
        .ant-switch-checked {
          background-color: #4A90D9 !important;
        }
      `}</style>
    </div>
  )
}

export default SettingsPage

