/**
 * 导师 - 研究组管理页面
 * 设计风格: 现代学府 - 深色学术主题配合渐变高亮
 */
import React, { useEffect, useState } from 'react';
import { 
  Card, Button, Space, Input, Modal, Form, message,
  Avatar, Tag, Empty, Spin, Row, Col, Typography, List,
  Dropdown, Tooltip, Badge, Progress, InputNumber
} from 'antd';
import {
  TeamOutlined, PlusOutlined, EditOutlined, DeleteOutlined,
  UserOutlined, SettingOutlined, MoreOutlined, UsergroupAddOutlined,
  CrownOutlined, CheckOutlined, CloseOutlined
} from '@ant-design/icons';
import { useRoleStore, ResearchGroup, StudentDetail } from '../../stores/roleStore';

const { Title, Text, Paragraph } = Typography;
const { TextArea } = Input;

const GroupsPage: React.FC = () => {
  const {
    groups, groupsLoading, students, studentsLoading,
    fetchGroups, fetchStudents, createGroup, updateGroup, deleteGroup,
    addGroupMember, removeGroupMember
  } = useRoleStore();

  const [createModalVisible, setCreateModalVisible] = useState(false);
  const [editModalVisible, setEditModalVisible] = useState(false);
  const [membersModalVisible, setMembersModalVisible] = useState(false);
  const [selectedGroup, setSelectedGroup] = useState<ResearchGroup | null>(null);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    fetchGroups();
    fetchStudents();
  }, []);

  const handleCreate = async (values: { name: string; description?: string; max_members?: number }) => {
    setSubmitting(true);
    try {
      await createGroup(values.name, values.description, values.max_members);
      message.success('研究组创建成功');
      setCreateModalVisible(false);
      createForm.resetFields();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '创建失败');
    } finally {
      setSubmitting(false);
    }
  };

  const handleEdit = async (values: { name: string; description?: string; max_members?: number }) => {
    if (!selectedGroup) return;
    setSubmitting(true);
    try {
      await updateGroup(selectedGroup.id, values);
      message.success('研究组更新成功');
      setEditModalVisible(false);
    } catch (error: any) {
      message.error(error.response?.data?.detail || '更新失败');
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = (group: ResearchGroup) => {
    Modal.confirm({
      title: '确认删除研究组',
      content: (
        <div>
          <p>确定要删除研究组 <strong>"{group.name}"</strong> 吗？</p>
          <p style={{ color: '#ff7875', fontSize: 13 }}>删除后，组内成员关系将被解除，但成员账号不受影响。</p>
        </div>
      ),
      okText: '确认删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        try {
          await deleteGroup(group.id);
          message.success('研究组已删除');
        } catch (error: any) {
          message.error(error.response?.data?.detail || '删除失败');
        }
      },
    });
  };

  const handleAddMember = async (studentId: number) => {
    if (!selectedGroup) return;
    try {
      await addGroupMember(selectedGroup.id, studentId);
      message.success('成员添加成功');
    } catch (error: any) {
      message.error(error.response?.data?.detail || '添加失败');
    }
  };

  const handleRemoveMember = async (studentId: number) => {
    if (!selectedGroup) return;
    try {
      await removeGroupMember(selectedGroup.id, studentId);
      message.success('成员已移除');
    } catch (error: any) {
      message.error(error.response?.data?.detail || '移除失败');
    }
  };

  // 研究组卡片组件
  const GroupCard: React.FC<{ group: ResearchGroup }> = ({ group }) => {
    const memberCount = group.member_count || 0;
    const progress = (memberCount / group.max_members) * 100;

    return (
      <Card
        style={{
          backgroundColor: '#161B22',
          borderColor: '#30363D',
          borderRadius: 16,
          overflow: 'hidden',
        }}
        bodyStyle={{ padding: 0 }}
      >
        {/* 卡片头部 - 渐变背景 */}
        <div style={{
          background: 'linear-gradient(135deg, #4A90D920 0%, #13c2c210 100%)',
          padding: '24px',
          borderBottom: '1px solid #30363D',
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div style={{
                width: 48, height: 48, borderRadius: 12,
                backgroundColor: '#4A90D925',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: 22, color: '#4A90D9',
              }}>
                <TeamOutlined />
              </div>
              <div>
                <Title level={4} style={{ margin: 0, color: '#E8E8E8' }}>{group.name}</Title>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  创建于 {new Date(group.created_at).toLocaleDateString('zh-CN')}
                </Text>
              </div>
            </div>
            <Dropdown
              menu={{
                items: [
                  {
                    key: 'members',
                    icon: <UsergroupAddOutlined />,
                    label: '管理成员',
                    onClick: () => {
                      setSelectedGroup(group);
                      setMembersModalVisible(true);
                    },
                  },
                  {
                    key: 'edit',
                    icon: <EditOutlined />,
                    label: '编辑信息',
                    onClick: () => {
                      setSelectedGroup(group);
                      editForm.setFieldsValue(group);
                      setEditModalVisible(true);
                    },
                  },
                  { type: 'divider' },
                  {
                    key: 'delete',
                    icon: <DeleteOutlined />,
                    label: '删除研究组',
                    danger: true,
                    onClick: () => handleDelete(group),
                  },
                ],
              }}
            >
              <Button type="text" icon={<MoreOutlined />} style={{ color: '#8899A6' }} />
            </Dropdown>
          </div>
        </div>

        {/* 卡片内容 */}
        <div style={{ padding: '20px 24px' }}>
          {group.description ? (
            <Paragraph 
              style={{ color: '#B8C4CE', marginBottom: 20 }}
              ellipsis={{ rows: 2 }}
            >
              {group.description}
            </Paragraph>
          ) : (
            <Text type="secondary" style={{ display: 'block', marginBottom: 20, fontStyle: 'italic' }}>
              暂无描述
            </Text>
          )}

          {/* 成员进度 */}
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
              <Text style={{ color: '#8899A6', fontSize: 13 }}>成员容量</Text>
              <Text style={{ color: '#E8E8E8', fontSize: 13 }}>
                {memberCount} / {group.max_members}
              </Text>
            </div>
            <Progress 
              percent={progress} 
              showInfo={false}
              strokeColor={{
                '0%': '#4A90D9',
                '100%': '#13c2c2',
              }}
              trailColor="#30363D"
              style={{ marginBottom: 0 }}
            />
          </div>
        </div>

        {/* 卡片底部 */}
        <div style={{
          padding: '12px 24px',
          borderTop: '1px solid #30363D',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <Space>
            <Badge status={group.is_active ? 'success' : 'default'} />
            <Text style={{ color: '#8899A6', fontSize: 12 }}>
              {group.is_active ? '活跃中' : '已停用'}
            </Text>
          </Space>
          <Button 
            type="link" 
            size="small"
            onClick={() => {
              setSelectedGroup(group);
              setMembersModalVisible(true);
            }}
          >
            管理成员 →
          </Button>
        </div>
      </Card>
    );
  };

  return (
    <div style={{ 
      padding: '32px 40px',
      minHeight: '100vh',
      background: 'linear-gradient(180deg, #0D1117 0%, #161B22 100%)',
    }}>
      {/* 页面标题 */}
      <div style={{ 
        marginBottom: 32, 
        display: 'flex', 
        justifyContent: 'space-between', 
        alignItems: 'flex-start' 
      }}>
        <div>
          <Title level={2} style={{ 
            margin: 0, 
            color: '#E8E8E8',
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            gap: 12,
          }}>
            <TeamOutlined style={{ color: '#13c2c2' }} />
            研究组管理
          </Title>
          <Text style={{ color: '#8899A6', marginTop: 8, display: 'block' }}>
            创建和管理您的研究组，组织学生进行协作研究
          </Text>
        </div>
        <Button 
          type="primary" 
          icon={<PlusOutlined />}
          onClick={() => setCreateModalVisible(true)}
          size="large"
          style={{ 
            backgroundColor: '#13c2c2',
            borderRadius: 10,
            height: 44,
            paddingLeft: 24,
            paddingRight: 24,
          }}
        >
          创建研究组
        </Button>
      </div>

      {/* 研究组列表 */}
      <Spin spinning={groupsLoading}>
        {groups.length === 0 ? (
          <Card
            style={{
              backgroundColor: '#161B22',
              borderColor: '#30363D',
              borderRadius: 16,
            }}
          >
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                <div>
                  <Text style={{ color: '#8899A6', display: 'block', marginBottom: 16 }}>
                    您还没有创建任何研究组
                  </Text>
                  <Button 
                    type="primary"
                    icon={<PlusOutlined />}
                    onClick={() => setCreateModalVisible(true)}
                    style={{ backgroundColor: '#13c2c2' }}
                  >
                    创建第一个研究组
                  </Button>
                </div>
              }
            />
          </Card>
        ) : (
          <Row gutter={[24, 24]}>
            {groups.map(group => (
              <Col xs={24} md={12} xl={8} key={group.id}>
                <GroupCard group={group} />
              </Col>
            ))}
          </Row>
        )}
      </Spin>

      {/* 创建研究组弹窗 */}
      <Modal
        title={
          <Space>
            <PlusOutlined style={{ color: '#13c2c2' }} />
            <span style={{ color: '#E8E8E8' }}>创建研究组</span>
          </Space>
        }
        open={createModalVisible}
        onCancel={() => {
          setCreateModalVisible(false);
          createForm.resetFields();
        }}
        footer={null}
        width={480}
      >
        <Form
          form={createForm}
          layout="vertical"
          onFinish={handleCreate}
          style={{ marginTop: 24 }}
          initialValues={{ max_members: 20 }}
        >
          <Form.Item
            name="name"
            label={<span style={{ color: '#B8C4CE' }}>研究组名称</span>}
            rules={[{ required: true, message: '请输入研究组名称' }]}
          >
            <Input 
              placeholder="例如：机器学习研究组"
              size="large"
              maxLength={100}
            />
          </Form.Item>

          <Form.Item
            name="description"
            label={<span style={{ color: '#B8C4CE' }}>研究组描述</span>}
          >
            <TextArea 
              placeholder="描述研究组的研究方向、目标等..."
              rows={4}
              maxLength={500}
              showCount
            />
          </Form.Item>

          <Form.Item
            name="max_members"
            label={<span style={{ color: '#B8C4CE' }}>最大成员数</span>}
          >
            <InputNumber 
              min={1} 
              max={100} 
              style={{ width: '100%' }}
              size="large"
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, marginTop: 24 }}>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => {
                setCreateModalVisible(false);
                createForm.resetFields();
              }}>
                取消
              </Button>
              <Button 
                type="primary" 
                htmlType="submit" 
                loading={submitting}
                style={{ backgroundColor: '#13c2c2' }}
              >
                创建研究组
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑研究组弹窗 */}
      <Modal
        title={
          <Space>
            <EditOutlined style={{ color: '#4A90D9' }} />
            <span style={{ color: '#E8E8E8' }}>编辑研究组</span>
          </Space>
        }
        open={editModalVisible}
        onCancel={() => setEditModalVisible(false)}
        footer={null}
        width={480}
      >
        <Form
          form={editForm}
          layout="vertical"
          onFinish={handleEdit}
          style={{ marginTop: 24 }}
        >
          <Form.Item
            name="name"
            label={<span style={{ color: '#B8C4CE' }}>研究组名称</span>}
            rules={[{ required: true, message: '请输入研究组名称' }]}
          >
            <Input 
              placeholder="例如：机器学习研究组"
              size="large"
              maxLength={100}
            />
          </Form.Item>

          <Form.Item
            name="description"
            label={<span style={{ color: '#B8C4CE' }}>研究组描述</span>}
          >
            <TextArea 
              placeholder="描述研究组的研究方向、目标等..."
              rows={4}
              maxLength={500}
              showCount
            />
          </Form.Item>

          <Form.Item
            name="max_members"
            label={<span style={{ color: '#B8C4CE' }}>最大成员数</span>}
          >
            <InputNumber 
              min={1} 
              max={100} 
              style={{ width: '100%' }}
              size="large"
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, marginTop: 24 }}>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => setEditModalVisible(false)}>
                取消
              </Button>
              <Button 
                type="primary" 
                htmlType="submit" 
                loading={submitting}
                style={{ backgroundColor: '#4A90D9' }}
              >
                保存更改
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* 管理成员弹窗 */}
      <Modal
        title={
          <Space>
            <UsergroupAddOutlined style={{ color: '#52c41a' }} />
            <span style={{ color: '#E8E8E8' }}>
              管理成员 - {selectedGroup?.name}
            </span>
          </Space>
        }
        open={membersModalVisible}
        onCancel={() => setMembersModalVisible(false)}
        footer={null}
        width={600}
      >
        <div style={{ marginTop: 16 }}>
          <Text style={{ color: '#8899A6', marginBottom: 16, display: 'block' }}>
            从您的学生列表中添加或移除组成员
          </Text>

          <Spin spinning={studentsLoading}>
            {students.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={<span style={{ color: '#8899A6' }}>您还没有学生</span>}
              />
            ) : (
              <List
                dataSource={students}
                renderItem={(student) => {
                  // 这里简化处理，实际应该检查学生是否已在组内
                  const isInGroup = false; // TODO: 从group成员列表检查

                  return (
                    <List.Item
                      style={{
                        padding: '12px 16px',
                        borderRadius: 8,
                        marginBottom: 8,
                        backgroundColor: '#0D1117',
                        border: '1px solid #30363D',
                      }}
                      actions={[
                        isInGroup ? (
                          <Button
                            type="text"
                            danger
                            icon={<CloseOutlined />}
                            onClick={() => handleRemoveMember(student.id)}
                          >
                            移除
                          </Button>
                        ) : (
                          <Button
                            type="link"
                            icon={<CheckOutlined />}
                            onClick={() => handleAddMember(student.id)}
                          >
                            添加
                          </Button>
                        ),
                      ]}
                    >
                      <List.Item.Meta
                        avatar={
                          <Avatar 
                            src={student.avatar} 
                            icon={<UserOutlined />}
                            style={{ backgroundColor: '#4A90D9' }}
                          />
                        }
                        title={
                          <span style={{ color: '#E8E8E8' }}>
                            {student.full_name || student.username}
                            {isInGroup && (
                              <Tag color="green" style={{ marginLeft: 8 }}>已加入</Tag>
                            )}
                          </span>
                        }
                        description={
                          <span style={{ color: '#8899A6' }}>
                            {student.research_direction || student.email}
                          </span>
                        }
                      />
                    </List.Item>
                  );
                }}
              />
            )}
          </Spin>
        </div>
      </Modal>

      {/* 全局样式 */}
      <style>{`
        .ant-modal-content {
          background: #161B22 !important;
        }
        .ant-modal-header {
          background: #161B22 !important;
          border-bottom: 1px solid #30363D !important;
        }
        .ant-form-item-label > label {
          color: #B8C4CE !important;
        }
        .ant-input, .ant-input-affix-wrapper, .ant-input-number {
          background: #0D1117 !important;
          border-color: #30363D !important;
          color: #E8E8E8 !important;
        }
        .ant-input::placeholder {
          color: #8899A6 !important;
        }
        .ant-input-number-input {
          background: transparent !important;
          color: #E8E8E8 !important;
        }
        .ant-dropdown-menu {
          background: #161B22 !important;
          border: 1px solid #30363D !important;
        }
        .ant-dropdown-menu-item {
          color: #E8E8E8 !important;
        }
        .ant-dropdown-menu-item:hover {
          background: #1C2128 !important;
        }
      `}</style>
    </div>
  );
};

export default GroupsPage;
