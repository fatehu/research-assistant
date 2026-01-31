import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Card, Tabs, Table, Button, Tag, Space, Avatar, Typography, Modal,
  Select, Input, Empty, Spin, message, Tooltip, Badge, Popconfirm,
  Segmented, Checkbox, Divider,
} from 'antd';
import {
  ShareAltOutlined, FileTextOutlined, FolderOutlined,
  EditOutlined, DeleteOutlined, PlusOutlined, UserOutlined, TeamOutlined,
  GlobalOutlined, EyeOutlined, ReloadOutlined,
  CheckOutlined, DatabaseOutlined,
} from '@ant-design/icons';
import { shareApi } from '@/services/api';
import { useAuthStore } from '@/stores/authStore';

const { Title, Text } = Typography;
const { Search } = Input;

interface SharedResource {
  id: number;
  resource_type: string;
  resource_id: number;
  resource_name: string;
  resource_detail?: {
    title?: string; authors?: string[]; year?: number; venue?: string;
    abstract?: string; pdf_url?: string; url?: string; citation_count?: number;
    description?: string; paper_count?: number; document_count?: number;
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

interface SelectableResource {
  id: number; title?: string; name?: string; authors?: string[];
  year?: number; venue?: string; description?: string;
  paper_count?: number; document_count?: number; color?: string;
}

interface MyGroup { id: number; name: string; role: string; }

const resourceTypeConfig: Record<string, { label: string; icon: React.ReactNode; color: string }> = {
  paper: { label: '论文', icon: <FileTextOutlined />, color: '#4A9EE8' },
  paper_collection: { label: '文献集', icon: <FolderOutlined />, color: '#52c41a' },
  knowledge_base: { label: '知识库', icon: <DatabaseOutlined />, color: '#722ed1' },
  notebook: { label: '笔记本', icon: <EditOutlined />, color: '#fa8c16' },
};

const sharedWithTypeConfig: Record<string, { label: string; icon: React.ReactNode; color: string }> = {
  user: { label: '个人', icon: <UserOutlined />, color: '#1890ff' },
  group: { label: '研究组', icon: <TeamOutlined />, color: '#52c41a' },
  all_students: { label: '所有学生', icon: <GlobalOutlined />, color: '#722ed1' },
};

const SharedResourcesPage: React.FC = () => {
  const { user } = useAuthStore();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState('received');
  const [loading, setLoading] = useState(false);
  const [sharedWithMe, setSharedWithMe] = useState<SharedResource[]>([]);
  const [myShares, setMyShares] = useState<SharedResource[]>([]);
  const [counts, setCounts] = useState({ paper: 0, paper_collection: 0, knowledge_base: 0, notebook: 0, total: 0 });
  const [filterType, setFilterType] = useState<string | undefined>();
  
  const [shareModalVisible, setShareModalVisible] = useState(false);
  const [shareLoading, setShareLoading] = useState(false);
  const [shareResourceType, setShareResourceType] = useState<'paper' | 'paper_collection' | 'knowledge_base'>('paper');
  const [resources, setResources] = useState<SelectableResource[]>([]);
  const [myGroups, setMyGroups] = useState<MyGroup[]>([]);
  const [resourceSearch, setResourceSearch] = useState('');
  const [selectedResources, setSelectedResources] = useState<number[]>([]);
  const [shareTarget, setShareTarget] = useState<{ type: string; id?: number }>({ type: 'group' });

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

  useEffect(() => { loadData(); }, [filterType]);

  const loadResources = async () => {
    try {
      let data: SelectableResource[] = [];
      if (shareResourceType === 'paper') {
        data = await shareApi.getMyPapers(resourceSearch);
      } else if (shareResourceType === 'paper_collection') {
        data = await shareApi.getMyCollections(resourceSearch);
      } else if (shareResourceType === 'knowledge_base') {
        data = await shareApi.getMyKnowledgeBases(resourceSearch);
      }
      setResources(data);
    } catch (error) {
      console.error('加载资源列表失败:', error);
    }
  };

  useEffect(() => {
    if (shareModalVisible) {
      shareApi.getMyGroups().then(setMyGroups);
      loadResources();
    }
  }, [shareModalVisible]);

  useEffect(() => {
    if (shareModalVisible) {
      setSelectedResources([]);
      loadResources();
    }
  }, [shareResourceType, resourceSearch]);

  const handleShare = async () => {
    if (selectedResources.length === 0) { message.warning('请选择要共享的资源'); return; }
    if (shareTarget.type !== 'all_students' && !shareTarget.id) { message.warning('请选择共享对象'); return; }

    setShareLoading(true);
    try {
      if (selectedResources.length === 1) {
        await shareApi.shareResource({
          resource_type: shareResourceType, resource_id: selectedResources[0],
          shared_with_type: shareTarget.type as any, shared_with_id: shareTarget.id, permission: 'read',
        });
        message.success('共享成功');
      } else {
        const result = await shareApi.batchShare({
          resource_type: shareResourceType, resource_ids: selectedResources,
          shared_with_type: shareTarget.type as any, shared_with_id: shareTarget.id, permission: 'read',
        });
        message.success(result.message);
      }
      setShareModalVisible(false);
      setSelectedResources([]);
      setShareTarget({ type: 'group' });
      loadData();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '共享失败');
    } finally {
      setShareLoading(false);
    }
  };

  const handleRemoveShare = async (shareId: number) => {
    try {
      await shareApi.removeShare(shareId);
      message.success('已取消共享');
      loadData();
    } catch (error) { message.error('取消共享失败'); }
  };

  const handleAccessResource = (resource: SharedResource) => {
    // 跳转到共享资源详情页
    navigate(`/shared/view/${resource.id}`);
  };

  const toggleResourceSelection = (id: number) => {
    setSelectedResources(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  };

  const toggleSelectAll = () => {
    setSelectedResources(selectedResources.length === resources.length ? [] : resources.map(r => r.id));
  };

  const receivedColumns = [
    {
      title: '资源', key: 'resource',
      render: (_: any, record: SharedResource) => {
        const config = resourceTypeConfig[record.resource_type] || resourceTypeConfig.paper;
        return (
          <Space>
            <div style={{ width: 42, height: 42, borderRadius: 12, background: `${config.color}15`, display: 'flex', alignItems: 'center', justifyContent: 'center', color: config.color, fontSize: 18 }}>{config.icon}</div>
            <div>
              <div style={{ fontWeight: 500, color: 'var(--text-primary)', maxWidth: 280 }}>
                <Tooltip title={record.resource_name}>{record.resource_name.length > 35 ? record.resource_name.slice(0, 35) + '...' : record.resource_name}</Tooltip>
              </div>
              <Space size={4}>
                <Tag color={config.color} style={{ fontSize: 10, padding: '0 6px', borderRadius: 10 }}>{config.label}</Tag>
                {record.resource_detail?.year && <Text style={{ fontSize: 11, color: 'var(--text-muted)' }}>{record.resource_detail.year}</Text>}
                {record.resource_detail?.paper_count !== undefined && <Text style={{ fontSize: 11, color: 'var(--text-muted)' }}>{record.resource_detail.paper_count} 篇</Text>}
              </Space>
            </div>
          </Space>
        );
      },
    },
    { title: '来源', key: 'owner', width: 150, render: (_: any, record: SharedResource) => (
      <Space><Avatar size="small" src={record.owner_avatar} icon={<UserOutlined />} /><Text style={{ color: 'var(--text-secondary)', fontSize: 13 }}>{record.owner_name}</Text></Space>
    )},
    { title: '共享方式', key: 'shared_type', width: 130, render: (_: any, record: SharedResource) => {
      const config = sharedWithTypeConfig[record.shared_with_type] || sharedWithTypeConfig.user;
      return <Tag icon={config.icon} color={config.color} style={{ borderRadius: 10 }}>{record.group_name || config.label}</Tag>;
    }},
    { title: '时间', key: 'time', width: 100, render: (_: any, record: SharedResource) => (
      <Text style={{ color: 'var(--text-muted)', fontSize: 12 }}>{new Date(record.shared_at || record.created_at).toLocaleDateString('zh-CN')}</Text>
    )},
    { title: '操作', key: 'action', width: 80, fixed: 'right' as const, render: (_: any, record: SharedResource) => (
      <Space size={4}>
        <Tooltip title="查看详情"><Button type="text" icon={<EyeOutlined />} onClick={() => handleAccessResource(record)} /></Tooltip>
      </Space>
    )},
  ];

  const sentColumns = [
    { title: '资源', key: 'resource', render: (_: any, record: SharedResource) => {
      const config = resourceTypeConfig[record.resource_type] || resourceTypeConfig.paper;
      return (
        <Space>
          <div style={{ width: 42, height: 42, borderRadius: 12, background: `${config.color}15`, display: 'flex', alignItems: 'center', justifyContent: 'center', color: config.color, fontSize: 18 }}>{config.icon}</div>
          <div>
            <div style={{ fontWeight: 500, color: 'var(--text-primary)', maxWidth: 260 }}><Tooltip title={record.resource_name}>{record.resource_name.length > 30 ? record.resource_name.slice(0, 30) + '...' : record.resource_name}</Tooltip></div>
            <Tag color={config.color} style={{ fontSize: 10, padding: '0 6px', borderRadius: 10 }}>{config.label}</Tag>
          </div>
        </Space>
      );
    }},
    { title: '共享给', key: 'shared_with', width: 160, render: (_: any, record: SharedResource) => {
      const config = sharedWithTypeConfig[record.shared_with_type] || sharedWithTypeConfig.user;
      return <Space>{config.icon}<Text style={{ color: 'var(--text-secondary)' }}>{record.shared_with_name || config.label}</Text></Space>;
    }},
    { title: '权限', key: 'permission', width: 80, render: (_: any, record: SharedResource) => (
      <Tag color={record.permission === 'write' ? 'orange' : 'blue'} style={{ borderRadius: 10 }}>{record.permission === 'write' ? '可编辑' : '只读'}</Tag>
    )},
    { title: '时间', key: 'time', width: 100, render: (_: any, record: SharedResource) => (
      <Text style={{ color: 'var(--text-muted)', fontSize: 12 }}>{new Date(record.created_at).toLocaleDateString('zh-CN')}</Text>
    )},
    { title: '操作', key: 'action', width: 70, fixed: 'right' as const, render: (_: any, record: SharedResource) => (
      <Popconfirm title="确定取消共享吗？" onConfirm={() => handleRemoveShare(record.id)} okText="确定" cancelText="取消">
        <Button type="text" danger icon={<DeleteOutlined />} />
      </Popconfirm>
    )},
  ];

  return (
    <div style={{ height: '100vh', overflow: 'auto', padding: '20px 24px', background: 'var(--bg-base)' }}>
      <div style={{ marginBottom: 20, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <Title level={4} style={{ margin: 0, color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{ width: 36, height: 36, borderRadius: 10, background: 'linear-gradient(135deg, var(--primary), var(--accent))', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'white' }}><ShareAltOutlined /></div>
            资源共享
          </Title>
          <Text style={{ color: 'var(--text-muted)', marginTop: 4, display: 'block' }}>与研究组成员共享论文、文献集、知识库等研究资源</Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={loadData}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setShareModalVisible(true)}>共享资源</Button>
        </Space>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16, marginBottom: 20 }}>
        {Object.entries(resourceTypeConfig).map(([type, config]) => (
          <Card key={type} size="small" style={{ background: 'var(--bg-elevated)', border: `1px solid ${filterType === type ? config.color : 'var(--border)'}`, borderRadius: 14, cursor: 'pointer', transition: 'all 0.2s' }} styles={{ body: { padding: '18px 20px' } }} onClick={() => setFilterType(filterType === type ? undefined : type)}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
              <div style={{ width: 48, height: 48, borderRadius: 14, background: `linear-gradient(135deg, ${config.color}20, ${config.color}10)`, border: `1px solid ${config.color}30`, display: 'flex', alignItems: 'center', justifyContent: 'center', color: config.color, fontSize: 22 }}>{config.icon}</div>
              <div>
                <div style={{ fontSize: 28, fontWeight: 700, color: 'var(--text-primary)', lineHeight: 1.1 }}>{counts[type as keyof typeof counts] || 0}</div>
                <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>共享{config.label}</div>
              </div>
            </div>
          </Card>
        ))}
      </div>

      <Card style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 16 }} styles={{ body: { padding: 0 } }}>
        <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Tabs activeKey={activeTab} onChange={setActiveTab} size="small" items={[
            { key: 'received', label: <Space>共享给我<Badge count={counts.total} style={{ backgroundColor: 'var(--primary)' }} /></Space> },
            { key: 'sent', label: <Space>我的共享<Badge count={myShares.length} style={{ backgroundColor: '#52c41a' }} /></Space> },
          ]} />
          {filterType && <Tag closable onClose={() => setFilterType(undefined)} color={resourceTypeConfig[filterType]?.color} style={{ borderRadius: 10, padding: '2px 10px' }}>{resourceTypeConfig[filterType]?.label}</Tag>}
        </div>

        <Spin spinning={loading}>
          {activeTab === 'received' ? (
            sharedWithMe.length > 0 ? <Table dataSource={sharedWithMe} columns={receivedColumns} rowKey="id" pagination={{ pageSize: 10, showSizeChanger: false }} scroll={{ x: 800 }} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无共享给您的资源" style={{ padding: 60 }} />
          ) : (
            myShares.length > 0 ? <Table dataSource={myShares} columns={sentColumns} rowKey="id" pagination={{ pageSize: 10, showSizeChanger: false }} scroll={{ x: 800 }} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="您还没有共享任何资源" style={{ padding: 60 }}><Button type="primary" icon={<PlusOutlined />} onClick={() => setShareModalVisible(true)}>立即共享</Button></Empty>
          )}
        </Spin>
      </Card>

      <Modal title={<Space><ShareAltOutlined style={{ color: 'var(--primary)' }} />共享资源</Space>} open={shareModalVisible} onCancel={() => { setShareModalVisible(false); setSelectedResources([]); setResourceSearch(''); }} onOk={handleShare} confirmLoading={shareLoading} okText={`共享 ${selectedResources.length > 0 ? `(${selectedResources.length})` : ''}`} cancelText="取消" width={680}>
        <div style={{ marginTop: 16 }}>
          <div style={{ marginBottom: 20 }}>
            <Text strong style={{ display: 'block', marginBottom: 10, color: 'var(--text-secondary)' }}>选择资源类型</Text>
            <Segmented value={shareResourceType} onChange={(value) => setShareResourceType(value as any)} options={[
              { value: 'paper', label: <Space><FileTextOutlined />论文</Space> },
              { value: 'paper_collection', label: <Space><FolderOutlined />文献集</Space> },
              { value: 'knowledge_base', label: <Space><DatabaseOutlined />知识库</Space> },
            ]} block />
          </div>

          <div style={{ marginBottom: 20 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
              <Text strong style={{ color: 'var(--text-secondary)' }}>选择要共享的{resourceTypeConfig[shareResourceType]?.label}</Text>
              {resources.length > 0 && <Button type="link" size="small" onClick={toggleSelectAll}>{selectedResources.length === resources.length ? '取消全选' : '全选'}</Button>}
            </div>
            <Search placeholder={`搜索${resourceTypeConfig[shareResourceType]?.label}...`} value={resourceSearch} onChange={(e) => setResourceSearch(e.target.value)} style={{ marginBottom: 12 }} allowClear />
            <div style={{ maxHeight: 240, overflow: 'auto', border: '1px solid var(--border)', borderRadius: 12 }}>
              {resources.length > 0 ? resources.map((item) => {
                const isSelected = selectedResources.includes(item.id);
                const displayTitle = item.title || item.name || '';
                return (
                  <div key={item.id} onClick={() => toggleResourceSelection(item.id)} style={{ padding: '14px 16px', cursor: 'pointer', background: isSelected ? 'rgba(74, 158, 232, 0.1)' : 'transparent', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 12 }}>
                    <Checkbox checked={isSelected} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontWeight: 500, color: 'var(--text-primary)', marginBottom: 4, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{displayTitle.length > 50 ? displayTitle.slice(0, 50) + '...' : displayTitle}</div>
                      <Space size={8}>
                        {item.authors && <Text style={{ fontSize: 12, color: 'var(--text-muted)' }}>{item.authors.slice(0, 2).join(', ')}{item.authors.length > 2 ? ' 等' : ''}</Text>}
                        {item.year && <Tag style={{ fontSize: 10, borderRadius: 10 }}>{item.year}</Tag>}
                        {item.paper_count !== undefined && <Tag style={{ fontSize: 10, borderRadius: 10 }}>{item.paper_count} 篇论文</Tag>}
                        {item.document_count !== undefined && <Tag style={{ fontSize: 10, borderRadius: 10 }}>{item.document_count} 个文档</Tag>}
                      </Space>
                    </div>
                    {isSelected && <CheckOutlined style={{ color: 'var(--primary)' }} />}
                  </div>
                );
              }) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`没有找到${resourceTypeConfig[shareResourceType]?.label}`} style={{ padding: 40 }} />}
            </div>
            {selectedResources.length > 0 && <div style={{ marginTop: 8, color: 'var(--primary)', fontSize: 13 }}>已选择 {selectedResources.length} 个{resourceTypeConfig[shareResourceType]?.label}</div>}
          </div>

          <Divider style={{ margin: '16px 0' }} />

          <div>
            <Text strong style={{ display: 'block', marginBottom: 10, color: 'var(--text-secondary)' }}>共享给</Text>
            <Space direction="vertical" style={{ width: '100%' }} size={12}>
              <Select style={{ width: '100%' }} value={shareTarget.type} onChange={(type) => setShareTarget({ type, id: undefined })} options={[
                { value: 'group', label: <Space><TeamOutlined />研究组</Space> },
                ...(user?.role === 'mentor' ? [{ value: 'all_students', label: <Space><GlobalOutlined />所有学生</Space> }] : []),
              ]} />
              {shareTarget.type === 'group' && (
                <Select style={{ width: '100%' }} placeholder="选择研究组" value={shareTarget.id} onChange={(id) => setShareTarget({ ...shareTarget, id })} options={myGroups.map((g) => ({ value: g.id, label: <Space><TeamOutlined />{g.name}<Tag style={{ fontSize: 10, marginLeft: 8 }}>{g.role === 'mentor' ? '导师' : '成员'}</Tag></Space> }))} />
              )}
            </Space>
          </div>
        </div>
      </Modal>

      <style>{`
        .ant-table { background: transparent !important; }
        .ant-table-thead > tr > th { background: var(--bg-base) !important; color: var(--text-muted) !important; border-bottom: 1px solid var(--border) !important; font-weight: 500 !important; font-size: 11px !important; text-transform: uppercase !important; letter-spacing: 0.06em !important; padding: 12px 16px !important; }
        .ant-table-tbody > tr > td { background: transparent !important; border-bottom: 1px solid var(--border-light) !important; padding: 12px 16px !important; }
        .ant-table-tbody > tr:hover > td { background: var(--bg-hover) !important; }
        .ant-table-tbody .ant-btn-text { color: var(--text-secondary) !important; width: 34px !important; height: 34px !important; border-radius: 10px !important; background: var(--bg-surface) !important; border: 1px solid var(--border) !important; }
        .ant-table-tbody .ant-btn-text:hover { color: var(--primary-light) !important; border-color: var(--primary) !important; box-shadow: 0 0 16px var(--primary-glow) !important; transform: translateY(-1px) scale(1.05) !important; }
        .ant-table-cell-fix-right { background: var(--bg-elevated) !important; }
        .ant-table-tbody > tr:hover .ant-table-cell-fix-right { background: var(--bg-hover) !important; }
        .ant-tabs-tab { color: var(--text-muted) !important; }
        .ant-tabs-tab-active .ant-tabs-tab-btn { color: var(--primary-light) !important; }
        .ant-tabs-ink-bar { background: linear-gradient(90deg, var(--primary), var(--accent)) !important; }
        .ant-modal-content { background: var(--bg-elevated) !important; border: 1px solid var(--border) !important; border-radius: 20px !important; }
        .ant-modal-header { background: transparent !important; border-bottom: 1px solid var(--border) !important; }
        .ant-modal-title { color: var(--text-primary) !important; }
        .ant-select-selector { background: var(--bg-base) !important; border-color: var(--border) !important; }
        .ant-input, .ant-input-affix-wrapper { background: var(--bg-base) !important; border-color: var(--border) !important; }
        .ant-pagination-item { background: var(--bg-surface) !important; border-color: var(--border) !important; }
        .ant-pagination-item-active { background: var(--primary) !important; border-color: var(--primary) !important; }
        .ant-segmented { background: var(--bg-base) !important; padding: 4px !important; }
        .ant-segmented-item { color: var(--text-secondary) !important; }
        .ant-segmented-item-selected { background: var(--bg-surface) !important; color: var(--text-primary) !important; }
        .ant-checkbox-inner { background: var(--bg-base) !important; border-color: var(--border) !important; }
        .ant-checkbox-checked .ant-checkbox-inner { background: var(--primary) !important; border-color: var(--primary) !important; }
      `}</style>
    </div>
  );
};

export default SharedResourcesPage;
