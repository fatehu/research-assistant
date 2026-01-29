/**
 * 学生 - 公告通知页面
 */
import React, { useEffect, useState } from 'react';
import {
  Card, List, Tag, Typography, Empty, Spin, Badge, Modal, Button, Space, message
} from 'antd';
import {
  NotificationOutlined, PushpinOutlined, CheckCircleOutlined,
  ClockCircleOutlined, UserOutlined, TeamOutlined, ReloadOutlined
} from '@ant-design/icons';
import api from '../../services/api';

const { Title, Text, Paragraph } = Typography;

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

const StudentAnnouncementsPage: React.FC = () => {
  const [announcements, setAnnouncements] = useState<Announcement[]>([]);
  const [loading, setLoading] = useState(false);
  const [unreadCount, setUnreadCount] = useState(0);
  const [selectedAnnouncement, setSelectedAnnouncement] = useState<Announcement | null>(null);
  const [detailVisible, setDetailVisible] = useState(false);

  const fetchAnnouncements = async () => {
    setLoading(true);
    try {
      const response = await api.get('/api/student/announcements');
      setAnnouncements(response.data);
    } catch (error) {
      message.error('获取公告失败');
    } finally {
      setLoading(false);
    }
  };

  const fetchUnreadCount = async () => {
    try {
      const response = await api.get('/api/student/announcements/unread-count');
      setUnreadCount(response.data.count);
    } catch (error) {
      console.error('获取未读数量失败:', error);
    }
  };

  useEffect(() => {
    fetchAnnouncements();
    fetchUnreadCount();
  }, []);

  const handleReadAnnouncement = async (announcement: Announcement) => {
    setSelectedAnnouncement(announcement);
    setDetailVisible(true);
    
    if (!announcement.is_read) {
      try {
        await api.post(`/api/student/announcements/${announcement.id}/read`);
        // 更新本地状态
        setAnnouncements(prev => 
          prev.map(a => a.id === announcement.id ? { ...a, is_read: true } : a)
        );
        setUnreadCount(prev => Math.max(0, prev - 1));
      } catch (error) {
        console.error('标记已读失败:', error);
      }
    }
  };

  const pinnedAnnouncements = announcements.filter(a => a.is_pinned);
  const normalAnnouncements = announcements.filter(a => !a.is_pinned);

  return (
    <div style={{ 
      padding: '20px 24px',
      minHeight: '100vh',
      background: 'linear-gradient(180deg, #0D1117 0%, #161B22 100%)',
      overflow: 'auto',
    }}>
      {/* 页面标题 */}
      <div style={{ marginBottom: 20, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <Title level={3} style={{ 
            margin: 0, 
            color: '#E8E8E8',
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            gap: 10,
          }}>
            <NotificationOutlined style={{ color: '#52c41a' }} />
            公告通知
          </Title>
          {unreadCount > 0 && (
            <Badge count={unreadCount} style={{ backgroundColor: '#fa8c16' }}>
              <Tag color="orange">未读</Tag>
            </Badge>
          )}
        </div>
        <Button 
          icon={<ReloadOutlined />}
          onClick={() => { fetchAnnouncements(); fetchUnreadCount(); }}
          style={{ borderColor: '#30363D' }}
        >
          刷新
        </Button>
      </div>

      <Spin spinning={loading}>
        {announcements.length === 0 ? (
          <Card style={{ backgroundColor: '#161B22', borderColor: '#30363D', borderRadius: 12 }}>
            <Empty 
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={<span style={{ color: '#8899A6' }}>暂无公告</span>}
            />
          </Card>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            {/* 置顶公告 */}
            {pinnedAnnouncements.length > 0 && (
              <div>
                <div style={{ 
                  display: 'flex', 
                  alignItems: 'center', 
                  gap: 8, 
                  marginBottom: 12,
                  color: '#fa8c16',
                  fontSize: 13,
                  fontWeight: 500,
                }}>
                  <PushpinOutlined />
                  置顶公告
                </div>
                <List
                  dataSource={pinnedAnnouncements}
                  renderItem={(item) => (
                    <AnnouncementCard 
                      announcement={item} 
                      onClick={() => handleReadAnnouncement(item)}
                    />
                  )}
                />
              </div>
            )}

            {/* 普通公告 */}
            {normalAnnouncements.length > 0 && (
              <div>
                {pinnedAnnouncements.length > 0 && (
                  <div style={{ 
                    display: 'flex', 
                    alignItems: 'center', 
                    gap: 8, 
                    marginBottom: 12,
                    color: '#8899A6',
                    fontSize: 13,
                    fontWeight: 500,
                  }}>
                    <NotificationOutlined />
                    全部公告
                  </div>
                )}
                <List
                  dataSource={normalAnnouncements}
                  renderItem={(item) => (
                    <AnnouncementCard 
                      announcement={item} 
                      onClick={() => handleReadAnnouncement(item)}
                    />
                  )}
                />
              </div>
            )}
          </div>
        )}
      </Spin>

      {/* 公告详情弹窗 */}
      <Modal
        title={null}
        open={detailVisible}
        onCancel={() => setDetailVisible(false)}
        footer={null}
        width={600}
        styles={{
          content: { backgroundColor: '#161B22', border: '1px solid #30363D', padding: 0 },
          body: { padding: 0 },
        }}
      >
        {selectedAnnouncement && (
          <div>
            {/* 头部 */}
            <div style={{ 
              padding: '20px 24px', 
              borderBottom: '1px solid #30363D',
              background: 'linear-gradient(135deg, rgba(82, 196, 26, 0.1) 0%, rgba(74, 144, 217, 0.05) 100%)',
            }}>
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 12 }}>
                {selectedAnnouncement.is_pinned && (
                  <PushpinOutlined style={{ color: '#fa8c16', marginTop: 4 }} />
                )}
                <Title level={4} style={{ color: '#E8E8E8', margin: 0, flex: 1 }}>
                  {selectedAnnouncement.title}
                </Title>
              </div>
              <Space size="middle">
                <span style={{ color: '#8899A6', fontSize: 13 }}>
                  <UserOutlined style={{ marginRight: 6 }} />
                  {selectedAnnouncement.mentor_name}
                </span>
                <Tag color={selectedAnnouncement.group_id ? 'blue' : 'green'} style={{ margin: 0 }}>
                  {selectedAnnouncement.group_name || '所有学生'}
                </Tag>
                <span style={{ color: '#6B8E9F', fontSize: 12 }}>
                  <ClockCircleOutlined style={{ marginRight: 4 }} />
                  {new Date(selectedAnnouncement.created_at).toLocaleString('zh-CN')}
                </span>
              </Space>
            </div>
            
            {/* 内容 */}
            <div style={{ padding: '24px' }}>
              <Paragraph style={{ 
                color: '#CBD5E1', 
                margin: 0, 
                whiteSpace: 'pre-wrap',
                fontSize: 14,
                lineHeight: 1.8,
              }}>
                {selectedAnnouncement.content}
              </Paragraph>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
};

// 公告卡片组件
const AnnouncementCard: React.FC<{
  announcement: Announcement;
  onClick: () => void;
}> = ({ announcement, onClick }) => (
  <Card
    hoverable
    onClick={onClick}
    style={{
      backgroundColor: '#161B22',
      borderColor: announcement.is_read ? '#30363D' : '#52c41a50',
      borderRadius: 12,
      marginBottom: 12,
      borderWidth: announcement.is_read ? 1 : 2,
    }}
    bodyStyle={{ padding: '16px 20px' }}
  >
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
      <div style={{ flex: 1 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          {announcement.is_pinned && (
            <PushpinOutlined style={{ color: '#fa8c16' }} />
          )}
          {!announcement.is_read && (
            <Badge status="processing" />
          )}
          <Text style={{ 
            color: '#E8E8E8', 
            fontSize: 15, 
            fontWeight: announcement.is_read ? 400 : 600,
          }}>
            {announcement.title}
          </Text>
        </div>
        <Paragraph 
          ellipsis={{ rows: 2 }}
          style={{ color: '#8899A6', margin: 0, fontSize: 13 }}
        >
          {announcement.content}
        </Paragraph>
        <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ color: '#6B8E9F', fontSize: 12 }}>
            <UserOutlined style={{ marginRight: 4 }} />
            {announcement.mentor_name}
          </span>
          <Tag 
            color={announcement.group_id ? 'blue' : 'green'} 
            style={{ margin: 0, fontSize: 11 }}
          >
            {announcement.group_name || '所有学生'}
          </Tag>
          <span style={{ color: '#6B8E9F', fontSize: 11 }}>
            {new Date(announcement.created_at).toLocaleDateString('zh-CN')}
          </span>
        </div>
      </div>
      <div style={{ marginLeft: 16 }}>
        {announcement.is_read ? (
          <CheckCircleOutlined style={{ color: '#52c41a', fontSize: 18 }} />
        ) : (
          <Badge dot>
            <NotificationOutlined style={{ color: '#fa8c16', fontSize: 18 }} />
          </Badge>
        )}
      </div>
    </div>
  </Card>
);

export default StudentAnnouncementsPage;
