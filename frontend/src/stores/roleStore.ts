/**
 * 角色系统状态管理
 */
import { create } from 'zustand';
import { devtools } from 'zustand/middleware';
import api from '../services/api';

// 用户角色枚举
export enum UserRole {
  ADMIN = 'admin',
  MENTOR = 'mentor',
  STUDENT = 'student',
}

// 邀请状态枚举
export enum InvitationStatus {
  PENDING = 'pending',
  ACCEPTED = 'accepted',
  REJECTED = 'rejected',
  CANCELLED = 'cancelled',
}

// 共享类型枚举
export enum ShareType {
  KNOWLEDGE_BASE = 'knowledge_base',
  PAPER_COLLECTION = 'paper_collection',
  NOTEBOOK = 'notebook',
}

// 共享权限枚举
export enum SharePermission {
  READ = 'read',
  WRITE = 'write',
  ADMIN = 'admin',
}

// 用户信息接口
export interface UserInfo {
  id: number;
  email: string;
  username: string;
  full_name?: string;
  avatar?: string;
  bio?: string;
  role: UserRole;
  mentor_id?: number;
  department?: string;
  research_direction?: string;
  joined_at?: string;
  is_active: boolean;
  created_at: string;
  last_login?: string;
}

// 学生详情接口（含统计）
export interface StudentDetail extends UserInfo {
  conversation_count: number;
  knowledge_base_count: number;
  paper_count: number;
  notebook_count: number;
}

// 研究组接口
export interface ResearchGroup {
  id: number;
  name: string;
  description?: string;
  mentor_id: number;
  avatar?: string;
  is_active: boolean;
  max_members: number;
  member_count?: number;
  created_at: string;
}

// 组成员接口
export interface GroupMember {
  id: number;
  group_id: number;
  user_id: number;
  role: string;
  joined_at: string;
  user?: UserInfo;
}

// 邀请接口
export interface Invitation {
  id: number;
  type: 'invite' | 'apply';
  from_user_id: number;
  to_user_id: number;
  group_id?: number;
  message?: string;
  status: InvitationStatus;
  responded_at?: string;
  created_at: string;
  expires_at?: string;
  from_user?: UserInfo;
  to_user?: UserInfo;
  group?: ResearchGroup;
}

// 共享资源接口
export interface SharedResource {
  id: number;
  resource_type: ShareType;
  resource_id: number;
  owner_id: number;
  shared_with_type: 'user' | 'group' | 'all_students';
  shared_with_id?: number;
  permission: SharePermission;
  created_at: string;
  expires_at?: string;
  owner?: UserInfo;
  resource_name?: string;
}

// 公告接口
export interface Announcement {
  id: number;
  mentor_id: number;
  group_id?: number;
  title: string;
  content: string;
  is_pinned: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  mentor?: UserInfo;
  group?: ResearchGroup;
  is_read?: boolean;
  read_count?: number;
}

// 系统统计接口
export interface SystemStatistics {
  time_window_days: number;
  total_users: number;
  admin_count: number;
  mentor_count: number;
  student_count: number;
  active_users: number;
  inactive_users: number;
  total_conversations: number;
  total_knowledge_bases: number;
  total_documents: number;
  total_papers: number;
  total_notebooks: number;
  total_groups: number;
  pending_invitations: number;
  total_shared_resources: number;
  total_announcements: number;
  students_with_mentor: number;
  students_without_mentor: number;
  activity: {
    new_users_last_7_days: number;
    new_conversations_last_7_days: number;
    new_knowledge_bases_last_7_days: number;
    new_papers_last_7_days: number;
    new_notebooks_last_7_days: number;
  };
  collaboration: {
    total_groups: number;
    active_groups: number;
    total_group_members: number;
    pending_invitations: number;
    total_shared_resources: number;
    total_announcements: number;
    active_announcements: number;
  };
  mentorship: {
    students_with_mentor: number;
    students_without_mentor: number;
  };
  document_pipeline: {
    total_documents: number;
    completed_documents: number;
    running_documents: number;
    failed_documents: number;
    pending_documents: number;
    timeout_documents: number;
    cancelled_documents: number;
  };
  trends_7d: {
    users: Array<{ date: string; count: number }>;
    conversations: Array<{ date: string; count: number }>;
    knowledge_bases: Array<{ date: string; count: number }>;
    papers: Array<{ date: string; count: number }>;
    notebooks: Array<{ date: string; count: number }>;
  };
  share_breakdown: Array<{ key: string; label: string; count: number }>;
  invitation_breakdown: Array<{ key: string; label: string; count: number }>;
  top_mentors: Array<{
    mentor_id: number;
    username: string;
    full_name?: string;
    student_count: number;
    group_count: number;
  }>;
  recent_activity: Array<{
    id: string;
    type: string;
    title: string;
    owner_name: string;
    owner_role: string;
    created_at: string;
  }>;
  ai_rag: {
    assistant_messages_last_window: number;
    rag_messages_last_window: number;
    knowledge_search_calls_last_window: number;
    citation_required_answers_last_window: number;
    citation_valid_answers_last_window: number;
    citation_repair_attempts_last_window: number;
    citation_repair_successes_last_window: number;
    compression_calls_last_window: number;
    compression_fallback_chunks_last_window: number;
    assistant_total_tokens_last_window: number;
    agent_runs_last_window: number;
    successful_agent_runs_last_window: number;
  };
  codelab: {
    notebooks_active_last_window: number;
    executed_notebooks: number;
    total_execution_count: number;
    code_cells: number;
    executed_code_cells: number;
    agent_runs_last_window: number;
    agent_tokens_last_window: number;
  };
  literature: {
    total_collections: number;
    active_read_sessions_last_window: number;
    annotations_last_window: number;
    comments_last_window: number;
    ratings_last_window: number;
    qa_sessions_last_window: number;
    qa_messages_last_window: number;
    knowledge_links_total: number;
    knowledge_link_breakdown: Array<{ key: string; label: string; count: number }>;
  };
}

const emptyDailySeries = () => ({
  users: [],
  conversations: [],
  knowledge_bases: [],
  papers: [],
  notebooks: [],
});

const defaultSystemStatistics = (): SystemStatistics => ({
  time_window_days: 7,
  total_users: 0,
  admin_count: 0,
  mentor_count: 0,
  student_count: 0,
  active_users: 0,
  inactive_users: 0,
  total_conversations: 0,
  total_knowledge_bases: 0,
  total_documents: 0,
  total_papers: 0,
  total_notebooks: 0,
  total_groups: 0,
  pending_invitations: 0,
  total_shared_resources: 0,
  total_announcements: 0,
  students_with_mentor: 0,
  students_without_mentor: 0,
  activity: {
    new_users_last_7_days: 0,
    new_conversations_last_7_days: 0,
    new_knowledge_bases_last_7_days: 0,
    new_papers_last_7_days: 0,
    new_notebooks_last_7_days: 0,
  },
  collaboration: {
    total_groups: 0,
    active_groups: 0,
    total_group_members: 0,
    pending_invitations: 0,
    total_shared_resources: 0,
    total_announcements: 0,
    active_announcements: 0,
  },
  mentorship: {
    students_with_mentor: 0,
    students_without_mentor: 0,
  },
  document_pipeline: {
    total_documents: 0,
    completed_documents: 0,
    running_documents: 0,
    failed_documents: 0,
    pending_documents: 0,
    timeout_documents: 0,
    cancelled_documents: 0,
  },
  trends_7d: emptyDailySeries(),
  share_breakdown: [],
  invitation_breakdown: [],
  top_mentors: [],
  recent_activity: [],
  ai_rag: {
    assistant_messages_last_window: 0,
    rag_messages_last_window: 0,
    knowledge_search_calls_last_window: 0,
    citation_required_answers_last_window: 0,
    citation_valid_answers_last_window: 0,
    citation_repair_attempts_last_window: 0,
    citation_repair_successes_last_window: 0,
    compression_calls_last_window: 0,
    compression_fallback_chunks_last_window: 0,
    assistant_total_tokens_last_window: 0,
    agent_runs_last_window: 0,
    successful_agent_runs_last_window: 0,
  },
  codelab: {
    notebooks_active_last_window: 0,
    executed_notebooks: 0,
    total_execution_count: 0,
    code_cells: 0,
    executed_code_cells: 0,
    agent_runs_last_window: 0,
    agent_tokens_last_window: 0,
  },
  literature: {
    total_collections: 0,
    active_read_sessions_last_window: 0,
    annotations_last_window: 0,
    comments_last_window: 0,
    ratings_last_window: 0,
    qa_sessions_last_window: 0,
    qa_messages_last_window: 0,
    knowledge_links_total: 0,
    knowledge_link_breakdown: [],
  },
});

const normalizeDailyPoints = (value: unknown): Array<{ date: string; count: number }> => {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    const row = item as { date?: unknown; count?: unknown };
    return {
      date: typeof row.date === 'string' ? row.date : '',
      count: Number(row.count || 0),
    };
  });
};

const normalizeBreakdownItems = (value: unknown): Array<{ key: string; label: string; count: number }> => {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    const row = item as { key?: unknown; label?: unknown; count?: unknown };
    return {
      key: typeof row.key === 'string' ? row.key : '',
      label: typeof row.label === 'string' ? row.label : '',
      count: Number(row.count || 0),
    };
  });
};

const normalizeTopMentors = (
  value: unknown,
): Array<{ mentor_id: number; username: string; full_name?: string; student_count: number; group_count: number }> => {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    const row = item as {
      mentor_id?: unknown;
      username?: unknown;
      full_name?: unknown;
      student_count?: unknown;
      group_count?: unknown;
    };
    return {
      mentor_id: Number(row.mentor_id || 0),
      username: typeof row.username === 'string' ? row.username : '',
      full_name: typeof row.full_name === 'string' ? row.full_name : undefined,
      student_count: Number(row.student_count || 0),
      group_count: Number(row.group_count || 0),
    };
  });
};

const normalizeRecentActivity = (
  value: unknown,
): Array<{ id: string; type: string; title: string; owner_name: string; owner_role: string; created_at: string }> => {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    const row = item as {
      id?: unknown;
      type?: unknown;
      title?: unknown;
      owner_name?: unknown;
      owner_role?: unknown;
      created_at?: unknown;
    };
    return {
      id: typeof row.id === 'string' ? row.id : '',
      type: typeof row.type === 'string' ? row.type : '',
      title: typeof row.title === 'string' ? row.title : '',
      owner_name: typeof row.owner_name === 'string' ? row.owner_name : '',
      owner_role: typeof row.owner_role === 'string' ? row.owner_role : '',
      created_at: typeof row.created_at === 'string' ? row.created_at : '',
    };
  });
};

const normalizeSystemStatistics = (value: unknown): SystemStatistics => {
  const defaults = defaultSystemStatistics();
  const raw = (value && typeof value === 'object' ? value : {}) as Partial<SystemStatistics>;

  return {
    ...defaults,
    ...raw,
    time_window_days: Number(raw.time_window_days || 7),
    inactive_users: Number(raw.inactive_users ?? Math.max(Number(raw.total_users || 0) - Number(raw.active_users || 0), 0)),
    total_documents: Number(raw.total_documents || 0),
    total_groups: Number(raw.total_groups || 0),
    pending_invitations: Number(raw.pending_invitations || 0),
    total_shared_resources: Number(raw.total_shared_resources || 0),
    total_announcements: Number(raw.total_announcements || 0),
    students_with_mentor: Number(raw.students_with_mentor || 0),
    students_without_mentor: Number(raw.students_without_mentor || 0),
    activity: {
      ...defaults.activity,
      ...(raw.activity || {}),
    },
    collaboration: {
      ...defaults.collaboration,
      ...(raw.collaboration || {}),
    },
    mentorship: {
      students_with_mentor: Number(raw.mentorship?.students_with_mentor ?? raw.students_with_mentor ?? 0),
      students_without_mentor: Number(raw.mentorship?.students_without_mentor ?? raw.students_without_mentor ?? 0),
    },
    document_pipeline: {
      ...defaults.document_pipeline,
      total_documents: Number(raw.document_pipeline?.total_documents ?? raw.total_documents ?? 0),
      completed_documents: Number(raw.document_pipeline?.completed_documents || 0),
      running_documents: Number(raw.document_pipeline?.running_documents || 0),
      failed_documents: Number(raw.document_pipeline?.failed_documents || 0),
      pending_documents: Number(raw.document_pipeline?.pending_documents || 0),
      timeout_documents: Number(raw.document_pipeline?.timeout_documents || 0),
      cancelled_documents: Number(raw.document_pipeline?.cancelled_documents || 0),
    },
    trends_7d: {
      users: normalizeDailyPoints(raw.trends_7d?.users),
      conversations: normalizeDailyPoints(raw.trends_7d?.conversations),
      knowledge_bases: normalizeDailyPoints(raw.trends_7d?.knowledge_bases),
      papers: normalizeDailyPoints(raw.trends_7d?.papers),
      notebooks: normalizeDailyPoints(raw.trends_7d?.notebooks),
    },
    share_breakdown: normalizeBreakdownItems(raw.share_breakdown),
    invitation_breakdown: normalizeBreakdownItems(raw.invitation_breakdown),
    top_mentors: normalizeTopMentors(raw.top_mentors),
    recent_activity: normalizeRecentActivity(raw.recent_activity),
    ai_rag: {
      ...defaults.ai_rag,
      ...(raw.ai_rag || {}),
    },
    codelab: {
      ...defaults.codelab,
      ...(raw.codelab || {}),
    },
    literature: {
      ...defaults.literature,
      ...(raw.literature || {}),
      knowledge_link_breakdown: normalizeBreakdownItems(raw.literature?.knowledge_link_breakdown),
    },
  };
};

// 状态接口
interface RoleState {
  // 管理员相关
  users: UserInfo[];
  usersLoading: boolean;
  usersTotal: number;
  statistics: SystemStatistics | null;
  statisticsLoading: boolean;

  // 导师相关
  students: StudentDetail[];
  studentsLoading: boolean;
  groups: ResearchGroup[];
  groupsLoading: boolean;

  // 学生相关
  mentor: UserInfo | null;
  mentorLoading: boolean;

  // 通用
  invitations: Invitation[];
  invitationsLoading: boolean;
  announcements: Announcement[];
  announcementsLoading: boolean;
  sharedResources: SharedResource[];
  sharedResourcesLoading: boolean;

  // 管理员操作
  fetchUsers: (params?: { skip?: number; limit?: number; role?: UserRole; search?: string; is_active?: boolean }) => Promise<void>;
  updateUserRole: (userId: number, role: UserRole) => Promise<void>;
  toggleUserActive: (userId: number) => Promise<void>;
  deleteUser: (userId: number) => Promise<void>;
  fetchStatistics: (days?: number) => Promise<void>;

  // 导师操作
  fetchStudents: () => Promise<void>;
  inviteStudent: (email: string, message?: string) => Promise<void>;
  removeStudent: (studentId: number) => Promise<void>;
  fetchGroups: () => Promise<void>;
  createGroup: (name: string, description?: string, maxMembers?: number) => Promise<void>;
  updateGroup: (groupId: number, data: Partial<ResearchGroup>) => Promise<void>;
  deleteGroup: (groupId: number) => Promise<void>;
  addGroupMember: (groupId: number, userId: number) => Promise<void>;
  removeGroupMember: (groupId: number, userId: number) => Promise<void>;

  // 学生操作
  fetchMentor: () => Promise<void>;
  applyToMentor: (mentorId: number, message?: string) => Promise<void>;
  leaveMentor: () => Promise<void>;
  searchMentors: (query: string) => Promise<UserInfo[]>;

  // 邀请操作
  fetchInvitations: () => Promise<void>;
  acceptInvitation: (invitationId: number) => Promise<void>;
  rejectInvitation: (invitationId: number) => Promise<void>;
  cancelInvitation: (invitationId: number) => Promise<void>;

  // 公告操作
  fetchAnnouncements: () => Promise<void>;
  createAnnouncement: (title: string, content: string, groupId?: number, isPinned?: boolean) => Promise<void>;
  updateAnnouncement: (announcementId: number, data: Partial<Announcement>) => Promise<void>;
  deleteAnnouncement: (announcementId: number) => Promise<void>;
  markAnnouncementRead: (announcementId: number) => Promise<void>;

  // 共享操作
  fetchSharedResources: () => Promise<void>;
  shareResource: (resourceType: ShareType, resourceId: number, sharedWithType: string, sharedWithId?: number, permission?: SharePermission) => Promise<void>;
  updateSharePermission: (shareId: number, permission: SharePermission) => Promise<void>;
  removeShare: (shareId: number) => Promise<void>;
}

export const useRoleStore = create<RoleState>()(
  devtools(
    (set, get) => ({
      // 初始状态
      users: [],
      usersLoading: false,
      usersTotal: 0,
      statistics: null,
      statisticsLoading: false,
      students: [],
      studentsLoading: false,
      groups: [],
      groupsLoading: false,
      mentor: null,
      mentorLoading: false,
      invitations: [],
      invitationsLoading: false,
      announcements: [],
      announcementsLoading: false,
      sharedResources: [],
      sharedResourcesLoading: false,

      // 管理员操作
      fetchUsers: async (params) => {
        set({ usersLoading: true });
        try {
          const [usersResponse, countResponse] = await Promise.all([
            api.get('/api/v1/admin/users', { params }),
            api.get('/api/v1/admin/users/count', {
              params: {
                role: params?.role,
                search: params?.search,
                is_active: params?.is_active,
              },
            }),
          ]);
          set({
            users: usersResponse.data,
            usersTotal: Number(countResponse.data?.count || 0),
            usersLoading: false,
          });
        } catch (error) {
          console.error('获取用户列表失败:', error);
          set({ usersLoading: false });
        }
      },

      updateUserRole: async (userId, role) => {
        try {
          await api.put(`/api/v1/admin/users/${userId}/role`, { role });
          const { users } = get();
          set({
            users: users.map(u => u.id === userId ? { ...u, role } : u)
          });
        } catch (error) {
          console.error('更新用户角色失败:', error);
          throw error;
        }
      },

      toggleUserActive: async (userId) => {
        try {
          const response = await api.put(`/api/v1/admin/users/${userId}/toggle-active`);
          const { users } = get();
          set({
            users: users.map(u => u.id === userId ? { ...u, is_active: response.data.is_active } : u)
          });
        } catch (error) {
          console.error('切换用户状态失败:', error);
          throw error;
        }
      },

      deleteUser: async (userId) => {
        try {
          await api.delete(`/api/v1/admin/users/${userId}`);
          const { users } = get();
          set({ users: users.filter(u => u.id !== userId) });
        } catch (error) {
          console.error('删除用户失败:', error);
          throw error;
        }
      },

      fetchStatistics: async (days) => {
        set({ statisticsLoading: true });
        try {
          const response = await api.get('/api/v1/admin/statistics', {
            params: typeof days === 'number' ? { days } : undefined,
          });
          set({ statistics: normalizeSystemStatistics(response.data), statisticsLoading: false });
        } catch (error) {
          console.error('获取统计数据失败:', error);
          set({ statisticsLoading: false });
        }
      },

      // 导师操作
      fetchStudents: async () => {
        set({ studentsLoading: true });
        try {
          const response = await api.get('/api/v1/mentor/students');
          set({ students: response.data, studentsLoading: false });
        } catch (error) {
          console.error('获取学生列表失败:', error);
          set({ studentsLoading: false });
        }
      },

      inviteStudent: async (email, message) => {
        try {
          await api.post('/api/v1/mentor/students/invite', { email, message });
        } catch (error) {
          console.error('邀请学生失败:', error);
          throw error;
        }
      },

      removeStudent: async (studentId) => {
        try {
          await api.delete(`/api/v1/mentor/students/${studentId}`);
          const { students } = get();
          set({ students: students.filter(s => s.id !== studentId) });
        } catch (error) {
          console.error('移除学生失败:', error);
          throw error;
        }
      },

      fetchGroups: async () => {
        set({ groupsLoading: true });
        try {
          const response = await api.get('/api/v1/mentor/groups');
          set({ groups: response.data, groupsLoading: false });
        } catch (error) {
          console.error('获取研究组失败:', error);
          set({ groupsLoading: false });
        }
      },

      createGroup: async (name, description, maxMembers) => {
        try {
          const response = await api.post('/api/v1/mentor/groups', { name, description, max_members: maxMembers });
          const { groups } = get();
          set({ groups: [...groups, response.data] });
        } catch (error) {
          console.error('创建研究组失败:', error);
          throw error;
        }
      },

      updateGroup: async (groupId, data) => {
        try {
          const response = await api.put(`/api/v1/mentor/groups/${groupId}`, data);
          const { groups } = get();
          set({ groups: groups.map(g => g.id === groupId ? response.data : g) });
        } catch (error) {
          console.error('更新研究组失败:', error);
          throw error;
        }
      },

      deleteGroup: async (groupId) => {
        try {
          await api.delete(`/api/v1/mentor/groups/${groupId}`);
          const { groups } = get();
          set({ groups: groups.filter(g => g.id !== groupId) });
        } catch (error) {
          console.error('删除研究组失败:', error);
          throw error;
        }
      },

      addGroupMember: async (groupId, userId) => {
        try {
          await api.post(`/api/v1/mentor/groups/${groupId}/members`, { user_id: userId });
          get().fetchGroups();
        } catch (error) {
          console.error('添加组成员失败:', error);
          throw error;
        }
      },

      removeGroupMember: async (groupId, userId) => {
        try {
          await api.delete(`/api/v1/mentor/groups/${groupId}/members/${userId}`);
          get().fetchGroups();
        } catch (error) {
          console.error('移除组成员失败:', error);
          throw error;
        }
      },

      // 学生操作
      fetchMentor: async () => {
        set({ mentorLoading: true });
        try {
          const response = await api.get('/api/v1/student/mentor');
          set({ mentor: response.data, mentorLoading: false });
        } catch (error: any) {
          if (error.response?.status === 404) {
            set({ mentor: null, mentorLoading: false });
          } else {
            console.error('获取导师信息失败:', error);
            set({ mentorLoading: false });
          }
        }
      },

      applyToMentor: async (mentorId, message) => {
        try {
          await api.post('/api/v1/student/mentor/apply', { mentor_id: mentorId, message });
        } catch (error) {
          console.error('申请导师失败:', error);
          throw error;
        }
      },

      leaveMentor: async () => {
        try {
          await api.delete('/api/v1/student/mentor/leave');
          set({ mentor: null });
        } catch (error) {
          console.error('离开导师失败:', error);
          throw error;
        }
      },

      searchMentors: async (query) => {
        try {
          const response = await api.get('/api/v1/student/mentors/search', { params: { query } });
          return response.data;
        } catch (error) {
          console.error('搜索导师失败:', error);
          return [];
        }
      },

      // 邀请操作
      fetchInvitations: async () => {
        set({ invitationsLoading: true });
        try {
          const response = await api.get('/api/v1/invitations');
          set({ invitations: response.data, invitationsLoading: false });
        } catch (error) {
          console.error('获取邀请列表失败:', error);
          set({ invitationsLoading: false });
        }
      },

      acceptInvitation: async (invitationId) => {
        try {
          await api.post(`/api/v1/invitations/${invitationId}/accept`);
          const { invitations } = get();
          set({
            invitations: invitations.map(i => 
              i.id === invitationId ? { ...i, status: InvitationStatus.ACCEPTED } : i
            )
          });
        } catch (error) {
          console.error('接受邀请失败:', error);
          throw error;
        }
      },

      rejectInvitation: async (invitationId) => {
        try {
          await api.post(`/api/v1/invitations/${invitationId}/reject`);
          const { invitations } = get();
          set({
            invitations: invitations.map(i => 
              i.id === invitationId ? { ...i, status: InvitationStatus.REJECTED } : i
            )
          });
        } catch (error) {
          console.error('拒绝邀请失败:', error);
          throw error;
        }
      },

      cancelInvitation: async (invitationId) => {
        try {
          await api.delete(`/api/v1/invitations/${invitationId}`);
          const { invitations } = get();
          set({ invitations: invitations.filter(i => i.id !== invitationId) });
        } catch (error) {
          console.error('取消邀请失败:', error);
          throw error;
        }
      },

      // 公告操作
      fetchAnnouncements: async () => {
        set({ announcementsLoading: true });
        try {
          const response = await api.get('/api/v1/announcements');
          set({ announcements: response.data, announcementsLoading: false });
        } catch (error) {
          console.error('获取公告失败:', error);
          set({ announcementsLoading: false });
        }
      },

      createAnnouncement: async (title, content, groupId, isPinned) => {
        try {
          const response = await api.post('/api/v1/announcements', { 
            title, content, group_id: groupId, is_pinned: isPinned 
          });
          const { announcements } = get();
          set({ announcements: [response.data, ...announcements] });
        } catch (error) {
          console.error('创建公告失败:', error);
          throw error;
        }
      },

      updateAnnouncement: async (announcementId, data) => {
        try {
          const response = await api.put(`/api/v1/announcements/${announcementId}`, data);
          const { announcements } = get();
          set({
            announcements: announcements.map(a => a.id === announcementId ? response.data : a)
          });
        } catch (error) {
          console.error('更新公告失败:', error);
          throw error;
        }
      },

      deleteAnnouncement: async (announcementId) => {
        try {
          await api.delete(`/api/v1/announcements/${announcementId}`);
          const { announcements } = get();
          set({ announcements: announcements.filter(a => a.id !== announcementId) });
        } catch (error) {
          console.error('删除公告失败:', error);
          throw error;
        }
      },

      markAnnouncementRead: async (announcementId) => {
        try {
          await api.post(`/api/v1/announcements/${announcementId}/read`);
          const { announcements } = get();
          set({
            announcements: announcements.map(a => 
              a.id === announcementId ? { ...a, is_read: true } : a
            )
          });
        } catch (error) {
          console.error('标记公告已读失败:', error);
        }
      },

      // 共享操作
      fetchSharedResources: async () => {
        set({ sharedResourcesLoading: true });
        try {
          const response = await api.get('/api/v1/share');
          set({ sharedResources: response.data, sharedResourcesLoading: false });
        } catch (error) {
          console.error('获取共享资源失败:', error);
          set({ sharedResourcesLoading: false });
        }
      },

      shareResource: async (resourceType, resourceId, sharedWithType, sharedWithId, permission) => {
        try {
          const response = await api.post('/api/v1/share', {
            resource_type: resourceType,
            resource_id: resourceId,
            shared_with_type: sharedWithType,
            shared_with_id: sharedWithId,
            permission: permission || SharePermission.READ,
          });
          const { sharedResources } = get();
          set({ sharedResources: [...sharedResources, response.data] });
        } catch (error) {
          console.error('共享资源失败:', error);
          throw error;
        }
      },

      updateSharePermission: async (shareId, permission) => {
        try {
          const response = await api.put(`/api/v1/share/${shareId}`, { permission });
          const { sharedResources } = get();
          set({
            sharedResources: sharedResources.map(s => s.id === shareId ? response.data : s)
          });
        } catch (error) {
          console.error('更新共享权限失败:', error);
          throw error;
        }
      },

      removeShare: async (shareId) => {
        try {
          await api.delete(`/api/v1/share/${shareId}`);
          const { sharedResources } = get();
          set({ sharedResources: sharedResources.filter(s => s.id !== shareId) });
        } catch (error) {
          console.error('取消共享失败:', error);
          throw error;
        }
      },
    }),
    { name: 'role-store' }
  )
);
