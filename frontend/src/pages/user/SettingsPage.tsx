import React, { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Divider,
  Form,
  Input,
  List,
  message,
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

const SettingsPage: React.FC = () => {
  const { user, updateUser } = useAuthStore()
  const [llmForm] = Form.useForm()
  const [passwordForm] = Form.useForm()

  const [savingLLM, setSavingLLM] = useState(false)
  const [changingPassword, setChangingPassword] = useState(false)
  const [passwordModalVisible, setPasswordModalVisible] = useState(false)

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
    }
  }, [user, llmForm])

  const loadMcpConfig = async () => {
    setMcpLoading(true)
    try {
      const data = await mcpApi.getConfig()
      setMcpConfigText(JSON.stringify(data.claude_desktop_config || { mcpServers: {} }, null, 2))
      message.success('Loaded current MCP config')
    } catch (error: any) {
      message.error(error.response?.data?.detail || 'Failed to load MCP config')
    } finally {
      setMcpLoading(false)
    }
  }

  const loadMcpTemplates = async () => {
    try {
      const data = await mcpApi.getTemplates()
      const templates = data.templates || []
      setMcpTemplates(templates)
      if (templates.length > 0) {
        setSelectedTemplateId(templates[0].id)
      }
    } catch (error) {
      console.error('Failed to load MCP templates:', error)
    }
  }

  const applyTemplate = () => {
    const template = mcpTemplates.find((item) => item.id === selectedTemplateId)
    if (!template) {
      message.warning('Please select a template first')
      return
    }
    setMcpConfigText(JSON.stringify(template.claude_desktop_config, null, 2))
    message.success(`Applied template: ${template.title}`)
  }

  const handleValidateMcp = async () => {
    if (!mcpConfigText.trim()) {
      message.warning('Please input MCP JSON first')
      return
    }
    setMcpValidating(true)
    try {
      const result = await mcpApi.validateConfig({ raw_json: mcpConfigText })
      message.success(`Validation passed, parsed ${result.server_count} server(s)`)
    } catch (error: any) {
      message.error(error.response?.data?.detail || 'MCP validation failed')
    } finally {
      setMcpValidating(false)
    }
  }

  const handleSaveMcp = async () => {
    if (!mcpConfigText.trim()) {
      message.warning('Please input MCP JSON first')
      return
    }
    setMcpSaving(true)
    try {
      const result = await mcpApi.saveConfig({ raw_json: mcpConfigText })
      message.success(`${result.message} (${result.server_count} server(s))`)
    } catch (error: any) {
      message.error(error.response?.data?.detail || 'Failed to save MCP config')
    } finally {
      setMcpSaving(false)
    }
  }

  const handleRefreshMcpStatus = async () => {
    setMcpLoading(true)
    try {
      const data = await mcpApi.refreshStatus(true)
      setMcpStatus(data.servers || [])
      message.success(`Probe done: ${data.server_count} server(s), ${data.tool_count} tool(s)`)
    } catch (error: any) {
      message.error(error.response?.data?.detail || 'Failed to probe MCP servers')
    } finally {
      setMcpLoading(false)
    }
  }

  useEffect(() => {
    loadMcpTemplates()
    loadMcpConfig()
  }, [])

  const mcpReachableCount = useMemo(() => mcpStatus.filter((item) => item.reachable === true).length, [mcpStatus])
  const mcpToolCount = useMemo(() => mcpStatus.reduce((sum, item) => sum + (item.discovered_tools || 0), 0), [mcpStatus])

  const handleSaveLLMSettings = async (values: any) => {
    setSavingLLM(true)
    try {
      const response = await api.put('/api/users/profile', {
        preferred_llm_provider: values.preferred_llm_provider,
        preferences: {
          ...user?.preferences,
          ...values.preferences,
        },
      })
      updateUser(response.data)
      message.success('Settings saved')
    } catch (error: any) {
      message.error(error.response?.data?.detail || 'Failed to save settings')
    } finally {
      setSavingLLM(false)
    }
  }

  const handleChangePassword = async (values: any) => {
    if (values.new_password !== values.confirm_password) {
      message.error('Passwords do not match')
      return
    }

    setChangingPassword(true)
    try {
      await api.post('/api/auth/change-password', {
        current_password: values.current_password,
        new_password: values.new_password,
      })
      message.success('Password updated')
      setPasswordModalVisible(false)
      passwordForm.resetFields()
    } catch (error: any) {
      message.error(error.response?.data?.detail || 'Failed to update password')
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
        Settings
      </Title>

      <Card
        title={
          <span style={{ color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
            <RobotOutlined style={{ color: '#52c41a' }} />
            LLM Settings
          </span>
        }
        style={{ backgroundColor: '#161B22', borderColor: '#30363D', borderRadius: 16, marginBottom: 24 }}
        styles={{ header: { borderBottom: '1px solid #30363D' }, body: { padding: 24 } }}
      >
        <Form form={llmForm} layout="vertical" onFinish={handleSaveLLMSettings}>
          <Form.Item
            name="preferred_llm_provider"
            label={<span style={{ color: '#8899A6' }}>Preferred Provider</span>}
          >
            <Select style={{ width: '100%' }} dropdownStyle={{ backgroundColor: '#161B22', borderColor: '#30363D' }}>
              <Option value="deepseek">DeepSeek (deepseek-chat)</Option>
              <Option value="openai">OpenAI (GPT-4o)</Option>
              <Option value="aliyun">Aliyun (qwen-plus)</Option>
              <Option value="ollama">Local (Ollama)</Option>
            </Select>
          </Form.Item>

          <Alert
            message="Tip"
            description="Choose a default model provider for all assistant actions."
            type="info"
            showIcon
            style={{ backgroundColor: 'rgba(74, 144, 217, 0.1)', border: '1px solid rgba(74, 144, 217, 0.3)', marginBottom: 16 }}
          />

          <Button type="primary" icon={<SaveOutlined />} htmlType="submit" loading={savingLLM} style={{ backgroundColor: '#4A90D9' }}>
            Save Settings
          </Button>
        </Form>
      </Card>

      <Card
        title={
          <span style={{ color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
            <CloudServerOutlined style={{ color: '#13c2c2' }} />
            MCP Server Config
          </span>
        }
        style={{ backgroundColor: '#161B22', borderColor: '#30363D', borderRadius: 16, marginBottom: 24 }}
        styles={{ header: { borderBottom: '1px solid #30363D' }, body: { padding: 24 } }}
      >
        <Alert
          message="MCP Config Guide"
          description="Workflow: apply template -> validate JSON -> save config -> refresh connectivity status."
          type="info"
          showIcon
          style={{ backgroundColor: 'rgba(19, 194, 194, 0.08)', border: '1px solid rgba(19, 194, 194, 0.35)', marginBottom: 16 }}
        />

        <Row gutter={12} style={{ marginBottom: 12 }}>
          <Col xs={24} md={8}>
            <Card size="small" style={{ background: '#0D1117', borderColor: '#30363D' }}>
              <Statistic title={<span style={{ color: '#8b949e' }}>Servers</span>} value={mcpStatus.length} valueStyle={{ color: '#e6edf3', fontSize: 20 }} />
            </Card>
          </Col>
          <Col xs={24} md={8}>
            <Card size="small" style={{ background: '#0D1117', borderColor: '#30363D' }}>
              <Statistic title={<span style={{ color: '#8b949e' }}>Reachable</span>} value={mcpReachableCount} valueStyle={{ color: '#52c41a', fontSize: 20 }} />
            </Card>
          </Col>
          <Col xs={24} md={8}>
            <Card size="small" style={{ background: '#0D1117', borderColor: '#30363D' }}>
              <Statistic title={<span style={{ color: '#8b949e' }}>Tools</span>} value={mcpToolCount} valueStyle={{ color: '#13c2c2', fontSize: 20 }} />
            </Card>
          </Col>
        </Row>

        <Space wrap style={{ marginBottom: 12 }}>
          <Select
            value={selectedTemplateId}
            onChange={setSelectedTemplateId}
            style={{ width: 320 }}
            placeholder="Select MCP template"
            options={mcpTemplates.map((item) => ({ value: item.id, label: item.title }))}
          />
          <Button onClick={applyTemplate}>Apply Template</Button>
          <Button icon={<ReloadOutlined />} loading={mcpLoading} onClick={loadMcpConfig}>Load Current</Button>
          <Button icon={<CheckCircleOutlined />} loading={mcpValidating} onClick={handleValidateMcp}>Validate</Button>
          <Button type="primary" icon={<SaveOutlined />} loading={mcpSaving} onClick={handleSaveMcp}>Save</Button>
          <Button loading={mcpLoading} onClick={handleRefreshMcpStatus}>Refresh Status</Button>
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
              No status yet. Click "Refresh Status" to probe MCP servers.
            </Text>
          ) : (
            <List
              dataSource={mcpStatus}
              renderItem={(item) => (
                <List.Item>
                  <Space direction="vertical" size={2} style={{ width: '100%' }}>
                    <Space>
                      <Tag color={item.reachable ? 'success' : item.reachable === false ? 'error' : 'default'}>
                        {item.reachable ? 'Reachable' : item.reachable === false ? 'Unreachable' : 'Unknown'}
                      </Tag>
                      <Text style={{ color: '#E8E8E8' }}>{item.name}</Text>
                      <Text style={{ color: '#8b949e', fontSize: 12 }}>transport: {item.transport}</Text>
                      <Text style={{ color: '#8b949e', fontSize: 12 }}>tools: {item.discovered_tools}</Text>
                    </Space>
                    {item.last_error && (
                      <Text style={{ color: '#ff7875', fontSize: 12 }}>error: {item.last_error}</Text>
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
            Security
          </span>
        }
        style={{ backgroundColor: '#161B22', borderColor: '#30363D', borderRadius: 16, marginBottom: 24 }}
        styles={{ header: { borderBottom: '1px solid #30363D' }, body: { padding: 24 } }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <Text style={{ color: '#E8E8E8', fontSize: 15 }}>Change Password</Text>
            <div>
              <Text style={{ color: '#6B8E9F', fontSize: 13 }}>
                Update password regularly to improve account security.
              </Text>
            </div>
          </div>
          <Button icon={<KeyOutlined />} onClick={() => setPasswordModalVisible(true)} style={{ borderColor: '#30363D' }}>
            Change Password
          </Button>
        </div>

        <Divider style={{ borderColor: '#30363D' }} />

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <Text style={{ color: '#E8E8E8', fontSize: 15 }}>Login Alert</Text>
            <div>
              <Text style={{ color: '#6B8E9F', fontSize: 13 }}>
                Notify by email when a new device logs in.
              </Text>
            </div>
          </div>
          <Switch defaultChecked disabled />
        </div>
      </Card>

      <Card
        title={
          <span style={{ color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
            <BellOutlined style={{ color: '#eb2f96' }} />
            Notification Settings
          </span>
        }
        style={{ backgroundColor: '#161B22', borderColor: '#30363D', borderRadius: 16, marginBottom: 24 }}
        styles={{ header: { borderBottom: '1px solid #30363D' }, body: { padding: 24 } }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <div>
            <Text style={{ color: '#E8E8E8', fontSize: 15 }}>Invitation Alerts</Text>
            <div>
              <Text style={{ color: '#6B8E9F', fontSize: 13 }}>
                Notify when receiving mentor/student invitations.
              </Text>
            </div>
          </div>
          <Switch defaultChecked />
        </div>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <div>
            <Text style={{ color: '#E8E8E8', fontSize: 15 }}>Announcement Alerts</Text>
            <div>
              <Text style={{ color: '#6B8E9F', fontSize: 13 }}>
                Notify when new announcements are published.
              </Text>
            </div>
          </div>
          <Switch defaultChecked />
        </div>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <Text style={{ color: '#E8E8E8', fontSize: 15 }}>System Alerts</Text>
            <div>
              <Text style={{ color: '#6B8E9F', fontSize: 13 }}>
                Notify on major system updates and maintenance windows.
              </Text>
            </div>
          </div>
          <Switch />
        </div>
      </Card>

      <Modal
        title={<span style={{ color: '#E8E8E8' }}>Change Password</span>}
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
            label={<span style={{ color: '#8899A6' }}>Current Password</span>}
            rules={[{ required: true, message: 'Please input current password' }]}
          >
            <Input.Password />
          </Form.Item>

          <Form.Item
            name="new_password"
            label={<span style={{ color: '#8899A6' }}>New Password</span>}
            rules={[
              { required: true, message: 'Please input new password' },
              { min: 6, message: 'Password must be at least 6 chars' },
            ]}
          >
            <Input.Password />
          </Form.Item>

          <Form.Item
            name="confirm_password"
            label={<span style={{ color: '#8899A6' }}>Confirm New Password</span>}
            rules={[{ required: true, message: 'Please confirm new password' }]}
          >
            <Input.Password />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0 }}>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => setPasswordModalVisible(false)}>Cancel</Button>
              <Button type="primary" htmlType="submit" loading={changingPassword} style={{ backgroundColor: '#fa8c16', borderColor: '#fa8c16' }}>
                Save Password
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
