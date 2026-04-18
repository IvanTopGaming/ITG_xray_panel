import { useForm, useWatch, Controller } from 'react-hook-form';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Switch } from '@/components/ui/Switch';
import { TagInput } from '@/components/ui/TagInput';
import { Inbound, Client, Node, MasterInfo } from '@/lib/types';
import api from '@/lib/api';
import { toast } from 'react-toastify';
import { RefreshCw } from 'lucide-react';

interface UserFormProps {
  inbound: Inbound;
  client: Client;
  onClose: () => void;
}

export function UserForm({ inbound, client, onClose }: UserFormProps) {
  const queryClient = useQueryClient();
  const { register, handleSubmit, setValue, control } = useForm({
    defaultValues: {
      email: client.email,
      id: client.id,
      limit_gb: client.limit_bytes ? client.limit_bytes / 1024 ** 3 : 0,
      expiry_date: client.expiry_time
        ? new Date(client.expiry_time).toISOString().split('T')[0]
        : '',
      reset_day: client.reset_day,
      enable: client.enable,
      flow: client.flow || '',
      global_limit_gb: client.global_limit_bytes ? client.global_limit_bytes / 1024 ** 3 : 0,
      allowed_node_groups: client.allowed_node_groups || [],
    },
  });

  const flow = useWatch({ control, name: 'flow' });

  const { data: nodes = [] } = useQuery<Node[]>({
    queryKey: ['nodes'],
    queryFn: () => api.get('/nodes').then((r) => r.data),
    staleTime: 60_000,
  });

  const { data: master } = useQuery<MasterInfo>({
    queryKey: ['nodes', 'master'],
    queryFn: () => api.get('/nodes/master').then((r) => r.data),
    staleTime: 60_000,
  });

  const tagSuggestions = Array.from(
    new Set([...nodes.flatMap((n) => n.groups || []), ...(master?.groups || [])])
  ).sort();

  const mutation = useMutation({
    mutationFn: (data: any) => api.put(`/inbounds/${inbound.tag}/users`, data),
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
    mutation.mutate({
      tag: inbound.tag,
      old_email: client.email,
      new_email: data.email,
      new_id: data.id,
      limit_bytes: Number(data.limit_gb) * 1024 ** 3,
      expiry_time: data.expiry_date ? new Date(data.expiry_date).getTime() : 0,
      reset_day: Number(data.reset_day),
      enable: data.enable,
      flow: data.flow,
      global_limit_bytes: Number(data.global_limit_gb) * 1024 ** 3,
      allowed_node_groups: data.allowed_node_groups,
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

      {inbound.protocol === 'vless' && (
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
        <Input label="Expiry Date" type="date" {...register('expiry_date')} />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Global Data Limit (GB)"
          type="number"
          {...register('global_limit_gb')}
          placeholder="0 = unlimited"
        />
        <Input
          label="Auto Reset Day (1-31)"
          type="number"
          {...register('reset_day')}
          placeholder="0 to disable"
        />
      </div>

      <Controller
        control={control}
        name="allowed_node_groups"
        render={({ field }) => (
          <TagInput
            label="Allowed Node Tags"
            value={field.value || []}
            onChange={field.onChange}
            suggestions={tagSuggestions}
            placeholder="Type a tag and press Enter (blank = all nodes)"
            helperText="User will only be provisioned on nodes carrying any of these tags"
          />
        )}
      />

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
