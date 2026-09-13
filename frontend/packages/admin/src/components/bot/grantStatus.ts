import { toast } from 'react-toastify';
import type { GrantProvisioning } from '@ui/lib/types';
import type { GrantWriteResult } from '../../lib/bot';

export function grantProvisioningLabel(grant: GrantProvisioning): string | null {
  switch (grant.provisioning_status) {
    case 'pending':
      return 'Awaiting provisioning';
    case 'revoking':
      return 'Access revocation pending';
    case 'revoked':
      return 'Access revoked';
    case 'review':
      return 'Provisioning needs review';
    default:
      return null;
  }
}

export function notifyGrantResult(data: GrantWriteResult, success: string) {
  const state = grantProvisioningLabel(data);
  if (data.pending || state || data.panel_failures?.length) {
    const nodes = data.panel_failures
      ?.map((failure) => failure.panel_name || `#${failure.panel_id}`)
      .join(', ');
    toast.warning(
      `${state || 'Awaiting provisioning'}. Changes are saved but access is not fully applied.${nodes ? ` Pending nodes: ${nodes}.` : ''}`
    );
  } else {
    toast.success(success);
  }
}
