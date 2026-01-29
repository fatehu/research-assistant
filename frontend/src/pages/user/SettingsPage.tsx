/**
 * 设置页面
 */
import React, { useState, useEffect } from 'react';
import {
  Card, Form, Input, Button, Select, Switch, message, Typography, Space, Divider, Row, Col, Modal, Alert
} from 'antd';
import {
  SettingOutlined, LockOutlined, BellOutlined, BgColorsOutlined,
  RobotOutlined, SaveOutlined, KeyOutlined
} from '@ant-design/icons';
import { useAuthStore } from '../../stores/authStore';
import api from '../../services/api';

const { Title, Text } = Typography;
const { Option } = Select;

const SettingsPage: React.FC = () => {
  const { user, updateUser } = useAuthStore();
  const [llmForm] = Form.useForm();
  const [passwordForm] = Form.useForm();
  const [savingLLM, setSavingLLM] = useState(false);
  const [changingPassword, setChangingPassword] = useState(false);
  const [passwordModalVisible, setPasswordModalVisible] = useState(false);

  useEffect(() => {
    if (user) {
      llmForm.setFieldsValue({
        preferred_llm_provider: user.preferred_llm_provider || 'openai',
      });
    }
  }, [user, llmForm]);

  const handleSaveLLMSettings = async (values: any) => {
    setSavingLLM(true);
    try {
      const response = await api.put('/api/users/profile', {
        preferred_llm_provider: values.preferred_llm_provider,
        preferences: {
          ...user?.preferences,
          ...values.preferences,
        },
      });
      updateUser(response.data);
      message.success('设置已保存');
    } catch (error: any) {
      message.error(error.response?.data?.detail || '保存失败');
    } finally {
      setSavingLLM(false);
    }
  };

  const handleChangePassword = async (values: any) => {
    if (values.new_password !== values.confirm_password) {
      message.error('两次输入的密码不一致');
      return;
    }
    
    setChangingPassword(true);
    try {
      await api.post('/api/auth/change-password', {
        current_password: values.current_password,
        new_password: values.new_password,
      });
      message.success('密码已修改');
      setPasswordModalVisible(false);
      passwordForm.resetFields();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '密码修改失败');
    } finally {
      setChangingPassword(false);
    }
  };

  return (
    <div style={{ 
      padding: 24, 
      maxWidth: 800, 
      margin: '0 auto',
      minHeight: '100%',
    }}>
      <Title level={3} style={{ color: '#E8E8E8', marginBottom: 24, display: 'flex', alignItems: 'center', gap: 12 }}>
        <SettingOutlined style={{ color: '#4A90D9' }} />
        设置
      </Title>

      {/* AI 模型设置 */}
      <Card
        title={
          <span style={{ color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
            <RobotOutlined style={{ color: '#52c41a' }} />
            AI 模型设置
          </span>
        }
        style={{
          backgroundColor: '#161B22',
          borderColor: '#30363D',
          borderRadius: 16,
          marginBottom: 24,
        }}
        styles={{
          header: { borderBottom: '1px solid #30363D' },
          body: { padding: 24 },
        }}
      >
        <Form
          form={llmForm}
          layout="vertical"
          onFinish={handleSaveLLMSettings}
        >
          <Form.Item
            name="preferred_llm_provider"
            label={<span style={{ color: '#8899A6' }}>首选 AI 服务商</span>}
          >
            <Select
              style={{ width: '100%' }}
              dropdownStyle={{ backgroundColor: '#161B22', borderColor: '#30363D' }}
            >
              <Option value="deepseek">DeepSeek (deepseek-chat)</Option>
              <Option value="openai">OpenAI (GPT-4o)</Option>
              <Option value="aliyun">阿里云通义千问 (qwen-plus)</Option>
              <Option value="ollama">本地模型 (Ollama)</Option>
            </Select>
          </Form.Item>

          <Alert
            message="提示"
            description="选择您偏好的 AI 服务商。不同服务商可能有不同的响应速度和能力特点。"
            type="info"
            showIcon
            style={{ 
              backgroundColor: 'rgba(74, 144, 217, 0.1)', 
              border: '1px solid rgba(74, 144, 217, 0.3)',
              marginBottom: 16,
            }}
          />

          <Button
            type="primary"
            icon={<SaveOutlined />}
            htmlType="submit"
            loading={savingLLM}
            style={{ backgroundColor: '#4A90D9' }}
          >
            保存设置
          </Button>
        </Form>
      </Card>

      {/* 安全设置 */}
      <Card
        title={
          <span style={{ color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
            <LockOutlined style={{ color: '#fa8c16' }} />
            安全设置
          </span>
        }
        style={{
          backgroundColor: '#161B22',
          borderColor: '#30363D',
          borderRadius: 16,
          marginBottom: 24,
        }}
        styles={{
          header: { borderBottom: '1px solid #30363D' },
          body: { padding: 24 },
        }}
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
          <Button
            icon={<KeyOutlined />}
            onClick={() => setPasswordModalVisible(true)}
            style={{ borderColor: '#30363D' }}
          >
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
          <Switch defaultChecked disabled />
        </div>
      </Card>

      {/* 通知设置 */}
      <Card
        title={
          <span style={{ color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
            <BellOutlined style={{ color: '#eb2f96' }} />
            通知设置
          </span>
        }
        style={{
          backgroundColor: '#161B22',
          borderColor: '#30363D',
          borderRadius: 16,
          marginBottom: 24,
        }}
        styles={{
          header: { borderBottom: '1px solid #30363D' },
          body: { padding: 24 },
        }}
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
          <Switch defaultChecked />
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
          <Switch defaultChecked />
        </div>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <Text style={{ color: '#E8E8E8', fontSize: 15 }}>资源共享通知</Text>
            <div>
              <Text style={{ color: '#6B8E9F', fontSize: 13 }}>
                收到新的共享资源时通知
              </Text>
            </div>
          </div>
          <Switch defaultChecked />
        </div>
      </Card>

      {/* 界面设置 */}
      <Card
        title={
          <span style={{ color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
            <BgColorsOutlined style={{ color: '#722ed1' }} />
            界面设置
          </span>
        }
        style={{
          backgroundColor: '#161B22',
          borderColor: '#30363D',
          borderRadius: 16,
        }}
        styles={{
          header: { borderBottom: '1px solid #30363D' },
          body: { padding: 24 },
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <div>
            <Text style={{ color: '#E8E8E8', fontSize: 15 }}>深色模式</Text>
            <div>
              <Text style={{ color: '#6B8E9F', fontSize: 13 }}>
                使用深色主题（当前已启用）
              </Text>
            </div>
          </div>
          <Switch checked disabled />
        </div>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <Text style={{ color: '#E8E8E8', fontSize: 15 }}>紧凑模式</Text>
            <div>
              <Text style={{ color: '#6B8E9F', fontSize: 13 }}>
                减少界面间距以显示更多内容
              </Text>
            </div>
          </div>
          <Switch />
        </div>
      </Card>

      {/* 修改密码弹窗 */}
      <Modal
        title={
          <span style={{ color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
            <KeyOutlined style={{ color: '#fa8c16' }} />
            修改密码
          </span>
        }
        open={passwordModalVisible}
        onCancel={() => {
          setPasswordModalVisible(false);
          passwordForm.resetFields();
        }}
        footer={null}
        width={400}
        styles={{
          content: { backgroundColor: '#161B22', border: '1px solid #30363D' },
          header: { backgroundColor: '#161B22', borderBottom: '1px solid #30363D' },
          body: { backgroundColor: '#161B22' },
        }}
      >
        <Form
          form={passwordForm}
          layout="vertical"
          onFinish={handleChangePassword}
          style={{ marginTop: 16 }}
        >
          <Form.Item
            name="current_password"
            label={<span style={{ color: '#8899A6' }}>当前密码</span>}
            rules={[{ required: true, message: '请输入当前密码' }]}
          >
            <Input.Password
              prefix={<LockOutlined style={{ color: '#8899A6' }} />}
              placeholder="输入当前密码"
              style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }}
            />
          </Form.Item>

          <Form.Item
            name="new_password"
            label={<span style={{ color: '#8899A6' }}>新密码</span>}
            rules={[
              { required: true, message: '请输入新密码' },
              { min: 6, message: '密码至少6位' },
            ]}
          >
            <Input.Password
              prefix={<LockOutlined style={{ color: '#8899A6' }} />}
              placeholder="输入新密码（至少6位）"
              style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }}
            />
          </Form.Item>

          <Form.Item
            name="confirm_password"
            label={<span style={{ color: '#8899A6' }}>确认新密码</span>}
            rules={[
              { required: true, message: '请再次输入新密码' },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('new_password') === value) {
                    return Promise.resolve();
                  }
                  return Promise.reject(new Error('两次输入的密码不一致'));
                },
              }),
            ]}
          >
            <Input.Password
              prefix={<LockOutlined style={{ color: '#8899A6' }} />}
              placeholder="再次输入新密码"
              style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }}
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0 }}>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => {
                setPasswordModalVisible(false);
                passwordForm.resetFields();
              }}>
                取消
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={changingPassword}
                style={{ backgroundColor: '#fa8c16', borderColor: '#fa8c16' }}
              >
                确认修改
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* 样式覆盖 */}
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
  );
};

export default SettingsPage;
