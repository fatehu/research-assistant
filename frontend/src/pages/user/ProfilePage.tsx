/**
 * 个人资料页面
 */
import React, { useState, useEffect } from 'react';
import {
  Card, Form, Input, Button, Avatar, message, Spin, Typography, Space, Divider, Row, Col, Tag
} from 'antd';
import {
  UserOutlined, MailOutlined, EditOutlined, SaveOutlined,
  BankOutlined, ExperimentOutlined, FileTextOutlined,
  CrownOutlined, CalendarOutlined
} from '@ant-design/icons';
import { useAuthStore } from '../../stores/authStore';
import api from '../../services/api';

const { Title, Text } = Typography;
const { TextArea } = Input;

const roleLabels: Record<string, string> = {
  admin: '管理员',
  mentor: '导师',
  student: '学生',
};

const roleColors: Record<string, string> = {
  admin: '#D4AF37',
  mentor: '#52c41a',
  student: '#4A90D9',
};

const ProfilePage: React.FC = () => {
  const { user, updateUser } = useAuthStore();
  const [form] = Form.useForm();
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (user) {
      form.setFieldsValue({
        full_name: user.full_name || '',
        bio: user.bio || '',
        department: user.department || '',
        research_direction: user.research_direction || '',
      });
    }
  }, [user, form]);

  const handleSave = async (values: any) => {
    setSaving(true);
    try {
      const response = await api.put('/api/users/profile', values);
      updateUser(response.data);
      message.success('个人资料已更新');
      setEditing(false);
    } catch (error: any) {
      message.error(error.response?.data?.detail || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  if (!user) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100%' }}>
        <Spin size="large" />
      </div>
    );
  }

  return (
    <div style={{ 
      padding: 24, 
      maxWidth: 900, 
      margin: '0 auto',
      minHeight: '100%',
    }}>
      <Title level={3} style={{ color: '#E8E8E8', marginBottom: 24, display: 'flex', alignItems: 'center', gap: 12 }}>
        <UserOutlined style={{ color: '#4A90D9' }} />
        个人资料
      </Title>

      <Row gutter={24}>
        {/* 左侧：头像和基本信息 */}
        <Col xs={24} md={8}>
          <Card
            style={{
              backgroundColor: '#161B22',
              borderColor: '#30363D',
              borderRadius: 16,
              textAlign: 'center',
            }}
          >
            <div style={{ position: 'relative', display: 'inline-block', marginBottom: 20 }}>
              <Avatar
                size={120}
                src={user.avatar}
                icon={<UserOutlined />}
                style={{ 
                  backgroundColor: roleColors[user.role] || '#4A90D9',
                  border: `3px solid ${roleColors[user.role] || '#4A90D9'}`,
                }}
              />
            </div>

            <Title level={4} style={{ color: '#E8E8E8', marginBottom: 4 }}>
              {user.full_name || user.username}
            </Title>
            <Text style={{ color: '#8899A6' }}>@{user.username}</Text>

            <div style={{ marginTop: 16 }}>
              <Tag 
                icon={<CrownOutlined />}
                color={roleColors[user.role]}
                style={{ fontSize: 13, padding: '4px 12px' }}
              >
                {roleLabels[user.role] || '用户'}
              </Tag>
            </div>

            <Divider style={{ borderColor: '#30363D', margin: '20px 0' }} />

            <div style={{ textAlign: 'left' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
                <MailOutlined style={{ color: '#8899A6' }} />
                <Text style={{ color: '#CBD5E1' }}>{user.email}</Text>
              </div>
              {user.department && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
                  <BankOutlined style={{ color: '#8899A6' }} />
                  <Text style={{ color: '#CBD5E1' }}>{user.department}</Text>
                </div>
              )}
              {user.research_direction && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
                  <ExperimentOutlined style={{ color: '#8899A6' }} />
                  <Text style={{ color: '#CBD5E1' }}>{user.research_direction}</Text>
                </div>
              )}
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <CalendarOutlined style={{ color: '#8899A6' }} />
                <Text style={{ color: '#8899A6', fontSize: 12 }}>
                  注册于 {new Date(user.created_at).toLocaleDateString('zh-CN')}
                </Text>
              </div>
            </div>
          </Card>
        </Col>

        {/* 右侧：编辑表单 */}
        <Col xs={24} md={16}>
          <Card
            title={
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
                  <FileTextOutlined style={{ color: '#4A90D9' }} />
                  详细信息
                </span>
                {!editing ? (
                  <Button
                    type="text"
                    icon={<EditOutlined />}
                    onClick={() => setEditing(true)}
                    style={{ color: '#4A90D9' }}
                  >
                    编辑
                  </Button>
                ) : (
                  <Space>
                    <Button onClick={() => { setEditing(false); form.resetFields(); }}>
                      取消
                    </Button>
                    <Button
                      type="primary"
                      icon={<SaveOutlined />}
                      onClick={() => form.submit()}
                      loading={saving}
                      style={{ backgroundColor: '#4A90D9' }}
                    >
                      保存
                    </Button>
                  </Space>
                )}
              </div>
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
            <Form
              form={form}
              layout="vertical"
              onFinish={handleSave}
              disabled={!editing}
            >
              <Form.Item
                name="full_name"
                label={<span style={{ color: '#8899A6' }}>姓名</span>}
              >
                <Input
                  prefix={<UserOutlined style={{ color: '#8899A6' }} />}
                  placeholder="您的真实姓名"
                  style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }}
                />
              </Form.Item>

              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item
                    name="department"
                    label={<span style={{ color: '#8899A6' }}>部门/院系</span>}
                  >
                    <Input
                      prefix={<BankOutlined style={{ color: '#8899A6' }} />}
                      placeholder="所属部门或院系"
                      style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    name="research_direction"
                    label={<span style={{ color: '#8899A6' }}>研究方向</span>}
                  >
                    <Input
                      prefix={<ExperimentOutlined style={{ color: '#8899A6' }} />}
                      placeholder="您的研究领域"
                      style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }}
                    />
                  </Form.Item>
                </Col>
              </Row>

              <Form.Item
                name="bio"
                label={<span style={{ color: '#8899A6' }}>个人简介</span>}
              >
                <TextArea
                  rows={4}
                  placeholder="介绍一下自己..."
                  style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }}
                  maxLength={500}
                  showCount
                />
              </Form.Item>
            </Form>

            {/* 账户信息（只读） */}
            <Divider style={{ borderColor: '#30363D' }}>
              <Text style={{ color: '#6B8E9F', fontSize: 12 }}>账户信息</Text>
            </Divider>

            <Row gutter={16}>
              <Col span={12}>
                <div style={{ marginBottom: 16 }}>
                  <Text style={{ color: '#6B8E9F', fontSize: 12 }}>用户名</Text>
                  <div style={{ 
                    padding: '8px 12px', 
                    backgroundColor: '#0D1117', 
                    borderRadius: 8,
                    border: '1px solid #30363D',
                    marginTop: 4,
                  }}>
                    <Text style={{ color: '#8899A6' }}>@{user.username}</Text>
                  </div>
                </div>
              </Col>
              <Col span={12}>
                <div style={{ marginBottom: 16 }}>
                  <Text style={{ color: '#6B8E9F', fontSize: 12 }}>邮箱地址</Text>
                  <div style={{ 
                    padding: '8px 12px', 
                    backgroundColor: '#0D1117', 
                    borderRadius: 8,
                    border: '1px solid #30363D',
                    marginTop: 4,
                  }}>
                    <Text style={{ color: '#8899A6' }}>{user.email}</Text>
                  </div>
                </div>
              </Col>
            </Row>

            <Row gutter={16}>
              <Col span={12}>
                <div>
                  <Text style={{ color: '#6B8E9F', fontSize: 12 }}>注册时间</Text>
                  <div style={{ 
                    padding: '8px 12px', 
                    backgroundColor: '#0D1117', 
                    borderRadius: 8,
                    border: '1px solid #30363D',
                    marginTop: 4,
                  }}>
                    <Text style={{ color: '#8899A6' }}>
                      {new Date(user.created_at).toLocaleString('zh-CN')}
                    </Text>
                  </div>
                </div>
              </Col>
              <Col span={12}>
                <div>
                  <Text style={{ color: '#6B8E9F', fontSize: 12 }}>最近登录</Text>
                  <div style={{ 
                    padding: '8px 12px', 
                    backgroundColor: '#0D1117', 
                    borderRadius: 8,
                    border: '1px solid #30363D',
                    marginTop: 4,
                  }}>
                    <Text style={{ color: '#8899A6' }}>
                      {user.last_login 
                        ? new Date(user.last_login).toLocaleString('zh-CN')
                        : '从未登录'
                      }
                    </Text>
                  </div>
                </div>
              </Col>
            </Row>
          </Card>
        </Col>
      </Row>

      {/* 样式覆盖 */}
      <style>{`
        .ant-input, .ant-input-affix-wrapper {
          color: #E8E8E8 !important;
        }
        .ant-input::placeholder {
          color: #6B8E9F !important;
        }
        .ant-input-disabled, .ant-input-affix-wrapper-disabled {
          background-color: #0D1117 !important;
          color: #8899A6 !important;
        }
        .ant-input-textarea-show-count::after {
          color: #6B8E9F !important;
        }
      `}</style>
    </div>
  );
};

export default ProfilePage;
