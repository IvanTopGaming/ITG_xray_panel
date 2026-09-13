export interface SubNode {
  name: string;
  tag: string;
  used: number;
  limit: number;
  expiry: number | null;
  online: boolean;
  enabled: boolean;
}

export interface SubInfo {
  brand: string;
  sub_url: string;
  status: 'active' | 'disabled';
  expiry_at: number | null;
  reason?: 'active' | 'expired' | 'blocked' | 'traffic_exhausted' | 'disabled' | 'not_configured';
  devices: { count: number; limit: number } | null;
  nodes: SubNode[];
  update_interval_hours: number;
}

export type SubInfoError = 'not_found' | 'unavailable' | 'invalid_response';

const nonnegative = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0;
const expiry = (value: unknown) =>
  value === null || (nonnegative(value) && value <= 8640000000000000);
const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

export function isSubInfo(value: unknown): value is SubInfo {
  if (!record(value) || typeof value.brand !== 'string' || typeof value.sub_url !== 'string')
    return false;
  try {
    const url = new URL(value.sub_url);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return false;
  } catch {
    return false;
  }
  if (
    typeof value.status !== 'string' ||
    !['active', 'disabled'].includes(value.status) ||
    !expiry(value.expiry_at)
  )
    return false;
  if (
    value.reason !== undefined &&
    (typeof value.reason !== 'string' ||
      !['active', 'expired', 'blocked', 'traffic_exhausted', 'disabled', 'not_configured'].includes(
        value.reason
      ))
  )
    return false;
  if (!nonnegative(value.update_interval_hours) || value.update_interval_hours === 0) return false;
  if (
    value.devices !== null &&
    (!record(value.devices) ||
      !nonnegative(value.devices.count) ||
      !nonnegative(value.devices.limit))
  )
    return false;
  return (
    Array.isArray(value.nodes) &&
    value.nodes.every(
      (node) =>
        record(node) &&
        typeof node.name === 'string' &&
        typeof node.tag === 'string' &&
        nonnegative(node.used) &&
        nonnegative(node.limit) &&
        expiry(node.expiry) &&
        typeof node.online === 'boolean' &&
        typeof node.enabled === 'boolean'
    )
  );
}
