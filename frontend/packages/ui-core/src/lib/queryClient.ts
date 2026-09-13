import { QueryClient } from '@tanstack/react-query';
import { useAuthStore } from '@ui/stores/authStore';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

useAuthStore.subscribe((state, previous) => {
  if (state.token !== previous.token) queryClient.clear();
});
