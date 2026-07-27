import axios from 'axios';
import { useAuthStore } from '@ui/stores/authStore';
import { panelBase } from '@ui/lib/panelBase';

const normalizedBase = panelBase.endsWith('/') ? panelBase : `${panelBase}/`;

const api = axios.create({
  baseURL: `${normalizedBase}api`,
});

api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      useAuthStore.getState().logout();
    }
    return Promise.reject(error);
  }
);

export default api;
