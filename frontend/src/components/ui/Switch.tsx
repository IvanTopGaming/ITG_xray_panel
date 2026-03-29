import { forwardRef } from 'react';
import { cn } from '@/lib/utils';

interface SwitchProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
}

const Switch = forwardRef<HTMLInputElement, SwitchProps>(({ className, label, ...props }, ref) => (
  <label className={cn('flex items-center cursor-pointer gap-3', className)}>
    <div className="relative">
      <input type="checkbox" className="sr-only peer" ref={ref} {...props} />
      <div className="w-11 h-6 bg-gray-700 peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-primary rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-primary"></div>
    </div>
    {label && <span className="text-sm font-medium text-gray-300">{label}</span>}
  </label>
));
Switch.displayName = 'Switch';
export { Switch };
