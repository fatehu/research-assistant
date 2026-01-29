/**
 * 导师 - 公告管理页面
 */
import React, { useEffect, useState } from 'react';
import {
  Card, Table, Button, Space, Input, Modal, Form, message,
  Tag, Tooltip, Empty, Typography, Switch, Dropdown, Popconfirm, Select
} from 'antd';
import {
  PlusOutlined, EditOutlined, DeleteOutlined, PushpinOutlined,
  NotificationOutlined, MoreOutlined, ReloadOutlined, EyeOutlined
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import api from '../../services/api';

const { Title, Text, Paragraph } = Typography;
const { TextArea } = Input;
const { Option } = Select;

interface Announcement {
  id: number;
  mentor_id: number;
  mentor_name: string;
  group_id?: number;
  group_name?: string;
  title: string;
  content: string;
  is_pinned: boolean;
  is_active: boolean;
  is_read: boolean;
  created_at: string;
  updated_at: string;
}

interface ResearchGroup {
  id: number;
  name: string;
}

const AnnouncementsPage: React.FC = () => {
  const [announcements, setAnnouncements] = useState<Announcement[]>([]);
  const [groups, setGroups] = useState<ResearchGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalVisible, setModalVisible] = useState(false);
  const [editingAnnouncement, setEditingAnnouncement] = useState<Announcement | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [previewVisible, setPreviewVisible] = useState(false);
  const [previewContent, setPreviewContent] = useState<Announcement | null>(null);
  const [form] = Form.useForm();

  const fetchAnnouncements = async () => {
    setLoading(true);
    try {
      const response = await api.get('/api/announcements/');
      setAnnouncements(response.data);
    } catch (error) {
      message.error('获取公告失败');
    } finally {
      setLoading(false);
    }
  };

  const fetchGroups = async () => {
    try {
      const response = await api.get('/api/mentor/groups');
      setGroups(response.data);
    } catch (error) {
      console.error('获取研究组失败:', error);
    }
  };

  useEffect(() => {
    fetchAnnouncements();
    fetchGroups();
  }, []);

  const handleCreate = () => {
    setEditingAnnouncement(null);
    form.resetFields();
    setModalVisible(true);
  };

  const handleEdit = (record: Announcement) => {
    setEditingAnnouncement(record);
    form.setFieldsValue({
      title: record.title,
      content: record.content,
      group_id: record.group_id,
      is_pinned: record.is_pinned,
    });
    setModalVisible(true);
  };

  const handleSubmit = async (values: any) => {
    setSubmitting(true);
    try {
      if (editingAnnouncement) {
        await api.put(`/api/announcements/${editingAnnouncement.id}`, values);
        message.success('公告已更新');
      } else {
        await api.post('/api/announcements/', values);
        message.success('公告已发布');
      }
      setModalVisible(false);
      form.resetFields();
      fetchAnnouncements();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '操作失败');
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await api.delete(`/api/announcements/${id}`);
      message.success('公告已删除');
      fetchAnnouncements();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '删除失败');
    }
  };

  const handleTogglePin = async (record: Announcement) => {
    try {
      await api.put(`/api/announcements/${record.id}`, {
        is_pinned: !record.is_pinned,
      });
      message.success(record.is_pinned ? '已取消置顶' : '已置顶');
      fetchAnnouncements();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '操作失败');
    }
  };

  const handleToggleActive = async (record: Announcement) => {
    try {
      await api.put(`/api/announcements/${record.id}`, {
        is_active: !record.is_active,
      });
      message.success(record.is_active ? '已隐藏' : '已显示');
      fetchAnnouncements();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '操作失败');
    }
  };

  const handlePreview = (record: Announcement) => {
    setPreviewContent(record);
    setPreviewVisible(true);
  };

  const columns: ColumnsType<Announcement> = [
    {
      title: '标题',
      key: 'title',
      render: (_, record) => (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {record.is_pinned && (
            <Tooltip title="置顶">
              <PushpinOutlined style={{ color: '#fa8c16' }} />
            </Tooltip>
          )}
          <span 
            style={{ color: '#E8E8E8', cursor: 'pointer' }}
            onClick={() => handlePreview(record)}
          >
            {record.title}
          </span>
          {!record.is_active && (
            <Tag color="default" style={{ marginLeft: 8 }}>已隐藏</Tag>
          )}
        </div>
      ),
    },
    {
      title: '发布范围',
      key: 'scope',
      width: 150,
      render: (_, record) => (
        <Tag color={record.group_id ? 'blue' : 'green'}>
          {record.group_name || '所有学生'}
        </Tag>
      ),
    },
    {
      title: '发布时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: (text: string) => (
        <Text style={{ color: '#8899A6', fontSize: 13 }}>
          {new Date(text).toLocaleString('zh-CN')}
        </Text>
      ),
    },
    {
      title: '状态',
      key: 'status',
      width: 100,
      render: (_, record) => (
        <Switch
          checked={record.is_active}
          checkedChildren="显示"
          unCheckedChildren="隐藏"
          onChange={() => handleToggleActive(record)}
          size="small"
        />
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 150,
      render: (_, record) => (
        <Space size="small">
          <Tooltip title="预览">
            <Button
              type="text"
              size="small"
              icon={<EyeOutlined />}
              onClick={() => handlePreview(record)}
              style={{ color: '#8899A6' }}
            />
          </Tooltip>
          <Tooltip title={record.is_pinned ? '取消置顶' : '置顶'}>
            <Button
              type="text"
              size="small"
              icon={<PushpinOutlined />}
              onClick={() => handleTogglePin(record)}
              style={{ color: record.is_pinned ? '#fa8c16' : '#8899A6' }}
            />
          </Tooltip>
          <Tooltip title="编辑">
            <Button
              type="text"
              size="small"
              icon={<EditOutlined />}
              onClick={() => handleEdit(record)}
              style={{ color: '#4A90D9' }}
            />
          </Tooltip>
          <Popconfirm
            title="确定要删除这条公告吗？"
            onConfirm={() => handleDelete(record.id)}
            okText="确定"
            cancelText="取消"
          >
            <Tooltip title="删除">
              <Button
                type="text"
                size="small"
                icon={<DeleteOutlined />}
                style={{ color: '#ff4d4f' }}
              />
            </Tooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ 
      padding: '20px 24px',
      minHeight: '100vh',
      background: 'linear-gradient(180deg, #0D1117 0%, #161B22 100%)',
    }}>
      {/* 页面标题 */}
      <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Title level={3} style={{ 
          margin: 0, 
          color: '#E8E8E8',
          fontWeight: 600,
          display: 'flex',
          alignItems: 'center',
          gap: 10,
        }}>
          <NotificationOutlined style={{ color: '#52c41a' }} />
          公告管理
        </Title>
        <Space>
          <Button 
            icon={<ReloadOutlined />}
            onClick={fetchAnnouncements}
            style={{ borderColor: '#30363D' }}
          >
            刷新
          </Button>
          <Button 
            type="primary" 
            icon={<PlusOutlined />}
            onClick={handleCreate}
            style={{ backgroundColor: '#52c41a', borderColor: '#52c41a' }}
          >
            发布公告
          </Button>
        </Space>
      </div>

      {/* 公告列表 */}
      <Card
        style={{
          backgroundColor: '#161B22',
          borderColor: '#30363D',
          borderRadius: 12,
        }}
        bodyStyle={{ padding: 0 }}
      >
        <Table
          columns={columns}
          dataSource={announcements}
          rowKey="id"
          loading={loading}
          pagination={{
            pageSize: 10,
            showSizeChanger: false,
            showTotal: (total) => `共 ${total} 条公告`,
          }}
          locale={{
            emptyText: (
              <Empty 
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={<span style={{ color: '#8899A6' }}>暂无公告，点击右上角发布新公告</span>}
              />
            ),
          }}
        />
      </Card>

      {/* 创建/编辑公告弹窗 */}
      <Modal
        title={
          <Space>
            <NotificationOutlined style={{ color: '#52c41a' }} />
            <span style={{ color: '#E8E8E8' }}>
              {editingAnnouncement ? '编辑公告' : '发布公告'}
            </span>
          </Space>
        }
        open={modalVisible}
        onCancel={() => {
          setModalVisible(false);
          form.resetFields();
        }}
        footer={null}
        width={600}
        styles={{
          content: { backgroundColor: '#161B22', border: '1px solid #30363D' },
          header: { backgroundColor: '#161B22', borderBottom: '1px solid #30363D' },
          body: { backgroundColor: '#161B22' },
        }}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
          style={{ marginTop: 16 }}
        >
          <Form.Item
            name="title"
            label={<span style={{ color: '#8899A6' }}>公告标题</span>}
            rules={[{ required: true, message: '请输入公告标题' }]}
          >
            <Input
              placeholder="输入公告标题"
              maxLength={500}
              style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }}
            />
          </Form.Item>

          <Form.Item
            name="content"
            label={<span style={{ color: '#8899A6' }}>公告内容</span>}
            rules={[{ required: true, message: '请输入公告内容' }]}
          >
            <TextArea
              rows={6}
              placeholder="输入公告内容，支持 Markdown 格式"
              style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }}
            />
          </Form.Item>

          <Form.Item
            name="group_id"
            label={<span style={{ color: '#8899A6' }}>发布范围</span>}
          >
            <Select
              placeholder="选择研究组（不选则发送给所有学生）"
              allowClear
              style={{ width: '100%' }}
              dropdownStyle={{ backgroundColor: '#161B22' }}
            >
              {groups.map(group => (
                <Option key={group.id} value={group.id}>{group.name}</Option>
              ))}
            </Select>
          </Form.Item>

          <Form.Item
            name="is_pinned"
            valuePropName="checked"
            initialValue={false}
          >
            <Space>
              <Switch />
              <span style={{ color: '#8899A6' }}>置顶公告</span>
            </Space>
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => {
                setModalVisible(false);
                form.resetFields();
              }}>
                取消
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={submitting}
                style={{ backgroundColor: '#52c41a', borderColor: '#52c41a' }}
              >
                {editingAnnouncement ? '保存修改' : '发布公告'}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* 预览弹窗 */}
      <Modal
        title={
          <Space>
            <EyeOutlined style={{ color: '#4A90D9' }} />
            <span style={{ color: '#E8E8E8' }}>公告预览</span>
          </Space>
        }
        open={previewVisible}
        onCancel={() => setPreviewVisible(false)}
        footer={null}
        width={600}
        styles={{
          content: { backgroundColor: '#161B22', border: '1px solid #30363D' },
          header: { backgroundColor: '#161B22', borderBottom: '1px solid #30363D' },
          body: { backgroundColor: '#161B22' },
        }}
      >
        {previewContent && (
          <div>
            <div style={{ marginBottom: 16 }}>
              <Title level={4} style={{ color: '#E8E8E8', margin: 0 }}>
                {previewContent.is_pinned && (
                  <PushpinOutlined style={{ color: '#fa8c16', marginRight: 8 }} />
                )}
                {previewContent.title}
              </Title>
              <div style={{ marginTop: 8 }}>
                <Tag color={previewContent.group_id ? 'blue' : 'green'}>
                  {previewContent.group_name || '所有学生'}
                </Tag>
                <Text style={{ color: '#6B8E9F', fontSize: 12, marginLeft: 12 }}>
                  发布于 {new Date(previewContent.created_at).toLocaleString('zh-CN')}
                </Text>
              </div>
            </div>
            <div 
              style={{ 
                padding: 16, 
                backgroundColor: '#0D1117', 
                borderRadius: 8,
                border: '1px solid #30363D',
              }}
            >
              <Paragraph style={{ color: '#CBD5E1', margin: 0, whiteSpace: 'pre-wrap' }}>
                {previewContent.content}
              </Paragraph>
            </div>
          </div>
        )}
      </Modal>

      {/* 样式覆盖 */}
      <style>{`
        .ant-table {
          background: transparent !important;
        }
        .ant-table-thead > tr > th {
          background: #1C2128 !important;
          color: #8899A6 !important;
          border-bottom: 1px solid #30363D !important;
        }
        .ant-table-tbody > tr > td {
          border-bottom: 1px solid #30363D !important;
          background: transparent !important;
        }
        .ant-table-tbody > tr:hover > td {
          background: #1C2128 !important;
        }
        .ant-input, .ant-input-affix-wrapper, .ant-select-selector {
          background-color: #0D1117 !important;
          border-color: #30363D !important;
          color: #E8E8E8 !important;
        }
        .ant-select-selection-item, .ant-select-selection-placeholder {
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
        .ant-pagination-item a {
          color: #8899A6 !important;
        }
        .ant-pagination-item-active {
          border-color: #4A90D9 !important;
        }
        .ant-pagination-item-active a {
          color: #4A90D9 !important;
        }
      `}</style>
    </div>
  );
};

export default AnnouncementsPage;
