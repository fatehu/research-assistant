import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { authApi, User } from '@/services/api'

interface AuthState {
  user: User | null
  token: string | null
  isAuthenticated: boolean
  isLoading: boolean
  isInitialized: boolean  // 是否已初始化
  
  // Actions
  login: (email: string, password: string) => Promise<void>
  register: (email: string, username: string, password: string, fullName?: string) => Promise<void>
  logout: () => void
  updateUser: (user: Partial<User>) => void
  checkAuth: () => Promise<void>
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
          set({
            user: response.user,
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
          set({
            user: response.user,
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
          set({ user: userData, isAuthenticated: true, isInitialized: true })
        } catch {
          // token 无效，清除状态
          set({ user: null, token: null, isAuthenticated: false, isInitialized: true })
        }
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
