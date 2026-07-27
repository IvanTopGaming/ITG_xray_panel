export interface SubNode {
  name: string;
  tag: string;
  used: number;
  limit: number;
  expiry: number;
  online: boolean;
  enabled: boolean;
}

export interface SubInfo {
  brand: string;
  sub_url: string;
  status: 'active' | 'disabled';
  expiry_at: number;
  devices: { count: number; limit: number } | null;
  nodes: SubNode[];
  update_interval_hours: number;
}
