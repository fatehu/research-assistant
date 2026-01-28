import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { authApi } from '@/services/api'

// 用户角色枚举
export enum UserRole {
  ADMIN = 'admin',
  MENTOR = 'mentor',
  STUDENT = 'student',
}

// 用户接口（扩展了角色字段）
export interface User {
  id: number
  email: string
  username: string
  full_name?: string
  avatar?: string
  bio?: string
  role: UserRole  // 新增：用户角色
  mentor_id?: number  // 新增：导师ID（学生才有）
  department?: string  // 新增：所属院系
  research_direction?: string  // 新增：研究方向
  joined_at?: string  // 新增：加入导师组时间
  is_active: boolean
  preferred_llm_provider: string
  preferences: Record<string, unknown>
  created_at: string
  last_login?: string
}

interface AuthState {
  user: User | null
  token: string | null
  isAuthenticated: boolean
  isLoading: boolean
  isInitialized: boolean
  
  // Actions
  login: (email: string, password: string) => Promise<void>
  register: (email: string, username: string, password: string, fullName?: string) => Promise<void>
  logout: () => void
  updateUser: (user: Partial<User>) => void
  checkAuth: () => Promise<void>
  
  // 角色相关辅助方法
  isAdmin: () => boolean
  isMentor: () => boolean
  isStudent: () => boolean
  hasRole: (role: UserRole) => boolean
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      user: null,
      token: null,
      isAuthenticated: false,
      isLoading: false,
      isInitialized: false,
      
      login: async (email: string, password: string) => {
        set({ isLoading: true })
        try {
          const response = await authApi.login(email, password)
          // 确保用户有角色字段，默认为 student
          const user = {
            ...response.user,
            role: response.user.role || UserRole.STUDENT,
          }
          set({
            user,
            token: response.access_token,
            isAuthenticated: true,
            isLoading: false,
            isInitialized: true,
          })
        } catch (error) {
          set({ isLoading: false })
          throw error
        }
      },
      
      register: async (email: string, username: string, password: string, fullName?: string) => {
        set({ isLoading: true })
        try {
          const response = await authApi.register(email, username, password, fullName)
          // 新注册用户默认为 student
          const user = {
            ...response.user,
            role: response.user.role || UserRole.STUDENT,
          }
          set({
            user,
            token: response.access_token,
            isAuthenticated: true,
            isLoading: false,
            isInitialized: true,
          })
        } catch (error) {
          set({ isLoading: false })
          throw error
        }
      },
      
      logout: () => {
        set({
          user: null,
          token: null,
          isAuthenticated: false,
          isInitialized: true,
        })
      },
      
      updateUser: (userData: Partial<User>) => {
        const currentUser = get().user
        if (currentUser) {
          set({ user: { ...currentUser, ...userData } })
        }
      },
      
      checkAuth: async () => {
        const { token, user } = get()
        
        // 如果没有 token，直接设为未认证
        if (!token) {
          set({ isAuthenticated: false, isInitialized: true })
          return
        }
        
        // 如果已有用户信息，先设为已认证（乐观更新）
        if (user) {
          set({ isAuthenticated: true, isInitialized: true })
        }
        
        // 后台验证 token 有效性（不阻塞）
        try {
          const userData = await authApi.me()
          // 确保用户有角色字段
          const userWithRole = {
            ...userData,
            role: userData.role || UserRole.STUDENT,
          }
          set({ user: userWithRole as User, isAuthenticated: true, isInitialized: true })
        } catch {
          // token 无效，清除状态
          set({ user: null, token: null, isAuthenticated: false, isInitialized: true })
        }
      },
      
      // 角色辅助方法
      isAdmin: () => {
        const { user } = get()
        return user?.role === UserRole.ADMIN
      },
      
      isMentor: () => {
        const { user } = get()
        return user?.role === UserRole.MENTOR
      },
      
      isStudent: () => {
        const { user } = get()
        return user?.role === UserRole.STUDENT
      },
      
      hasRole: (role: UserRole) => {
        const { user } = get()
        return user?.role === role
      },
    }),
    {
      name: 'auth-storage',
      // 持久化 token 和 user
      partialize: (state) => ({ 
        token: state.token,
        user: state.user,
      }),
      // hydration 完成后的回调
      onRehydrateStorage: () => (state) => {
        // hydration 完成后，如果有 token 和 user，立即设置为已认证
        if (state?.token && state?.user) {
          state.isAuthenticated = true
          state.isInitialized = true
        } else if (state) {
          // 如果没有有效数据，也设置为已初始化
          state.isInitialized = true
        }
      },
    }
  )
)
