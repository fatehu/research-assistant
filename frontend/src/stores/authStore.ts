import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { authApi, UserRole as ApiUserRole } from '@/services/api'

export type UserRole = ApiUserRole

// 鐢ㄦ埛鎺ュ彛锛堟墿灞曚簡瑙掕壊瀛楁锛?
export interface User {
  id: number
  email: string
  username: string
  full_name?: string
  avatar?: string
  bio?: string
  role: UserRole  // 鏂板锛氱敤鎴疯鑹?
  mentor_id?: number  // 鏂板锛氬甯圛D锛堝鐢熸墠鏈夛級
  department?: string  // 鏂板锛氭墍灞為櫌绯?
  research_direction?: string  // 鏂板锛氱爺绌舵柟鍚?
  joined_at?: string  // 鏂板锛氬姞鍏ュ甯堢粍鏃堕棿
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
  
  // 瑙掕壊鐩稿叧杈呭姪鏂规硶
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
          // 纭繚鐢ㄦ埛鏈夎鑹插瓧娈碉紝榛樿涓?student
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
          // 鏂版敞鍐岀敤鎴烽粯璁や负 student
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
        
        // 濡傛灉娌℃湁 token锛岀洿鎺ヨ涓烘湭璁よ瘉
        if (!token) {
          set({ isAuthenticated: false, isInitialized: true })
          return
        }
        
        // 濡傛灉宸叉湁鐢ㄦ埛淇℃伅锛屽厛璁句负宸茶璇侊紙涔愯鏇存柊锛?
        if (user) {
          set({ isAuthenticated: true, isInitialized: true })
        }
        
        // 鍚庡彴楠岃瘉 token 鏈夋晥鎬э紙涓嶉樆濉烇級
        try {
          const userData = await authApi.me()
          // 纭繚鐢ㄦ埛鏈夎鑹插瓧娈?
          const userWithRole = {
            ...userData,
            role: userData.role || ApiUserRole.STUDENT,
          }
          set({ user: userWithRole as User, isAuthenticated: true, isInitialized: true })
        } catch {
          // token 鏃犳晥锛屾竻闄ょ姸鎬?
          set({ user: null, token: null, isAuthenticated: false, isInitialized: true })
        }
      },
      
      // 瑙掕壊杈呭姪鏂规硶
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
      // 鎸佷箙鍖?token 鍜?user
      partialize: (state) => ({ 
        token: state.token,
        user: state.user,
      }),
      // hydration 瀹屾垚鍚庣殑鍥炶皟
      onRehydrateStorage: () => (state) => {
        // hydration 瀹屾垚鍚庯紝濡傛灉鏈?token 鍜?user锛岀珛鍗宠缃负宸茶璇?
        if (state?.token && state?.user) {
          state.isAuthenticated = true
          state.isInitialized = true
        } else if (state) {
          // 濡傛灉娌℃湁鏈夋晥鏁版嵁锛屼篃璁剧疆涓哄凡鍒濆鍖?
          state.isInitialized = true
        }
      },
    }
  )
)

