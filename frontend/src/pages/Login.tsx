import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate } from 'react-router-dom';
import api from '@/lib/api';
import { useAuthStore, AuthState } from '@/stores/authStore';
import { Input } from '@/components/ui/Input';
import { Button } from '@/components/ui/Button';
import { Radar, Lock } from 'lucide-react';
import { toast } from 'react-toastify';
import { motion } from 'framer-motion';

const containerVariants = {
  hidden: { opacity: 0 },
  visible: {
    opacity: 1,
    transition: { staggerChildren: 0.1, delayChildren: 0.15 },
  },
};

const itemVariants = {
  hidden: { opacity: 0, y: 16 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.4, ease: [0.25, 0.46, 0.45, 0.94] } },
};

export default function Login() {
  const { register, handleSubmit } = useForm();
  const [isLoading, setIsLoading] = useState(false);
  const login = useAuthStore((state: AuthState) => state.login);
  const navigate = useNavigate();

  const onSubmit = async (data: any) => {
    setIsLoading(true);
    try {
      const res = await api.post('/login', data);
      login(res.data.token, res.data.username || data.username);
      navigate('/');
      toast.success('Welcome back!');
    } catch (error) {
      toast.error('Invalid credentials');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="h-screen w-full flex items-center justify-center bg-[#050505] relative overflow-hidden">
      {/* Background blobs */}
      <motion.div
        animate={{ scale: [1, 1.2, 1], opacity: [0.2, 0.4, 0.2] }}
        transition={{ duration: 8, repeat: Infinity, ease: 'easeInOut' }}
        className="absolute top-[-15%] left-[-10%] w-[600px] h-[600px] bg-primary/15 rounded-full blur-[130px] pointer-events-none"
      />
      <motion.div
        animate={{ scale: [1, 1.15, 1], opacity: [0.15, 0.3, 0.15] }}
        transition={{ duration: 10, repeat: Infinity, ease: 'easeInOut', delay: 3 }}
        className="absolute bottom-[-15%] right-[-10%] w-[600px] h-[600px] bg-violet-700/15 rounded-full blur-[130px] pointer-events-none"
      />

      {/* Dot grid */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          backgroundImage: 'radial-gradient(circle, rgba(208,188,255,0.07) 1px, transparent 1px)',
          backgroundSize: '36px 36px',
        }}
      />

      <motion.div
        variants={containerVariants}
        initial="hidden"
        animate="visible"
        className="relative w-full max-w-sm mx-4"
      >
        {/* Card glow */}
        <div className="absolute inset-0 rounded-[36px] blur-3xl bg-primary/8 scale-110 pointer-events-none" />

        <div className="relative w-full p-8 bg-[#0e0c14]/85 backdrop-blur-2xl rounded-[32px] border border-white/[0.08] shadow-[0_24px_64px_-16px_rgba(0,0,0,0.8),0_0_0_1px_rgba(208,188,255,0.06)]">
          {/* Logo */}
          <motion.div variants={itemVariants} className="flex flex-col items-center mb-10">
            <div className="relative mb-5">
              {/* Pulsing rings */}
              <motion.div
                animate={{ scale: [1, 1.6, 1], opacity: [0.35, 0, 0.35] }}
                transition={{ duration: 2.8, repeat: Infinity, ease: 'easeOut' }}
                className="absolute inset-0 rounded-full bg-primary/20"
              />
              <motion.div
                animate={{ scale: [1, 2.2, 1], opacity: [0.15, 0, 0.15] }}
                transition={{ duration: 2.8, repeat: Infinity, ease: 'easeOut', delay: 0.5 }}
                className="absolute inset-0 rounded-full bg-primary/10"
              />
              {/* Icon container */}
              <motion.div
                whileHover={{ rotate: 180, scale: 1.05 }}
                transition={{ duration: 0.5, ease: 'easeInOut' }}
                className="relative p-4 bg-gradient-to-br from-primary/20 to-violet-600/20 rounded-full border border-primary/20 shadow-[0_0_24px_rgba(208,188,255,0.15)]"
              >
                <Radar size={40} className="text-primary" />
              </motion.div>
            </div>
            <h1 className="text-2xl font-bold text-white tracking-tight">Xray Panel</h1>
            <p className="text-gray-500 text-sm mt-1.5 tracking-wide">Sign in to continue</p>
          </motion.div>

          <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
            <motion.div variants={itemVariants}>
              <Input
                placeholder="Username"
                {...register('username', { required: true })}
                className="bg-white/[0.05] border-white/[0.08] hover:border-white/20 focus:bg-white/[0.07] h-12 transition-colors"
              />
            </motion.div>

            <motion.div variants={itemVariants}>
              <Input
                type="password"
                placeholder="Password"
                {...register('password', { required: true })}
                className="bg-white/[0.05] border-white/[0.08] hover:border-white/20 focus:bg-white/[0.07] h-12 transition-colors"
              />
            </motion.div>

            <motion.div variants={itemVariants} className="pt-2">
              <Button
                type="submit"
                className="w-full h-12 text-base rounded-2xl shadow-[0_0_24px_rgba(208,188,255,0.2)]"
                isLoading={isLoading}
              >
                {!isLoading && <Lock size={15} className="mr-2 opacity-70" />}
                Sign In
              </Button>
            </motion.div>
          </form>
        </div>
      </motion.div>
    </div>
  );
}
