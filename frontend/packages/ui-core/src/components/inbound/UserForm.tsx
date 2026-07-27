import { useForm, useWatch } from 'react-hook-form';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Switch } from '@/components/ui/Switch';
import { Inbound, Client } from '@/lib/types';
import { supportsVlessFlow } from '@/lib/protocols';
import api from '@/lib/api';
import { epochMsFromLocalDateTimeInput, formatDateTimeForLocalInput } from '@/lib/datetime';
import { toast } from 'react-toastify';
import { RefreshCw } from 'lucide-react';

interface UserFormProps {
  inbound: Inbound;
  client: Client;
  onClose: () => void;
  panelQs?: string;
}

export function UserForm({ inbound, client, onClose, panelQs = '' }: UserFormProps) {
  const queryClient = useQueryClient();
  const {
    register,
    handleSubmit,
    setValue,
    control,
    formState: { dirtyFields },
  } = useForm({
    defaultValues: {
      email: client.email,
      id: client.id,
      limit_gb: client.limit_bytes ? client.limit_bytes / 1024 ** 3 : 0,

      expiry_date: formatDateTimeForLocalInput(client.expiry_time || Date.now()),
      reset_day: client.reset_day,
      enable: client.enable,
      flow: client.flow || '',
      device_limit:
        client.device_limit === null || client.device_limit === undefined
          ? ''
          : String(client.device_limit),
    },
  });

  const inboundDeviceLimit = inbound.device_limit ?? 0;

  const flowSupported = supportsVlessFlow(inbound);
  const flow = useWatch({ control, name: 'flow' });

  const mutation = useMutation({
    mutationFn: (data: any) => api.put(`/inbounds/${inbound.tag}/users${panelQs}`, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['inbounds'] });
      toast.success('User updated');
      onClose();
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.error || 'Failed to update user');
    },
  });

  const generateIdMutation = useMutation({
    mutationFn: async () => {
      if (inbound.protocol === 'wireguard') {
        const res = await api.post('/server-keys', { type: 'wireguard' });
        return String(res.data?.privateKey || '');
      }
      return crypto.randomUUID();
    },
    onSuccess: (value: string) => {
      if (value) {
        setValue('id', value);
        toast.success(
          inbound.protocol === 'wireguard' ? 'Private key generated' : 'UUID generated'
        );
      }
    },
    onError: () => {
      toast.error('Failed to generate ID');
    },
  });

  const onSubmit = (data: any) => {
    const expiry_time = dirtyFields.expiry_date
      ? data.expiry_date
        ? epochMsFromLocalDateTimeInput(data.expiry_date)
        : 0
      : client.expiry_time;

    mutation.mutate({
      tag: inbound.tag,
      old_email: client.email,
      new_email: data.email,
      new_id: data.id,
      limit_bytes: Number(data.limit_gb) * 1024 ** 3,
      expiry_time,
      reset_day: Number(data.reset_day),
      enable: data.enable,
      flow: flowSupported ? data.flow : '',
      device_limit:
        data.device_limit === '' || data.device_limit === null || data.device_limit === undefined
          ? null
          : Number(data.device_limit),
    });
  };

  return (
    <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
      <Input label="Email / Username" {...register('email')} />

      <div className="flex gap-2 items-end">
        <Input
          label={inbound.protocol === 'wireguard' ? 'Private Key' : 'UUID / Password'}
          {...register('id')}
          className="font-mono"
        />
        <Button
          type="button"
          variant="secondary"
          onClick={() => generateIdMutation.mutate()}
          isLoading={generateIdMutation.isPending}
        >
          <RefreshCw size={16} />
        </Button>
      </div>

      {flowSupported && (
        <Select
          label="Flow"
          {...register('flow')}
          value={flow}
          options={[
            { value: '', label: 'None' },
            { value: 'xtls-rprx-vision', label: 'xtls-rprx-vision' },
          ]}
        />
      )}

      <div className="grid grid-cols-2 gap-4">
        <Input label="Per-Node Data Limit (GB)" type="number" {...register('limit_gb')} />

        <Input label="Expiry" type="datetime-local" lang="en-GB" {...register('expiry_date')} />
      </div>

      <Input
        label="Auto Reset Day (1-31)"
        type="number"
        {...register('reset_day')}
        placeholder="0 to disable"
      />

      <div>
        <Input
          label="Device limit override"
          type="number"
          min={0}
          {...register('device_limit')}
          placeholder={`from inbound (${inboundDeviceLimit})`}
        />
        <p className="mt-1 text-xs text-gray-500">
          Leave empty to inherit from inbound · 0 = unlimited for this user · N = hard cap
        </p>
      </div>

      <div className="pt-2">
        <Switch label="Enable User" {...register('enable')} />
      </div>

      <div className="flex justify-end gap-3 pt-4">
        <Button type="button" variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" isLoading={mutation.isPending}>
          Save User
        </Button>
      </div>
    </form>
  );
}
