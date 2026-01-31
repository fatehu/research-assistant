import React, { useState, useEffect } from 'react';
import {
  Card,
  Tabs,
  Table,
  Button,
  Tag,
  Space,
  Avatar,
  Typography,
  Modal,
  Select,
  Input,
  Empty,
  Spin,
  message,
  Tooltip,
  Badge,
  Popconfirm,
} from 'antd';
import {
  ShareAltOutlined,
  FileTextOutlined,
  FolderOutlined,
  BookOutlined,
  EditOutlined,
  DeleteOutlined,
  PlusOutlined,
  UserOutlined,
  TeamOutlined,
  GlobalOutlined,
  SearchOutlined,
  LinkOutlined,
  CalendarOutlined,
  EyeOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { shareApi } from '@/services/api';
import { useAuthStore } from '@/stores/authStore';

const { Title, Text, Paragraph } = Typography;
const { TabPane } = Tabs;
const { Search } = Input;

interface SharedResource {
  id: number;
  resource_type: string;
  resource_id: number;
  resource_name: string;
  resource_detail?: {
    title?: string;
    authors?: string[];
    year?: number;
    venue?: string;
    abstract?: string;
    pdf_url?: string;
    url?: string;
    citation_count?: number;
    description?: string;
    paper_count?: number;
  };
  owner_id: number;
  owner_name: string;
  owner_avatar?: string;
  shared_with_type: string;
  shared_with_id?: number;
  shared_with_name?: string;
  permission: string;
  created_at: string;
  shared_at?: string;
  group_name?: string;
}

interface MyPaper {
  id: number;
  title: string;
  authors: string[];
  year: number;
  venue: string;
}

interface MyGroup {
  id: number;
  name: string;
  role: string;
}

// 资源类型配置
const resourceTypeConfig: Record<string, { label: string; icon: React.ReactNode; color: string }> = {
  paper: { label: '论文', icon: <FileTextOutlined />, color: '#4A9EE8' },
  paper_collection: { label: '文献集', icon: <FolderOutlined />, color: '#52c41a' },
  knowledge_base: { label: '知识库', icon: <BookOutlined />, color: '#722ed1' },
  notebook: { label: '笔记本', icon: <EditOutlined />, color: '#fa8c16' },
};

// 共享对象类型配置
const sharedWithTypeConfig: Record<string, { label: string; icon: React.ReactNode; color: string }> = {
  user: { label: '个人', icon: <UserOutlined />, color: '#1890ff' },
  group: { label: '研究组', icon: <TeamOutlined />, color: '#52c41a' },
  all_students: { label: '所有学生', icon: <GlobalOutlined />, color: '#722ed1' },
};

const SharedResourcesPage: React.FC = () => {
  const { user } = useAuthStore();
  const [activeTab, setActiveTab] = useState('received');
  const [loading, setLoading] = useState(false);
  const [sharedWithMe, setSharedWithMe] = useState<SharedResource[]>([]);
  const [myShares, setMyShares] = useState<SharedResource[]>([]);
  const [counts, setCounts] = useState({ paper: 0, paper_collection: 0, knowledge_base: 0, notebook: 0, total: 0 });
  const [filterType, setFilterType] = useState<string | undefined>();
  
  // 共享模态框
  const [shareModalVisible, setShareModalVisible] = useState(false);
  const [shareLoading, setShareLoading] = useState(false);
  const [myPapers, setMyPapers] = useState<MyPaper[]>([]);
  const [myGroups, setMyGroups] = useState<MyGroup[]>([]);
  const [paperSearch, setPaperSearch] = useState('');
  const [selectedPaper, setSelectedPaper] = useState<number | null>(null);
  const [shareTarget, setShareTarget] = useState<{ type: string; id?: number }>({ type: 'group' });
  
  // 详情模态框
  const [detailModalVisible, setDetailModalVisible] = useState(false);
  const [selectedResource, setSelectedResource] = useState<SharedResource | null>(null);

  // 加载数据
  const loadData = async () => {
    setLoading(true);
    try {
      const [withMe, mySharesData, countsData] = await Promise.all([
        shareApi.getSharedWithMe(filterType),
        shareApi.getMyShares(filterType),
        shareApi.getSharedCount(),
      ]);
      setSharedWithMe(withMe);
      setMyShares(mySharesData);
      setCounts(countsData);
    } catch (error) {
      console.error('加载共享资源失败:', error);
      message.error('加载数据失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, [filterType]);

  // 加载共享选项
  const loadShareOptions = async () => {
    try {
      const [papers, groups] = await Promise.all([
        shareApi.getMyPapers(paperSearch),
        shareApi.getMyGroups(),
      ]);
      setMyPapers(papers);
      setMyGroups(groups);
    } catch (error) {
      console.error('加载选项失败:', error);
    }
  };

  useEffect(() => {
    if (shareModalVisible) {
      loadShareOptions();
    }
  }, [shareModalVisible, paperSearch]);

  // 执行共享
  const handleShare = async () => {
    if (!selectedPaper) {
      message.warning('请选择要共享的论文');
      return;
    }
    if (shareTarget.type !== 'all_students' && !shareTarget.id) {
      message.warning('请选择共享对象');
      return;
    }

    setShareLoading(true);
    try {
      await shareApi.shareResource({
        resource_type: 'paper',
        resource_id: selectedPaper,
        shared_with_type: shareTarget.type as 'user' | 'group' | 'all_students',
        shared_with_id: shareTarget.id,
        permission: 'read',
      });
      message.success('共享成功');
      setShareModalVisible(false);
      setSelectedPaper(null);
      setShareTarget({ type: 'group' });
      loadData();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '共享失败');
    } finally {
      setShareLoading(false);
    }
  };

  // 取消共享
  const handleRemoveShare = async (shareId: number) => {
    try {
      await shareApi.removeShare(shareId);
      message.success('已取消共享');
      loadData();
    } catch (error) {
      message.error('取消共享失败');
    }
  };

  // 查看详情
  const handleViewDetail = (resource: SharedResource) => {
    setSelectedResource(resource);
    setDetailModalVisible(true);
  };

  // 共享给我的资源列表
  const receivedColumns = [
    {
      title: '资源',
      key: 'resource',
      render: (_: any, record: SharedResource) => {
        const config = resourceTypeConfig[record.resource_type] || resourceTypeConfig.paper;
        return (
          <Space>
            <div style={{
              width: 40,
              height: 40,
              borderRadius: 10,
              background: `${config.color}15`,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: config.color,
              fontSize: 18,
            }}>
              {config.icon}
            </div>
            <div>
              <div style={{ fontWeight: 500, color: 'var(--text-primary)', maxWidth: 300 }}>
                <Tooltip title={record.resource_name}>
                  {record.resource_name.length > 40 ? record.resource_name.slice(0, 40) + '...' : record.resource_name}
                </Tooltip>
              </div>
              <Space size={4}>
                <Tag color={config.color} style={{ fontSize: 10, padding: '0 6px', borderRadius: 10 }}>
                  {config.label}
                </Tag>
                {record.resource_detail?.year && (
                  <Text style={{ fontSize: 11, color: 'var(--text-muted)' }}>{record.resource_detail.year}</Text>
                )}
              </Space>
            </div>
          </Space>
        );
      },
    },
    {
      title: '来源',
      key: 'owner',
      width: 160,
      render: (_: any, record: SharedResource) => (
        <Space>
          <Avatar size="small" src={record.owner_avatar} icon={<UserOutlined />} />
          <Text style={{ color: 'var(--text-secondary)' }}>{record.owner_name}</Text>
        </Space>
      ),
    },
    {
      title: '共享方式',
      key: 'shared_type',
      width: 120,
      render: (_: any, record: SharedResource) => {
        const config = sharedWithTypeConfig[record.shared_with_type] || sharedWithTypeConfig.user;
        return (
          <Tag icon={config.icon} color={config.color} style={{ borderRadius: 10 }}>
            {record.group_name || config.label}
          </Tag>
        );
      },
    },
    {
      title: '共享时间',
      key: 'time',
      width: 120,
      render: (_: any, record: SharedResource) => (
        <Text style={{ color: 'var(--text-muted)', fontSize: 12 }}>
          {new Date(record.shared_at || record.created_at).toLocaleDateString('zh-CN')}
        </Text>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 80,
      render: (_: any, record: SharedResource) => (
        <Button 
          type="text" 
          icon={<EyeOutlined />} 
          onClick={() => handleViewDetail(record)}
        />
      ),
    },
  ];

  // 我共享的资源列表
  const sentColumns = [
    {
      title: '资源',
      key: 'resource',
      render: (_: any, record: SharedResource) => {
        const config = resourceTypeConfig[record.resource_type] || resourceTypeConfig.paper;
        return (
          <Space>
            <div style={{
              width: 40,
              height: 40,
              borderRadius: 10,
              background: `${config.color}15`,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: config.color,
              fontSize: 18,
            }}>
              {config.icon}
            </div>
            <div>
              <div style={{ fontWeight: 500, color: 'var(--text-primary)', maxWidth: 280 }}>
                <Tooltip title={record.resource_name}>
                  {record.resource_name.length > 35 ? record.resource_name.slice(0, 35) + '...' : record.resource_name}
                </Tooltip>
              </div>
              <Tag color={config.color} style={{ fontSize: 10, padding: '0 6px', borderRadius: 10 }}>
                {config.label}
              </Tag>
            </div>
          </Space>
        );
      },
    },
    {
      title: '共享给',
      key: 'shared_with',
      width: 160,
      render: (_: any, record: SharedResource) => {
        const config = sharedWithTypeConfig[record.shared_with_type] || sharedWithTypeConfig.user;
        return (
          <Space>
            {config.icon}
            <Text style={{ color: 'var(--text-secondary)' }}>
              {record.shared_with_name || config.label}
            </Text>
          </Space>
        );
      },
    },
    {
      title: '权限',
      key: 'permission',
      width: 80,
      render: (_: any, record: SharedResource) => (
        <Tag color={record.permission === 'write' ? 'orange' : 'blue'} style={{ borderRadius: 10 }}>
          {record.permission === 'write' ? '可编辑' : '只读'}
        </Tag>
      ),
    },
    {
      title: '共享时间',
      key: 'time',
      width: 120,
      render: (_: any, record: SharedResource) => (
        <Text style={{ color: 'var(--text-muted)', fontSize: 12 }}>
          {new Date(record.created_at).toLocaleDateString('zh-CN')}
        </Text>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 80,
      render: (_: any, record: SharedResource) => (
        <Popconfirm
          title="确定取消共享吗？"
          onConfirm={() => handleRemoveShare(record.id)}
          okText="确定"
          cancelText="取消"
        >
          <Button type="text" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ];

  return (
    <div style={{
      height: '100vh',
      overflow: 'auto',
      padding: '20px 24px',
      background: 'var(--bg-base)',
    }}>
      {/* 页面标题 */}
      <div style={{ marginBottom: 20, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <Title level={4} style={{ margin: 0, color: 'var(--text-primary)' }}>
            <ShareAltOutlined style={{ marginRight: 10, color: 'var(--primary)' }} />
            资源共享
          </Title>
          <Text style={{ color: 'var(--text-muted)' }}>
            与研究组成员共享论文、知识库等研究资源
          </Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={loadData}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setShareModalVisible(true)}>
            共享论文
          </Button>
        </Space>
      </div>

      {/* 统计卡片 */}
      <div style={{ display: 'flex', gap: 16, marginBottom: 20 }}>
        {Object.entries(resourceTypeConfig).map(([type, config]) => (
          <Card
            key={type}
            size="small"
            style={{
              flex: 1,
              background: 'var(--bg-elevated)',
              border: '1px solid var(--border)',
              borderRadius: 12,
            }}
            styles={{ body: { padding: '16px 20px' } }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div style={{
                width: 44,
                height: 44,
                borderRadius: 12,
                background: `${config.color}15`,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: config.color,
                fontSize: 20,
              }}>
                {config.icon}
              </div>
              <div>
                <div style={{ fontSize: 24, fontWeight: 600, color: 'var(--text-primary)' }}>
                  {counts[type as keyof typeof counts] || 0}
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                  共享{config.label}
                </div>
              </div>
            </div>
          </Card>
        ))}
      </div>

      {/* 主内容区 */}
      <Card
        style={{
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border)',
          borderRadius: 16,
        }}
        styles={{ body: { padding: 0 } }}
      >
        {/* 工具栏 */}
        <div style={{
          padding: '16px 20px',
          borderBottom: '1px solid var(--border)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <Tabs
            activeKey={activeTab}
            onChange={setActiveTab}
            size="small"
            items={[
              {
                key: 'received',
                label: (
                  <Space>
                    共享给我
                    <Badge count={counts.total} style={{ backgroundColor: 'var(--primary)' }} />
                  </Space>
                ),
              },
              {
                key: 'sent',
                label: (
                  <Space>
                    我的共享
                    <Badge count={myShares.length} style={{ backgroundColor: '#52c41a' }} />
                  </Space>
                ),
              },
            ]}
          />
          <Select
            placeholder="筛选类型"
            allowClear
            style={{ width: 140 }}
            value={filterType}
            onChange={setFilterType}
            options={[
              { value: 'paper', label: '论文' },
              { value: 'paper_collection', label: '文献集' },
              { value: 'knowledge_base', label: '知识库' },
              { value: 'notebook', label: '笔记本' },
            ]}
          />
        </div>

        {/* 列表 */}
        <Spin spinning={loading}>
          {activeTab === 'received' ? (
            sharedWithMe.length > 0 ? (
              <Table
                dataSource={sharedWithMe}
                columns={receivedColumns}
                rowKey="id"
                pagination={{ pageSize: 10, showSizeChanger: false }}
                scroll={{ x: 800 }}
              />
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="暂无共享给您的资源"
                style={{ padding: 60 }}
              />
            )
          ) : (
            myShares.length > 0 ? (
              <Table
                dataSource={myShares}
                columns={sentColumns}
                rowKey="id"
                pagination={{ pageSize: 10, showSizeChanger: false }}
                scroll={{ x: 800 }}
              />
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="您还没有共享任何资源"
                style={{ padding: 60 }}
              >
                <Button type="primary" icon={<PlusOutlined />} onClick={() => setShareModalVisible(true)}>
                  立即共享
                </Button>
              </Empty>
            )
          )}
        </Spin>
      </Card>

      {/* 共享模态框 */}
      <Modal
        title={
          <Space>
            <ShareAltOutlined style={{ color: 'var(--primary)' }} />
            共享论文
          </Space>
        }
        open={shareModalVisible}
        onCancel={() => {
          setShareModalVisible(false);
          setSelectedPaper(null);
          setPaperSearch('');
        }}
        onOk={handleShare}
        confirmLoading={shareLoading}
        okText="共享"
        cancelText="取消"
        width={600}
      >
        <div style={{ marginTop: 20 }}>
          {/* 选择论文 */}
          <div style={{ marginBottom: 20 }}>
            <Text strong style={{ display: 'block', marginBottom: 8, color: 'var(--text-secondary)' }}>
              选择要共享的论文
            </Text>
            <Search
              placeholder="搜索论文标题..."
              value={paperSearch}
              onChange={(e) => setPaperSearch(e.target.value)}
              style={{ marginBottom: 12 }}
            />
            <div style={{
              maxHeight: 200,
              overflow: 'auto',
              border: '1px solid var(--border)',
              borderRadius: 10,
            }}>
              {myPapers.length > 0 ? (
                myPapers.map((paper) => (
                  <div
                    key={paper.id}
                    onClick={() => setSelectedPaper(paper.id)}
                    style={{
                      padding: '12px 16px',
                      cursor: 'pointer',
                      background: selectedPaper === paper.id ? 'var(--bg-hover)' : 'transparent',
                      borderBottom: '1px solid var(--border)',
                      transition: 'background 0.2s',
                    }}
                  >
                    <div style={{ fontWeight: 500, color: 'var(--text-primary)', marginBottom: 4 }}>
                      {paper.title.length > 60 ? paper.title.slice(0, 60) + '...' : paper.title}
                    </div>
                    <Space size={8}>
                      <Text style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                        {paper.authors.slice(0, 2).join(', ')}
                        {paper.authors.length > 2 ? ' 等' : ''}
                      </Text>
                      {paper.year && (
                        <Tag style={{ fontSize: 10, borderRadius: 10 }}>{paper.year}</Tag>
                      )}
                    </Space>
                  </div>
                ))
              ) : (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="没有找到论文"
                  style={{ padding: 40 }}
                />
              )}
            </div>
          </div>

          {/* 选择共享对象 */}
          <div>
            <Text strong style={{ display: 'block', marginBottom: 8, color: 'var(--text-secondary)' }}>
              共享给
            </Text>
            <Space direction="vertical" style={{ width: '100%' }}>
              <Select
                style={{ width: '100%' }}
                value={shareTarget.type}
                onChange={(type) => setShareTarget({ type, id: undefined })}
                options={[
                  { value: 'group', label: '研究组' },
                  ...(user?.role === 'mentor' ? [{ value: 'all_students', label: '所有学生' }] : []),
                ]}
              />
              {shareTarget.type === 'group' && (
                <Select
                  style={{ width: '100%' }}
                  placeholder="选择研究组"
                  value={shareTarget.id}
                  onChange={(id) => setShareTarget({ ...shareTarget, id })}
                  options={myGroups.map((g) => ({
                    value: g.id,
                    label: (
                      <Space>
                        <TeamOutlined />
                        {g.name}
                        <Tag style={{ fontSize: 10 }}>{g.role === 'mentor' ? '导师' : '成员'}</Tag>
                      </Space>
                    ),
                  }))}
                />
              )}
            </Space>
          </div>
        </div>
      </Modal>

      {/* 详情模态框 */}
      <Modal
        title={
          <Space>
            {selectedResource && resourceTypeConfig[selectedResource.resource_type]?.icon}
            资源详情
          </Space>
        }
        open={detailModalVisible}
        onCancel={() => setDetailModalVisible(false)}
        footer={[
          <Button key="close" onClick={() => setDetailModalVisible(false)}>关闭</Button>,
          selectedResource?.resource_detail?.url && (
            <Button 
              key="link" 
              type="primary" 
              icon={<LinkOutlined />}
              onClick={() => window.open(selectedResource.resource_detail!.url, '_blank')}
            >
              查看原文
            </Button>
          ),
        ]}
        width={640}
      >
        {selectedResource && (
          <div style={{ padding: '10px 0' }}>
            <Title level={5} style={{ color: 'var(--text-primary)', marginBottom: 16 }}>
              {selectedResource.resource_name}
            </Title>
            
            {selectedResource.resource_detail?.authors && (
              <div style={{ marginBottom: 12 }}>
                <Text strong style={{ color: 'var(--text-muted)', fontSize: 12 }}>作者：</Text>
                <Text style={{ color: 'var(--text-secondary)' }}>
                  {selectedResource.resource_detail.authors.join(', ')}
                </Text>
              </div>
            )}
            
            <Space wrap style={{ marginBottom: 16 }}>
              {selectedResource.resource_detail?.year && (
                <Tag icon={<CalendarOutlined />}>{selectedResource.resource_detail.year}</Tag>
              )}
              {selectedResource.resource_detail?.venue && (
                <Tag color="blue">{selectedResource.resource_detail.venue}</Tag>
              )}
              {selectedResource.resource_detail?.citation_count !== undefined && (
                <Tag color="gold">被引 {selectedResource.resource_detail.citation_count}</Tag>
              )}
            </Space>
            
            {selectedResource.resource_detail?.abstract && (
              <div style={{ marginBottom: 16 }}>
                <Text strong style={{ color: 'var(--text-muted)', fontSize: 12, display: 'block', marginBottom: 8 }}>
                  摘要：
                </Text>
                <Paragraph 
                  style={{ color: 'var(--text-secondary)', fontSize: 13, lineHeight: 1.8 }}
                  ellipsis={{ rows: 6, expandable: true, symbol: '展开' }}
                >
                  {selectedResource.resource_detail.abstract}
                </Paragraph>
              </div>
            )}
            
            <div style={{
              padding: 12,
              background: 'var(--bg-surface)',
              borderRadius: 10,
              marginTop: 16,
            }}>
              <Space>
                <Avatar size="small" src={selectedResource.owner_avatar} icon={<UserOutlined />} />
                <Text style={{ color: 'var(--text-secondary)' }}>
                  由 <strong>{selectedResource.owner_name}</strong> 共享于{' '}
                  {new Date(selectedResource.shared_at || selectedResource.created_at).toLocaleDateString('zh-CN')}
                </Text>
              </Space>
            </div>
          </div>
        )}
      </Modal>

      {/* 2026 Design System Styles */}
      <style>{`
        .ant-table {
          background: transparent !important;
        }
        .ant-table-thead > tr > th {
          background: var(--bg-base) !important;
          color: var(--text-muted) !important;
          border-bottom: 1px solid var(--border) !important;
          font-weight: 500 !important;
          font-size: 11px !important;
          text-transform: uppercase !important;
          letter-spacing: 0.06em !important;
          padding: 12px 16px !important;
        }
        .ant-table-tbody > tr > td {
          background: transparent !important;
          border-bottom: 1px solid var(--border-light) !important;
          padding: 12px 16px !important;
        }
        .ant-table-tbody > tr:hover > td {
          background: var(--bg-hover) !important;
        }
        .ant-table-tbody .ant-btn-text {
          color: var(--text-secondary) !important;
          width: 36px !important;
          height: 36px !important;
          border-radius: 10px !important;
          background: var(--bg-surface) !important;
          border: 1px solid var(--border) !important;
        }
        .ant-table-tbody .ant-btn-text:hover {
          color: var(--primary-light) !important;
          border-color: var(--primary) !important;
          box-shadow: 0 0 20px var(--primary-glow) !important;
          transform: translateY(-2px) scale(1.05) !important;
        }
        .ant-tabs-tab {
          color: var(--text-muted) !important;
        }
        .ant-tabs-tab-active .ant-tabs-tab-btn {
          color: var(--primary-light) !important;
        }
        .ant-tabs-ink-bar {
          background: linear-gradient(90deg, var(--primary), var(--accent)) !important;
        }
        .ant-modal-content {
          background: var(--bg-elevated) !important;
          border: 1px solid var(--border) !important;
          border-radius: 20px !important;
        }
        .ant-modal-header {
          background: transparent !important;
          border-bottom: 1px solid var(--border) !important;
        }
        .ant-modal-title {
          color: var(--text-primary) !important;
        }
        .ant-select-selector {
          background: var(--bg-base) !important;
          border-color: var(--border) !important;
        }
        .ant-input, .ant-input-affix-wrapper {
          background: var(--bg-base) !important;
          border-color: var(--border) !important;
        }
        .ant-pagination-item {
          background: var(--bg-surface) !important;
          border-color: var(--border) !important;
        }
        .ant-pagination-item-active {
          background: var(--primary) !important;
          border-color: var(--primary) !important;
        }
      `}</style>
    </div>
  );
};

export default SharedResourcesPage;
