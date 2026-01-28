/**
 * 导师 - 学生管理页面
 * 设计风格: 学术星辰 - 深邃夜空配合温暖的橙色与冷静的蓝绿色
 */
import React, { useEffect, useState } from 'react';
import { 
  Card, Table, Button, Space, Input, Modal, Form, message,
  Avatar, Tag, Progress, Tooltip, Empty, Spin, Row, Col,
  Typography, Statistic, Badge, Dropdown, Tabs
} from 'antd';
import {
  UserAddOutlined, UserOutlined, MailOutlined, TeamOutlined,
  BookOutlined, FileTextOutlined, ExperimentOutlined, MessageOutlined,
  DeleteOutlined, EyeOutlined, SendOutlined, ClockCircleOutlined,
  CheckCircleOutlined, MoreOutlined, ReloadOutlined, StarOutlined
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { useRoleStore, StudentDetail, Invitation, InvitationStatus } from '../../stores/roleStore';

const { Title, Text, Paragraph } = Typography;
const { TextArea } = Input;

const StudentsPage: React.FC = () => {
  const {
    students, studentsLoading, invitations, invitationsLoading,
    fetchStudents, inviteStudent, removeStudent, fetchInvitations, cancelInvitation
  } = useRoleStore();

  const [inviteModalVisible, setInviteModalVisible] = useState(false);
  const [detailModalVisible, setDetailModalVisible] = useState(false);
  const [selectedStudent, setSelectedStudent] = useState<StudentDetail | null>(null);
  const [inviteForm] = Form.useForm();
  const [inviting, setInviting] = useState(false);

  useEffect(() => {
    fetchStudents();
    fetchInvitations();
  }, []);

  // 过滤出待处理的邀请
  const pendingInvitations = invitations.filter(
    inv => inv.type === 'invite' && inv.status === InvitationStatus.PENDING
  );

  // 过滤出收到的申请
  const receivedApplications = invitations.filter(
    inv => inv.type === 'apply' && inv.status === InvitationStatus.PENDING
  );

  const handleInvite = async (values: { email: string; message?: string }) => {
    setInviting(true);
    try {
      await inviteStudent(values.email, values.message);
      message.success('邀请已发送');
      setInviteModalVisible(false);
      inviteForm.resetFields();
      fetchInvitations();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '邀请发送失败');
    } finally {
      setInviting(false);
    }
  };

  const handleRemoveStudent = async (student: StudentDetail) => {
    Modal.confirm({
      title: '确认移除学生',
      content: (
        <div>
          <p>确定要将 <strong>{student.username}</strong> 从您的学生列表中移除吗？</p>
          <p style={{ color: '#ff7875', fontSize: 13 }}>移除后，该学生将不再与您关联，但其数据不会被删除。</p>
        </div>
      ),
      okText: '确认移除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        try {
          await removeStudent(student.id);
          message.success('学生已移除');
        } catch (error: any) {
          message.error(error.response?.data?.detail || '操作失败');
        }
      },
    });
  };

  const handleCancelInvitation = async (invitationId: number) => {
    try {
      await cancelInvitation(invitationId);
      message.success('邀请已取消');
    } catch (error: any) {
      message.error(error.response?.data?.detail || '操作失败');
    }
  };

  // 计算学生活跃度得分
  const getActivityScore = (student: StudentDetail) => {
    const total = student.conversation_count + student.knowledge_base_count * 5 + 
                  student.paper_count * 3 + student.notebook_count * 4;
    return Math.min(100, total);
  };

  // 获取活跃度颜色
  const getActivityColor = (score: number) => {
    if (score >= 70) return '#52c41a';
    if (score >= 40) return '#faad14';
    return '#8899A6';
  };

  const columns: ColumnsType<StudentDetail> = [
    {
      title: '学生',
      key: 'student',
      render: (_, record) => (
        <Space>
          <Avatar 
            size={44} 
            src={record.avatar}
            icon={<UserOutlined />}
            style={{ 
              backgroundColor: '#4A90D9',
              border: '2px solid #4A90D920'
            }}
          />
          <div>
            <div style={{ fontWeight: 600, color: '#E8E8E8', display: 'flex', alignItems: 'center', gap: 8 }}>
              {record.username}
              {getActivityScore(record) >= 70 && (
                <Tooltip title="活跃学生">
                  <StarOutlined style={{ color: '#D4AF37', fontSize: 12 }} />
                </Tooltip>
              )}
            </div>
            <Text type="secondary" style={{ fontSize: 12 }}>{record.email}</Text>
          </div>
        </Space>
      ),
    },
    {
      title: '研究方向',
      key: 'research',
      render: (_, record) => (
        <div>
          <div style={{ color: '#B8C4CE' }}>{record.department || '-'}</div>
          {record.research_direction && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {record.research_direction}
            </Text>
          )}
        </div>
      ),
    },
    {
      title: '活跃度',
      key: 'activity',
      width: 160,
      render: (_, record) => {
        const score = getActivityScore(record);
        return (
          <Tooltip title={`活跃度得分: ${score}`}>
            <Progress 
              percent={score} 
              size="small"
              strokeColor={getActivityColor(score)}
              trailColor="#30363D"
              format={() => (
                <span style={{ color: getActivityColor(score), fontSize: 12 }}>{score}%</span>
              )}
            />
          </Tooltip>
        );
      },
    },
    {
      title: '研究数据',
      key: 'stats',
      render: (_, record) => (
        <Space size={16}>
          <Tooltip title="对话数">
            <Badge count={record.conversation_count} showZero color="#4A90D9" overflowCount={999}>
              <div style={{ 
                width: 32, height: 32, borderRadius: 8, 
                backgroundColor: '#4A90D915', display: 'flex', 
                alignItems: 'center', justifyContent: 'center' 
              }}>
                <MessageOutlined style={{ color: '#4A90D9' }} />
              </div>
            </Badge>
          </Tooltip>
          <Tooltip title="知识库">
            <Badge count={record.knowledge_base_count} showZero color="#13c2c2" overflowCount={99}>
              <div style={{ 
                width: 32, height: 32, borderRadius: 8, 
                backgroundColor: '#13c2c215', display: 'flex', 
                alignItems: 'center', justifyContent: 'center' 
              }}>
                <BookOutlined style={{ color: '#13c2c2' }} />
              </div>
            </Badge>
          </Tooltip>
          <Tooltip title="论文数">
            <Badge count={record.paper_count} showZero color="#eb2f96" overflowCount={99}>
              <div style={{ 
                width: 32, height: 32, borderRadius: 8, 
                backgroundColor: '#eb2f9615', display: 'flex', 
                alignItems: 'center', justifyContent: 'center' 
              }}>
                <FileTextOutlined style={{ color: '#eb2f96' }} />
              </div>
            </Badge>
          </Tooltip>
          <Tooltip title="笔记本">
            <Badge count={record.notebook_count} showZero color="#fa8c16" overflowCount={99}>
              <div style={{ 
                width: 32, height: 32, borderRadius: 8, 
                backgroundColor: '#fa8c1615', display: 'flex', 
                alignItems: 'center', justifyContent: 'center' 
              }}>
                <ExperimentOutlined style={{ color: '#fa8c16' }} />
              </div>
            </Badge>
          </Tooltip>
        </Space>
      ),
    },
    {
      title: '加入时间',
      dataIndex: 'joined_at',
      key: 'joined_at',
      render: (date: string) => (
        <Space>
          <ClockCircleOutlined style={{ color: '#8899A6' }} />
          <span style={{ color: '#8899A6' }}>
            {date ? new Date(date).toLocaleDateString('zh-CN') : '-'}
          </span>
        </Space>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 100,
      render: (_, record) => (
        <Dropdown
          menu={{
            items: [
              {
                key: 'view',
                icon: <EyeOutlined />,
                label: '查看详情',
                onClick: () => {
                  setSelectedStudent(record);
                  setDetailModalVisible(true);
                },
              },
              { type: 'divider' },
              {
                key: 'remove',
                icon: <DeleteOutlined />,
                label: '移除学生',
                danger: true,
                onClick: () => handleRemoveStudent(record),
              },
            ],
          }}
        >
          <Button type="text" icon={<MoreOutlined />} />
        </Dropdown>
      ),
    },
  ];

  // 邀请列表列
  const invitationColumns: ColumnsType<Invitation> = [
    {
      title: '被邀请人',
      key: 'to_user',
      render: (_, record) => (
        <Space>
          <Avatar size={36} icon={<UserOutlined />} style={{ backgroundColor: '#6B8E9F' }} />
          <div>
            <div style={{ color: '#E8E8E8' }}>{record.to_user?.username || '未注册用户'}</div>
            <Text type="secondary" style={{ fontSize: 12 }}>{record.to_user?.email}</Text>
          </div>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (status: InvitationStatus) => {
        const config = {
          [InvitationStatus.PENDING]: { color: 'processing', text: '待处理' },
          [InvitationStatus.ACCEPTED]: { color: 'success', text: '已接受' },
          [InvitationStatus.REJECTED]: { color: 'error', text: '已拒绝' },
          [InvitationStatus.CANCELLED]: { color: 'default', text: '已取消' },
        };
        return <Tag color={config[status].color}>{config[status].text}</Tag>;
      },
    },
    {
      title: '发送时间',
      dataIndex: 'created_at',
      key: 'created_at',
      render: (date: string) => (
        <span style={{ color: '#8899A6' }}>
          {new Date(date).toLocaleString('zh-CN')}
        </span>
      ),
    },
    {
      title: '操作',
      key: 'action',
      render: (_, record) => (
        record.status === InvitationStatus.PENDING ? (
          <Button 
            type="link" 
            danger 
            size="small"
            onClick={() => handleCancelInvitation(record.id)}
          >
            取消邀请
          </Button>
        ) : null
      ),
    },
  ];

  // 统计卡片
  const StatCard: React.FC<{
    title: string;
    value: number;
    icon: React.ReactNode;
    color: string;
  }> = ({ title, value, icon, color }) => (
    <Card
      style={{
        background: `linear-gradient(135deg, ${color}12 0%, ${color}05 100%)`,
        borderColor: `${color}25`,
        borderRadius: 16,
      }}
      bodyStyle={{ padding: '24px' }}
    >
      <Space align="start">
        <div 
          style={{ 
            width: 52, height: 52, borderRadius: 14, 
            backgroundColor: `${color}18`,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 24, color: color,
          }}
        >
          {icon}
        </div>
        <div>
          <Text style={{ color: '#8899A6', fontSize: 13 }}>{title}</Text>
          <div style={{ fontSize: 32, fontWeight: 700, color: color, lineHeight: 1.2, marginTop: 4 }}>
            {value}
          </div>
        </div>
      </Space>
    </Card>
  );

  return (
    <div style={{ 
      padding: '32px 40px',
      minHeight: '100vh',
      background: 'linear-gradient(180deg, #0D1117 0%, #161B22 100%)',
    }}>
      {/* 页面标题 */}
      <div style={{ marginBottom: 32, display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <Title level={2} style={{ 
            margin: 0, 
            color: '#E8E8E8',
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            gap: 12,
          }}>
            <TeamOutlined style={{ color: '#4A90D9' }} />
            学生管理
          </Title>
          <Text style={{ color: '#8899A6', marginTop: 8, display: 'block' }}>
            管理您指导的学生，跟踪研究进度，发送邀请
          </Text>
        </div>
        <Button 
          type="primary" 
          icon={<UserAddOutlined />}
          onClick={() => setInviteModalVisible(true)}
          size="large"
          style={{ 
            backgroundColor: '#4A90D9',
            borderRadius: 10,
            height: 44,
            paddingLeft: 24,
            paddingRight: 24,
          }}
        >
          邀请学生
        </Button>
      </div>

      {/* 统计卡片 */}
      <Row gutter={[20, 20]} style={{ marginBottom: 32 }}>
        <Col xs={24} sm={12} lg={6}>
          <StatCard 
            title="我的学生" 
            value={students.length} 
            icon={<TeamOutlined />}
            color="#4A90D9"
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatCard 
            title="待处理邀请" 
            value={pendingInvitations.length} 
            icon={<SendOutlined />}
            color="#faad14"
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatCard 
            title="待处理申请" 
            value={receivedApplications.length} 
            icon={<MailOutlined />}
            color="#52c41a"
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatCard 
            title="活跃学生" 
            value={students.filter(s => getActivityScore(s) >= 70).length} 
            icon={<StarOutlined />}
            color="#D4AF37"
          />
        </Col>
      </Row>

      {/* 主内容区 */}
      <Card
        style={{
          backgroundColor: '#161B22',
          borderColor: '#30363D',
          borderRadius: 16,
        }}
        bodyStyle={{ padding: 0 }}
      >
        <Tabs
          defaultActiveKey="students"
          items={[
            {
              key: 'students',
              label: (
                <span>
                  <TeamOutlined />
                  学生列表 ({students.length})
                </span>
              ),
              children: (
                <>
                  <div style={{ 
                    padding: '16px 24px', 
                    borderBottom: '1px solid #30363D',
                    display: 'flex',
                    justifyContent: 'flex-end',
                  }}>
                    <Button 
                      icon={<ReloadOutlined />} 
                      onClick={() => fetchStudents()}
                      style={{ borderColor: '#30363D' }}
                    >
                      刷新
                    </Button>
                  </div>
                  <Table
                    columns={columns}
                    dataSource={students}
                    rowKey="id"
                    loading={studentsLoading}
                    pagination={false}
                    locale={{
                      emptyText: (
                        <Empty 
                          image={Empty.PRESENTED_IMAGE_SIMPLE}
                          description={
                            <span style={{ color: '#8899A6' }}>
                              暂无学生，点击右上角邀请学生加入
                            </span>
                          }
                        />
                      ),
                    }}
                  />
                </>
              ),
            },
            {
              key: 'invitations',
              label: (
                <span>
                  <SendOutlined />
                  已发邀请 ({pendingInvitations.length})
                </span>
              ),
              children: (
                <Table
                  columns={invitationColumns}
                  dataSource={invitations.filter(i => i.type === 'invite')}
                  rowKey="id"
                  loading={invitationsLoading}
                  pagination={false}
                  locale={{
                    emptyText: (
                      <Empty 
                        image={Empty.PRESENTED_IMAGE_SIMPLE}
                        description={<span style={{ color: '#8899A6' }}>暂无邀请记录</span>}
                      />
                    ),
                  }}
                />
              ),
            },
          ]}
          style={{ padding: '0 24px' }}
        />
      </Card>

      {/* 邀请学生弹窗 */}
      <Modal
        title={
          <Space>
            <UserAddOutlined style={{ color: '#4A90D9' }} />
            <span style={{ color: '#E8E8E8' }}>邀请学生</span>
          </Space>
        }
        open={inviteModalVisible}
        onCancel={() => {
          setInviteModalVisible(false);
          inviteForm.resetFields();
        }}
        footer={null}
        width={480}
      >
        <Form
          form={inviteForm}
          layout="vertical"
          onFinish={handleInvite}
          style={{ marginTop: 24 }}
        >
          <Form.Item
            name="email"
            label={<span style={{ color: '#B8C4CE' }}>学生邮箱</span>}
            rules={[
              { required: true, message: '请输入学生邮箱' },
              { type: 'email', message: '请输入有效的邮箱地址' },
            ]}
          >
            <Input 
              prefix={<MailOutlined style={{ color: '#8899A6' }} />}
              placeholder="请输入学生的注册邮箱"
              size="large"
            />
          </Form.Item>

          <Form.Item
            name="message"
            label={<span style={{ color: '#B8C4CE' }}>邀请留言（可选）</span>}
          >
            <TextArea 
              placeholder="您可以附上一段邀请留言..."
              rows={4}
              maxLength={500}
              showCount
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, marginTop: 24 }}>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => {
                setInviteModalVisible(false);
                inviteForm.resetFields();
              }}>
                取消
              </Button>
              <Button 
                type="primary" 
                htmlType="submit" 
                loading={inviting}
                icon={<SendOutlined />}
                style={{ backgroundColor: '#4A90D9' }}
              >
                发送邀请
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* 学生详情弹窗 */}
      <Modal
        title={
          <Space>
            <UserOutlined style={{ color: '#4A90D9' }} />
            <span style={{ color: '#E8E8E8' }}>学生详情</span>
          </Space>
        }
        open={detailModalVisible}
        onCancel={() => setDetailModalVisible(false)}
        footer={null}
        width={560}
      >
        {selectedStudent && (
          <div style={{ padding: '20px 0' }}>
            {/* 基本信息 */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 24 }}>
              <Avatar 
                size={72} 
                src={selectedStudent.avatar}
                icon={<UserOutlined />}
                style={{ backgroundColor: '#4A90D9' }}
              />
              <div>
                <Title level={4} style={{ margin: 0, color: '#E8E8E8' }}>
                  {selectedStudent.full_name || selectedStudent.username}
                </Title>
                <Text type="secondary">{selectedStudent.email}</Text>
                <div style={{ marginTop: 8 }}>
                  {selectedStudent.department && (
                    <Tag color="blue">{selectedStudent.department}</Tag>
                  )}
                  {selectedStudent.research_direction && (
                    <Tag color="cyan">{selectedStudent.research_direction}</Tag>
                  )}
                </div>
              </div>
            </div>

            {/* 研究数据统计 */}
            <Card 
              style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }}
              bodyStyle={{ padding: 20 }}
            >
              <Row gutter={[16, 16]}>
                <Col span={6}>
                  <Statistic 
                    title={<span style={{ color: '#8899A6' }}>对话</span>}
                    value={selectedStudent.conversation_count}
                    prefix={<MessageOutlined style={{ color: '#4A90D9' }} />}
                    valueStyle={{ color: '#E8E8E8' }}
                  />
                </Col>
                <Col span={6}>
                  <Statistic 
                    title={<span style={{ color: '#8899A6' }}>知识库</span>}
                    value={selectedStudent.knowledge_base_count}
                    prefix={<BookOutlined style={{ color: '#13c2c2' }} />}
                    valueStyle={{ color: '#E8E8E8' }}
                  />
                </Col>
                <Col span={6}>
                  <Statistic 
                    title={<span style={{ color: '#8899A6' }}>论文</span>}
                    value={selectedStudent.paper_count}
                    prefix={<FileTextOutlined style={{ color: '#eb2f96' }} />}
                    valueStyle={{ color: '#E8E8E8' }}
                  />
                </Col>
                <Col span={6}>
                  <Statistic 
                    title={<span style={{ color: '#8899A6' }}>笔记本</span>}
                    value={selectedStudent.notebook_count}
                    prefix={<ExperimentOutlined style={{ color: '#fa8c16' }} />}
                    valueStyle={{ color: '#E8E8E8' }}
                  />
                </Col>
              </Row>
            </Card>

            {/* 加入时间 */}
            <div style={{ marginTop: 20, padding: '12px 16px', backgroundColor: '#0D1117', borderRadius: 8 }}>
              <Space>
                <ClockCircleOutlined style={{ color: '#8899A6' }} />
                <Text style={{ color: '#8899A6' }}>
                  加入时间: {selectedStudent.joined_at ? new Date(selectedStudent.joined_at).toLocaleString('zh-CN') : '-'}
                </Text>
              </Space>
            </div>
          </div>
        )}
      </Modal>

      {/* 全局样式 */}
      <style>{`
        .ant-table {
          background: transparent !important;
        }
        .ant-table-thead > tr > th {
          background: #0D1117 !important;
          color: #8899A6 !important;
          border-bottom: 1px solid #30363D !important;
        }
        .ant-table-tbody > tr > td {
          border-bottom: 1px solid #30363D !important;
          background: transparent !important;
          color: #E8E8E8;
        }
        .ant-table-tbody > tr:hover > td {
          background: #1C2128 !important;
        }
        .ant-tabs-nav {
          margin-bottom: 0 !important;
        }
        .ant-tabs-tab {
          color: #8899A6 !important;
        }
        .ant-tabs-tab-active .ant-tabs-tab-btn {
          color: #4A90D9 !important;
        }
        .ant-tabs-ink-bar {
          background: #4A90D9 !important;
        }
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
        .ant-input, .ant-input-affix-wrapper {
          background: #0D1117 !important;
          border-color: #30363D !important;
          color: #E8E8E8 !important;
        }
        .ant-input::placeholder {
          color: #8899A6 !important;
        }
      `}</style>
    </div>
  );
};

export default StudentsPage;
