/**
 * 学生 - 导师管理页面
 * 设计风格: 星河学院 - 优雅渐变配合温暖的金橙色调
 */
import React, { useEffect, useState } from 'react';
import { 
  Card, Button, Space, Input, Modal, Form, message,
  Avatar, Tag, Empty, Spin, Row, Col, Typography, List,
  Badge, Tooltip, Divider, Result, Alert
} from 'antd';
import {
  UserOutlined, MailOutlined, SearchOutlined, SendOutlined,
  SolutionOutlined, DisconnectOutlined, TeamOutlined, BookOutlined,
  ExperimentOutlined, FileTextOutlined, ClockCircleOutlined,
  CheckCircleOutlined, StarOutlined, RocketOutlined
} from '@ant-design/icons';
import { useRoleStore, UserInfo, Invitation, InvitationStatus } from '../../stores/roleStore';

const { Title, Text, Paragraph } = Typography;
const { TextArea } = Input;

const MentorPage: React.FC = () => {
  const {
    mentor, mentorLoading, invitations, invitationsLoading,
    fetchMentor, fetchInvitations, applyToMentor, leaveMentor,
    searchMentors, acceptInvitation, rejectInvitation
  } = useRoleStore();

  const [searchModalVisible, setSearchModalVisible] = useState(false);
  const [applyModalVisible, setApplyModalVisible] = useState(false);
  const [searchResults, setSearchResults] = useState<UserInfo[]>([]);
  const [selectedMentor, setSelectedMentor] = useState<UserInfo | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [applyForm] = Form.useForm();
  const [applying, setApplying] = useState(false);

  useEffect(() => {
    fetchMentor();
    fetchInvitations();
  }, []);

  // 收到的邀请（从导师发来的）
  const receivedInvitations = invitations.filter(
    inv => inv.type === 'invite' && inv.status === InvitationStatus.PENDING
  );

  // 已发送的申请
  const sentApplications = invitations.filter(
    inv => inv.type === 'apply'
  );

  const handleSearch = async () => {
    if (!searchQuery.trim()) {
      message.warning('请输入搜索关键词');
      return;
    }
    setSearchLoading(true);
    try {
      const results = await searchMentors(searchQuery);
      setSearchResults(results);
    } finally {
      setSearchLoading(false);
    }
  };

  const handleApply = async (values: { message?: string }) => {
    if (!selectedMentor) return;
    setApplying(true);
    try {
      await applyToMentor(selectedMentor.id, values.message);
      message.success('申请已发送，请等待导师审核');
      setApplyModalVisible(false);
      setSearchModalVisible(false);
      applyForm.resetFields();
      fetchInvitations();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '申请发送失败');
    } finally {
      setApplying(false);
    }
  };

  const handleLeaveMentor = () => {
    Modal.confirm({
      title: '确认离开导师',
      content: (
        <div>
          <p>确定要离开导师 <strong>{mentor?.full_name || mentor?.username}</strong> 吗？</p>
          <p style={{ color: '#faad14', fontSize: 13 }}>
            离开后，您需要重新申请才能加入导师。您的研究数据不会受到影响。
          </p>
        </div>
      ),
      okText: '确认离开',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        try {
          await leaveMentor();
          message.success('已离开导师');
        } catch (error: any) {
          message.error(error.response?.data?.detail || '操作失败');
        }
      },
    });
  };

  const handleAcceptInvitation = async (invitationId: number) => {
    try {
      await acceptInvitation(invitationId);
      message.success('已接受邀请');
      fetchMentor();
      fetchInvitations();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '操作失败');
    }
  };

  const handleRejectInvitation = async (invitationId: number) => {
    try {
      await rejectInvitation(invitationId);
      message.success('已拒绝邀请');
      fetchInvitations();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '操作失败');
    }
  };

  // 导师信息卡片
  const MentorCard: React.FC = () => {
    if (mentorLoading) {
      return (
        <Card
          style={{
            backgroundColor: '#161B22',
            borderColor: '#30363D',
            borderRadius: 20,
            textAlign: 'center',
            padding: 40,
          }}
        >
          <Spin size="large" />
        </Card>
      );
    }

    if (!mentor) {
      return (
        <Card
          style={{
            backgroundColor: '#161B22',
            borderColor: '#30363D',
            borderRadius: 20,
            overflow: 'hidden',
          }}
          bodyStyle={{ padding: 0 }}
        >
          <div style={{
            background: 'linear-gradient(135deg, #D4AF3715 0%, #fa8c1610 100%)',
            padding: '48px 40px',
            textAlign: 'center',
          }}>
            <div style={{
              width: 80, height: 80, borderRadius: '50%',
              backgroundColor: '#D4AF3720',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              margin: '0 auto 20px',
              fontSize: 36, color: '#D4AF37',
            }}>
              <RocketOutlined />
            </div>
            <Title level={3} style={{ color: '#E8E8E8', marginBottom: 8 }}>
              开启学术之旅
            </Title>
            <Text style={{ color: '#8899A6', fontSize: 15 }}>
              您还没有加入任何导师，开始搜索并申请加入吧
            </Text>
          </div>

          <div style={{ padding: '32px 40px', textAlign: 'center' }}>
            <Button 
              type="primary" 
              size="large" 
              icon={<SearchOutlined />}
              onClick={() => setSearchModalVisible(true)}
              style={{
                background: 'linear-gradient(135deg, #D4AF37 0%, #fa8c16 100%)',
                border: 'none',
                borderRadius: 12,
                height: 48,
                paddingLeft: 32,
                paddingRight: 32,
                fontWeight: 600,
              }}
            >
              搜索导师
            </Button>
          </div>
        </Card>
      );
    }

    return (
      <Card
        style={{
          backgroundColor: '#161B22',
          borderColor: '#30363D',
          borderRadius: 20,
          overflow: 'hidden',
        }}
        bodyStyle={{ padding: 0 }}
      >
        {/* 导师头部 */}
        <div style={{
          background: 'linear-gradient(135deg, #4A90D920 0%, #13c2c215 100%)',
          padding: '32px 40px',
        }}>
          <div style={{ display: 'flex', gap: 24, alignItems: 'center' }}>
            <Avatar 
              size={96} 
              src={mentor.avatar}
              icon={<UserOutlined />}
              style={{ 
                backgroundColor: '#4A90D9',
                border: '4px solid #4A90D930',
              }}
            />
            <div style={{ flex: 1 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
                <Title level={3} style={{ margin: 0, color: '#E8E8E8' }}>
                  {mentor.full_name || mentor.username}
                </Title>
                <Tag 
                  icon={<StarOutlined />}
                  style={{ 
                    backgroundColor: '#D4AF3720', 
                    borderColor: '#D4AF37',
                    color: '#D4AF37',
                  }}
                >
                  我的导师
                </Tag>
              </div>
              <Text type="secondary" style={{ fontSize: 14 }}>{mentor.email}</Text>
              <div style={{ marginTop: 12 }}>
                {mentor.department && (
                  <Tag color="blue">{mentor.department}</Tag>
                )}
                {mentor.research_direction && (
                  <Tag color="cyan">{mentor.research_direction}</Tag>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* 导师信息 */}
        <div style={{ padding: '24px 40px' }}>
          {mentor.bio && (
            <div style={{ marginBottom: 24 }}>
              <Text type="secondary" style={{ fontSize: 12, marginBottom: 8, display: 'block' }}>
                导师简介
              </Text>
              <Paragraph style={{ color: '#B8C4CE', margin: 0 }}>
                {mentor.bio}
              </Paragraph>
            </div>
          )}

          <Divider style={{ borderColor: '#30363D', margin: '20px 0' }} />

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <Space>
              <ClockCircleOutlined style={{ color: '#8899A6' }} />
              <Text style={{ color: '#8899A6' }}>
                加入时间：{/* 这里应该显示加入导师的时间 */}
                {new Date().toLocaleDateString('zh-CN')}
              </Text>
            </Space>
            <Button 
              danger 
              ghost
              icon={<DisconnectOutlined />}
              onClick={handleLeaveMentor}
            >
              离开导师
            </Button>
          </div>
        </div>
      </Card>
    );
  };

  // 邀请卡片
  const InvitationCard: React.FC<{ invitation: Invitation }> = ({ invitation }) => (
    <Card
      style={{
        backgroundColor: '#161B22',
        borderColor: '#52c41a50',
        borderRadius: 16,
      }}
      bodyStyle={{ padding: '20px 24px' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
        <Avatar 
          size={56} 
          src={invitation.from_user?.avatar}
          icon={<UserOutlined />}
          style={{ backgroundColor: '#52c41a' }}
        />
        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            <Text style={{ fontWeight: 600, color: '#E8E8E8', fontSize: 15 }}>
              {invitation.from_user?.full_name || invitation.from_user?.username}
            </Text>
            <Tag color="green">邀请您</Tag>
          </div>
          <Text type="secondary" style={{ fontSize: 13 }}>
            {invitation.from_user?.department || invitation.from_user?.email}
          </Text>
          {invitation.message && (
            <Paragraph 
              style={{ color: '#B8C4CE', margin: '8px 0 0', fontSize: 13 }}
              ellipsis={{ rows: 2 }}
            >
              "{invitation.message}"
            </Paragraph>
          )}
        </div>
      </div>
      <div style={{ marginTop: 16, display: 'flex', gap: 12, justifyContent: 'flex-end' }}>
        <Button 
          onClick={() => handleRejectInvitation(invitation.id)}
        >
          婉拒
        </Button>
        <Button 
          type="primary"
          icon={<CheckCircleOutlined />}
          onClick={() => handleAcceptInvitation(invitation.id)}
          style={{ backgroundColor: '#52c41a' }}
        >
          接受邀请
        </Button>
      </div>
    </Card>
  );

  return (
    <div style={{ 
      padding: '32px 40px',
      minHeight: '100vh',
      background: 'linear-gradient(180deg, #0D1117 0%, #161B22 100%)',
    }}>
      {/* 页面标题 */}
      <div style={{ marginBottom: 32 }}>
        <Title level={2} style={{ 
          margin: 0, 
          color: '#E8E8E8',
          fontWeight: 600,
          display: 'flex',
          alignItems: 'center',
          gap: 12,
        }}>
          <SolutionOutlined style={{ color: '#D4A84B' }} />
          我的导师
        </Title>
        <Text style={{ color: '#8899A6', marginTop: 8, display: 'block' }}>
          查看导师信息，管理师生关系
        </Text>
      </div>

      <Row gutter={[24, 24]}>
        {/* 导师信息区 */}
        <Col xs={24} lg={16}>
          <MentorCard />
        </Col>

        {/* 右侧信息区 */}
        <Col xs={24} lg={8}>
          {/* 收到的邀请 */}
          {receivedInvitations.length > 0 && (
            <div style={{ marginBottom: 24 }}>
              <Title level={5} style={{ color: '#E8E8E8', marginBottom: 16 }}>
                <Badge count={receivedInvitations.length} offset={[8, 0]}>
                  <span style={{ color: '#E8E8E8' }}>收到的邀请</span>
                </Badge>
              </Title>
              <Space direction="vertical" style={{ width: '100%' }} size={12}>
                {receivedInvitations.map(inv => (
                  <InvitationCard key={inv.id} invitation={inv} />
                ))}
              </Space>
            </div>
          )}

          {/* 我的申请记录 */}
          <Card
            title={
              <span style={{ color: '#E8E8E8' }}>
                <SendOutlined style={{ marginRight: 8, color: '#4A90D9' }} />
                我的申请
              </span>
            }
            style={{
              backgroundColor: '#161B22',
              borderColor: '#30363D',
              borderRadius: 16,
            }}
            headStyle={{ 
              borderBottom: '1px solid #30363D',
              backgroundColor: 'transparent',
            }}
          >
            {sentApplications.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={<span style={{ color: '#8899A6' }}>暂无申请记录</span>}
              />
            ) : (
              <List
                dataSource={sentApplications}
                renderItem={(app) => (
                  <List.Item style={{ borderBottom: '1px solid #30363D', padding: '12px 0' }}>
                    <List.Item.Meta
                      avatar={
                        <Avatar 
                          src={app.to_user?.avatar}
                          icon={<UserOutlined />}
                          style={{ backgroundColor: '#4A90D9' }}
                        />
                      }
                      title={
                        <span style={{ color: '#E8E8E8' }}>
                          {app.to_user?.full_name || app.to_user?.username}
                        </span>
                      }
                      description={
                        <span style={{ color: '#8899A6', fontSize: 12 }}>
                          {new Date(app.created_at).toLocaleDateString('zh-CN')}
                        </span>
                      }
                    />
                    <Tag color={
                      app.status === InvitationStatus.PENDING ? 'processing' :
                      app.status === InvitationStatus.ACCEPTED ? 'success' :
                      app.status === InvitationStatus.REJECTED ? 'error' : 'default'
                    }>
                      {app.status === InvitationStatus.PENDING ? '待处理' :
                       app.status === InvitationStatus.ACCEPTED ? '已通过' :
                       app.status === InvitationStatus.REJECTED ? '已拒绝' : '已取消'}
                    </Tag>
                  </List.Item>
                )}
              />
            )}
          </Card>
        </Col>
      </Row>

      {/* 搜索导师弹窗 */}
      <Modal
        title={
          <Space>
            <SearchOutlined style={{ color: '#D4AF37' }} />
            <span style={{ color: '#E8E8E8' }}>搜索导师</span>
          </Space>
        }
        open={searchModalVisible}
        onCancel={() => {
          setSearchModalVisible(false);
          setSearchResults([]);
          setSearchQuery('');
        }}
        footer={null}
        width={600}
      >
        <div style={{ marginTop: 16 }}>
          <Space.Compact style={{ width: '100%', marginBottom: 24 }}>
            <Input
              placeholder="输入导师姓名、邮箱或研究方向..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onPressEnter={handleSearch}
              size="large"
            />
            <Button 
              type="primary" 
              icon={<SearchOutlined />}
              onClick={handleSearch}
              loading={searchLoading}
              size="large"
              style={{ backgroundColor: '#D4AF37' }}
            >
              搜索
            </Button>
          </Space.Compact>

          <Spin spinning={searchLoading}>
            {searchResults.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <span style={{ color: '#8899A6' }}>
                    {searchQuery ? '未找到匹配的导师' : '请输入关键词搜索导师'}
                  </span>
                }
              />
            ) : (
              <List
                dataSource={searchResults}
                renderItem={(mentor) => (
                  <List.Item
                    style={{
                      padding: '16px',
                      borderRadius: 12,
                      marginBottom: 12,
                      backgroundColor: '#0D1117',
                      border: '1px solid #30363D',
                    }}
                    actions={[
                      <Button
                        type="primary"
                        size="small"
                        onClick={() => {
                          setSelectedMentor(mentor);
                          setApplyModalVisible(true);
                        }}
                        style={{ backgroundColor: '#D4AF37' }}
                      >
                        申请加入
                      </Button>,
                    ]}
                  >
                    <List.Item.Meta
                      avatar={
                        <Avatar 
                          size={48}
                          src={mentor.avatar}
                          icon={<UserOutlined />}
                          style={{ backgroundColor: '#4A90D9' }}
                        />
                      }
                      title={
                        <span style={{ color: '#E8E8E8', fontWeight: 600 }}>
                          {mentor.full_name || mentor.username}
                        </span>
                      }
                      description={
                        <div>
                          <div style={{ color: '#8899A6', marginBottom: 4 }}>{mentor.email}</div>
                          <Space size={4}>
                            {mentor.department && <Tag color="blue">{mentor.department}</Tag>}
                            {mentor.research_direction && <Tag color="cyan">{mentor.research_direction}</Tag>}
                          </Space>
                        </div>
                      }
                    />
                  </List.Item>
                )}
              />
            )}
          </Spin>
        </div>
      </Modal>

      {/* 申请加入弹窗 */}
      <Modal
        title={
          <Space>
            <SendOutlined style={{ color: '#52c41a' }} />
            <span style={{ color: '#E8E8E8' }}>申请加入导师</span>
          </Space>
        }
        open={applyModalVisible}
        onCancel={() => {
          setApplyModalVisible(false);
          applyForm.resetFields();
        }}
        footer={null}
        width={480}
      >
        {selectedMentor && (
          <div style={{ marginTop: 16 }}>
            {/* 导师信息 */}
            <div style={{
              padding: 16,
              backgroundColor: '#0D1117',
              borderRadius: 12,
              marginBottom: 24,
              display: 'flex',
              alignItems: 'center',
              gap: 16,
            }}>
              <Avatar 
                size={56}
                src={selectedMentor.avatar}
                icon={<UserOutlined />}
                style={{ backgroundColor: '#4A90D9' }}
              />
              <div>
                <Text style={{ fontWeight: 600, color: '#E8E8E8', display: 'block' }}>
                  {selectedMentor.full_name || selectedMentor.username}
                </Text>
                <Text type="secondary" style={{ fontSize: 13 }}>
                  {selectedMentor.department || selectedMentor.email}
                </Text>
              </div>
            </div>

            <Form
              form={applyForm}
              layout="vertical"
              onFinish={handleApply}
            >
              <Form.Item
                name="message"
                label={<span style={{ color: '#B8C4CE' }}>申请留言（可选）</span>}
              >
                <TextArea 
                  placeholder="介绍一下自己，以及您希望跟随导师学习的方向..."
                  rows={4}
                  maxLength={500}
                  showCount
                />
              </Form.Item>

              <Form.Item style={{ marginBottom: 0, marginTop: 24 }}>
                <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
                  <Button onClick={() => {
                    setApplyModalVisible(false);
                    applyForm.resetFields();
                  }}>
                    取消
                  </Button>
                  <Button 
                    type="primary" 
                    htmlType="submit" 
                    loading={applying}
                    icon={<SendOutlined />}
                    style={{ backgroundColor: '#52c41a' }}
                  >
                    提交申请
                  </Button>
                </Space>
              </Form.Item>
            </Form>
          </div>
        )}
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
        .ant-input, .ant-input-affix-wrapper {
          background: #0D1117 !important;
          border-color: #30363D !important;
          color: #E8E8E8 !important;
        }
        .ant-input::placeholder {
          color: #8899A6 !important;
        }
        .ant-card-head {
          background: transparent !important;
        }
        .ant-card-head-title {
          color: #E8E8E8 !important;
        }
        .ant-list-item-meta-title {
          color: #E8E8E8 !important;
        }
        .ant-list-item-meta-description {
          color: #8899A6 !important;
        }
      `}</style>
    </div>
  );
};

export default MentorPage;
