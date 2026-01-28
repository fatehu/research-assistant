import { create } from 'zustand'
import { mentorshipApi, Mentorship, MentorshipStatus, UserBrief } from '@/services/api'

// 学生活动类型（用于导师仪表板）
export interface StudentActivity {
  id: string
  type: 'conversation' | 'notebook' | 'knowledge' | 'literature' | 'codelab'
  title: string
  description?: string
  timestamp: string
  student: UserBrief
}

interface MentorshipState {
  // 数据状态
  mentors: UserBrief[]                    // 可用导师列表
  myMentorship: Mentorship | null         // 学生的师生关系
  pendingRequests: Mentorship[]           // 导师待处理的申请
  myStudents: UserBrief[]                 // 导师名下学生
  allMentorships: Mentorship[]            // 管理员查看所有关系
  studentActivities: StudentActivity[]    // 学生活动（模拟数据）
  pendingCount: number                    // 待处理数量
  
  // 加载状态
  isLoading: boolean
  isSubmitting: boolean
  error: string | null
  
  // === 学生 Actions ===
  fetchMentors: () => Promise<void>
  fetchMyMentorship: () => Promise<void>
  applyMentorship: (mentorId: number, message?: string) => Promise<void>
  cancelApplication: (mentorshipId: number) => Promise<void>
  
  // === 导师 Actions ===
  fetchPendingRequests: () => Promise<void>
  fetchMyStudents: () => Promise<void>
  approveMentorship: (mentorshipId: number, message?: string) => Promise<void>
  rejectMentorship: (mentorshipId: number, message?: string) => Promise<void>
  archiveMentorship: (mentorshipId: number, message?: string) => Promise<void>
  
  // === 管理员 Actions ===
  fetchAllMentorships: () => Promise<void>
  deleteMentorship: (mentorshipId: number) => Promise<void>
  
  // === 通用 ===
  fetchPendingCount: () => Promise<void>
  clearError: () => void
  reset: () => void
}

// 模拟学生活动数据（TODO: 后续接入真实 API）
const mockStudentActivities: StudentActivity[] = [
  {
    id: '1',
    type: 'conversation',
    title: '深度学习模型优化讨论',
    description: '与 AI 助手讨论了 Transformer 模型的优化策略',
    timestamp: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
    student: { id: 1, username: 'student1', full_name: '张三', role: 'student' as const },
  },
  {
    id: '2',
    type: 'notebook',
    title: '实验数据分析',
    description: '完成了实验数据的可视化分析',
    timestamp: new Date(Date.now() - 5 * 60 * 60 * 1000).toISOString(),
    student: { id: 2, username: 'student2', full_name: '李四', role: 'student' as const },
  },
  {
    id: '3',
    type: 'literature',
    title: '论文收藏: Attention Is All You Need',
    description: '收藏并标注了 Transformer 原始论文',
    timestamp: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
    student: { id: 1, username: 'student1', full_name: '张三', role: 'student' as const },
  },
  {
    id: '4',
    type: 'codelab',
    title: 'PyTorch 模型训练',
    description: '运行了 CNN 图像分类训练代码',
    timestamp: new Date(Date.now() - 48 * 60 * 60 * 1000).toISOString(),
    student: { id: 2, username: 'student2', full_name: '李四', role: 'student' as const },
  },
]

export const useMentorshipStore = create<MentorshipState>((set, get) => ({
  // 初始状态
  mentors: [],
  myMentorship: null,
  pendingRequests: [],
  myStudents: [],
  allMentorships: [],
  studentActivities: mockStudentActivities,
  pendingCount: 0,
  isLoading: false,
  isSubmitting: false,
  error: null,

  // === 学生 Actions ===
  
  fetchMentors: async () => {
    set({ isLoading: true, error: null })
    try {
      const mentors = await mentorshipApi.getMentors()
      set({ mentors, isLoading: false })
    } catch (error) {
      const message = error instanceof Error ? error.message : '获取导师列表失败'
      set({ error: message, isLoading: false })
    }
  },

  fetchMyMentorship: async () => {
    set({ isLoading: true, error: null })
    try {
      const response = await mentorshipApi.getMentorships(undefined, 'as_student')
      // 学生只有一个师生关系（取第一个非归档的）
      const active = response.items.find(
        m => m.status === MentorshipStatus.ACTIVE || m.status === MentorshipStatus.PENDING
      )
      set({ myMentorship: active || null, isLoading: false })
    } catch (error) {
      const message = error instanceof Error ? error.message : '获取师生关系失败'
      set({ error: message, isLoading: false })
    }
  },

  applyMentorship: async (mentorId: number, message?: string) => {
    set({ isSubmitting: true, error: null })
    try {
      const mentorship = await mentorshipApi.applyMentorship(mentorId, message)
      set({ myMentorship: mentorship, isSubmitting: false })
    } catch (error) {
      const message = error instanceof Error ? error.message : '申请失败'
      set({ error: message, isSubmitting: false })
      throw error
    }
  },

  cancelApplication: async (mentorshipId: number) => {
    set({ isSubmitting: true, error: null })
    try {
      await mentorshipApi.updateMentorshipStatus(
        mentorshipId,
        MentorshipStatus.ARCHIVED,
        '学生取消申请'
      )
      set({ myMentorship: null, isSubmitting: false })
    } catch (error) {
      const message = error instanceof Error ? error.message : '取消失败'
      set({ error: message, isSubmitting: false })
      throw error
    }
  },

  // === 导师 Actions ===

  fetchPendingRequests: async () => {
    set({ isLoading: true, error: null })
    try {
      const response = await mentorshipApi.getMentorships(MentorshipStatus.PENDING, 'as_mentor')
      set({ pendingRequests: response.items, pendingCount: response.total, isLoading: false })
    } catch (error) {
      const message = error instanceof Error ? error.message : '获取待处理申请失败'
      set({ error: message, isLoading: false })
    }
  },

  fetchMyStudents: async () => {
    set({ isLoading: true, error: null })
    try {
      const students = await mentorshipApi.getMyStudents(MentorshipStatus.ACTIVE)
      set({ myStudents: students, isLoading: false })
    } catch (error) {
      const message = error instanceof Error ? error.message : '获取学生列表失败'
      set({ error: message, isLoading: false })
    }
  },

  approveMentorship: async (mentorshipId: number, message?: string) => {
    set({ isSubmitting: true, error: null })
    try {
      await mentorshipApi.updateMentorshipStatus(mentorshipId, MentorshipStatus.ACTIVE, message)
      // 刷新列表
      await get().fetchPendingRequests()
      await get().fetchMyStudents()
      set({ isSubmitting: false })
    } catch (error) {
      const message = error instanceof Error ? error.message : '审批失败'
      set({ error: message, isSubmitting: false })
      throw error
    }
  },

  rejectMentorship: async (mentorshipId: number, message?: string) => {
    set({ isSubmitting: true, error: null })
    try {
      await mentorshipApi.updateMentorshipStatus(mentorshipId, MentorshipStatus.ARCHIVED, message)
      // 刷新列表
      await get().fetchPendingRequests()
      set({ isSubmitting: false })
    } catch (error) {
      const msg = error instanceof Error ? error.message : '拒绝失败'
      set({ error: msg, isSubmitting: false })
      throw error
    }
  },

  archiveMentorship: async (mentorshipId: number, message?: string) => {
    set({ isSubmitting: true, error: null })
    try {
      await mentorshipApi.updateMentorshipStatus(mentorshipId, MentorshipStatus.ARCHIVED, message)
      // 刷新列表
      await get().fetchMyStudents()
      set({ isSubmitting: false })
    } catch (error) {
      const msg = error instanceof Error ? error.message : '归档失败'
      set({ error: msg, isSubmitting: false })
      throw error
    }
  },

  // === 管理员 Actions ===

  fetchAllMentorships: async () => {
    set({ isLoading: true, error: null })
    try {
      const response = await mentorshipApi.getMentorships()
      set({ allMentorships: response.items, isLoading: false })
    } catch (error) {
      const message = error instanceof Error ? error.message : '获取所有师生关系失败'
      set({ error: message, isLoading: false })
    }
  },

  deleteMentorship: async (mentorshipId: number) => {
    set({ isSubmitting: true, error: null })
    try {
      await mentorshipApi.deleteMentorship(mentorshipId)
      // 刷新列表
      await get().fetchAllMentorships()
      set({ isSubmitting: false })
    } catch (error) {
      const message = error instanceof Error ? error.message : '删除失败'
      set({ error: message, isSubmitting: false })
      throw error
    }
  },

  // === 通用 ===

  fetchPendingCount: async () => {
    try {
      const count = await mentorshipApi.getPendingCount()
      set({ pendingCount: count })
    } catch {
      // 静默失败
    }
  },

  clearError: () => set({ error: null }),

  reset: () => set({
    mentors: [],
    myMentorship: null,
    pendingRequests: [],
    myStudents: [],
    allMentorships: [],
    pendingCount: 0,
    isLoading: false,
    isSubmitting: false,
    error: null,
  }),
}))
