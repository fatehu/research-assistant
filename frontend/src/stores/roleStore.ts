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
  total_users: number;
  admin_count: number;
  mentor_count: number;
  student_count: number;
  active_users: number;
  total_conversations: number;
  total_knowledge_bases: number;
  total_papers: number;
  total_notebooks: number;
}

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
  fetchStatistics: () => Promise<void>;

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
          const response = await api.get('/api/admin/users', { params });
          set({ users: response.data, usersLoading: false });
        } catch (error) {
          console.error('获取用户列表失败:', error);
          set({ usersLoading: false });
        }
      },

      updateUserRole: async (userId, role) => {
        try {
          await api.put(`/api/admin/users/${userId}/role`, { role });
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
          const response = await api.put(`/api/admin/users/${userId}/toggle-active`);
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
          await api.delete(`/api/admin/users/${userId}`);
          const { users } = get();
          set({ users: users.filter(u => u.id !== userId) });
        } catch (error) {
          console.error('删除用户失败:', error);
          throw error;
        }
      },

      fetchStatistics: async () => {
        set({ statisticsLoading: true });
        try {
          const response = await api.get('/api/admin/statistics');
          set({ statistics: response.data, statisticsLoading: false });
        } catch (error) {
          console.error('获取统计数据失败:', error);
          set({ statisticsLoading: false });
        }
      },

      // 导师操作
      fetchStudents: async () => {
        set({ studentsLoading: true });
        try {
          const response = await api.get('/api/mentor/students');
          set({ students: response.data, studentsLoading: false });
        } catch (error) {
          console.error('获取学生列表失败:', error);
          set({ studentsLoading: false });
        }
      },

      inviteStudent: async (email, message) => {
        try {
          await api.post('/api/mentor/students/invite', { email, message });
        } catch (error) {
          console.error('邀请学生失败:', error);
          throw error;
        }
      },

      removeStudent: async (studentId) => {
        try {
          await api.delete(`/api/mentor/students/${studentId}`);
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
          const response = await api.get('/api/mentor/groups');
          set({ groups: response.data, groupsLoading: false });
        } catch (error) {
          console.error('获取研究组失败:', error);
          set({ groupsLoading: false });
        }
      },

      createGroup: async (name, description, maxMembers) => {
        try {
          const response = await api.post('/api/mentor/groups', { name, description, max_members: maxMembers });
          const { groups } = get();
          set({ groups: [...groups, response.data] });
        } catch (error) {
          console.error('创建研究组失败:', error);
          throw error;
        }
      },

      updateGroup: async (groupId, data) => {
        try {
          const response = await api.put(`/api/mentor/groups/${groupId}`, data);
          const { groups } = get();
          set({ groups: groups.map(g => g.id === groupId ? response.data : g) });
        } catch (error) {
          console.error('更新研究组失败:', error);
          throw error;
        }
      },

      deleteGroup: async (groupId) => {
        try {
          await api.delete(`/api/mentor/groups/${groupId}`);
          const { groups } = get();
          set({ groups: groups.filter(g => g.id !== groupId) });
        } catch (error) {
          console.error('删除研究组失败:', error);
          throw error;
        }
      },

      addGroupMember: async (groupId, userId) => {
        try {
          await api.post(`/api/mentor/groups/${groupId}/members`, { user_id: userId });
          get().fetchGroups();
        } catch (error) {
          console.error('添加组成员失败:', error);
          throw error;
        }
      },

      removeGroupMember: async (groupId, userId) => {
        try {
          await api.delete(`/api/mentor/groups/${groupId}/members/${userId}`);
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
          const response = await api.get('/api/student/mentor');
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
          await api.post('/api/student/mentor/apply', { mentor_id: mentorId, message });
        } catch (error) {
          console.error('申请导师失败:', error);
          throw error;
        }
      },

      leaveMentor: async () => {
        try {
          await api.delete('/api/student/mentor/leave');
          set({ mentor: null });
        } catch (error) {
          console.error('离开导师失败:', error);
          throw error;
        }
      },

      searchMentors: async (query) => {
        try {
          const response = await api.get('/api/student/mentors/search', { params: { query } });
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
          const response = await api.get('/api/invitations');
          set({ invitations: response.data, invitationsLoading: false });
        } catch (error) {
          console.error('获取邀请列表失败:', error);
          set({ invitationsLoading: false });
        }
      },

      acceptInvitation: async (invitationId) => {
        try {
          await api.post(`/api/invitations/${invitationId}/accept`);
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
          await api.post(`/api/invitations/${invitationId}/reject`);
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
          await api.delete(`/api/invitations/${invitationId}`);
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
          const response = await api.get('/api/announcements');
          set({ announcements: response.data, announcementsLoading: false });
        } catch (error) {
          console.error('获取公告失败:', error);
          set({ announcementsLoading: false });
        }
      },

      createAnnouncement: async (title, content, groupId, isPinned) => {
        try {
          const response = await api.post('/api/announcements', { 
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
          const response = await api.put(`/api/announcements/${announcementId}`, data);
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
          await api.delete(`/api/announcements/${announcementId}`);
          const { announcements } = get();
          set({ announcements: announcements.filter(a => a.id !== announcementId) });
        } catch (error) {
          console.error('删除公告失败:', error);
          throw error;
        }
      },

      markAnnouncementRead: async (announcementId) => {
        try {
          await api.post(`/api/announcements/${announcementId}/read`);
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
          const response = await api.get('/api/share');
          set({ sharedResources: response.data, sharedResourcesLoading: false });
        } catch (error) {
          console.error('获取共享资源失败:', error);
          set({ sharedResourcesLoading: false });
        }
      },

      shareResource: async (resourceType, resourceId, sharedWithType, sharedWithId, permission) => {
        try {
          const response = await api.post('/api/share', {
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
          const response = await api.put(`/api/share/${shareId}`, { permission });
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
          await api.delete(`/api/share/${shareId}`);
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
