import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { authApi, UserRole as ApiUserRole } from '@/services/api'

export type UserRole = ApiUserRole

export interface User {
  id: number
  email: string
  username: string
  full_name?: string
  avatar?: string
  bio?: string
  role: UserRole
  mentor_id?: number
  department?: string
  research_direction?: string
  joined_at?: string
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
  
  isAdmin: () => boolean
  isMentor: () => boolean
  isStudent: () => boolean
  hasRole: (roles: string | string[]) => boolean
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
          // Ensure user has a role, default to student
          const user = {
            ...response.user,
            role: response.user.role || ApiUserRole.STUDENT,
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
          // New users default to student
          const user = {
            ...response.user,
            role: response.user.role || ApiUserRole.STUDENT,
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
        
        // If no token exists, mark as unauthenticated
        if (!token) {
          set({ isAuthenticated: false, isInitialized: true })
          return
        }
        
        // If user info exists, set authenticated first (optimistic update)
        if (user) {
          set({ isAuthenticated: true, isInitialized: true })
        }
        
        // Validate token in background (non-blocking)
        try {
          const userData = await authApi.me()
          // Ensure user object has role field
          const userWithRole = {
            ...userData,
            role: userData.role || ApiUserRole.STUDENT,
          }
          set({ user: userWithRole as User, isAuthenticated: true, isInitialized: true })
        } catch {
          // Token invalid, clear auth state
          set({ user: null, token: null, isAuthenticated: false, isInitialized: true })
        }
      },
      
      // Role helper methods
      isAdmin: () => {
        const { user } = get()
        return user?.role === ApiUserRole.ADMIN
      },
      
      isMentor: () => {
        const { user } = get()
        return user?.role === ApiUserRole.MENTOR
      },
      
      isStudent: () => {
        const { user } = get()
        return user?.role === ApiUserRole.STUDENT
      },
      
      hasRole: (roles: string | string[]) => {
        const { user } = get()
        if (!user?.role) {
          return false
        }
        return Array.isArray(roles) ? roles.includes(user.role) : user.role === roles
      },
    }),
    {
      name: 'auth-storage',
      // Persist only token and user
      partialize: (state) => ({ 
        token: state.token,
        user: state.user,
      }),
      // Callback after hydration completes
      onRehydrateStorage: () => (state) => {
        // After hydration, token + user means authenticated
        if (state?.token && state?.user) {
          state.isAuthenticated = true
          state.isInitialized = true
        } else if (state) {
          // If data is invalid, still mark as initialized
          state.isInitialized = true
        }
      },
    }
  )
)

