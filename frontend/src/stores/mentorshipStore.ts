import { create } from 'zustand'
import { mentorshipApi, Mentorship, MentorshipStatus, UserBrief, UserRole, MentorActivity } from '@/services/api'

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
  fetchStudentActivities: (skip?: number, limit?: number) => Promise<void>
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

export const useMentorshipStore = create<MentorshipState>((set, get) => ({
  // 初始状态
  mentors: [],
  myMentorship: null,
  pendingRequests: [],
  myStudents: [],
  allMentorships: [],
  studentActivities: [],
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
      await get().fetchStudentActivities(0, 50)
    } catch (error) {
      const message = error instanceof Error ? error.message : '获取学生列表失败'
      set({ error: message, isLoading: false, studentActivities: [] })
    }
  },

  fetchStudentActivities: async (skip = 0, limit = 20) => {
    try {
      const activities = await mentorshipApi.getActivities(skip, limit)
      const normalized: StudentActivity[] = (activities as MentorActivity[]).map((item) => ({
        ...item,
        type: item.type as StudentActivity['type'],
        student: {
          ...item.student,
          role: (item.student.role || UserRole.STUDENT) as UserRole,
        },
      }))
      set({ studentActivities: normalized })
    } catch (error) {
      const message = error instanceof Error ? error.message : '获取学生活动失败'
      set({ error: message })
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
    studentActivities: [],
    pendingCount: 0,
    isLoading: false,
    isSubmitting: false,
    error: null,
  }),
}))
