import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export interface AuthState {
  token: string | null;
  username: string | null;
  login: (token: string, username: string) => void;
  logout: () => void;
  isAuthenticated: () => boolean;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null,
      username: null,
      login: (token, username) => set({ token, username }),
      logout: () => set({ token: null, username: null }),
      isAuthenticated: () => !!get().token,
    }),
    { name: 'auth' }
  )
);
