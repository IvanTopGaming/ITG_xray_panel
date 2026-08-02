import type { RoutingRule } from './types';

const IP_ONLY_PREFIXES = ['geoip:'];
const DOMAIN_ONLY_PREFIXES = ['geosite:', 'domain:', 'regexp:', 'keyword:', 'full:', 'dotless:'];
const ALLOWED_PROTOCOLS = ['http', 'tls', 'quic', 'bittorrent'];
const ALLOWED_NETWORKS = ['tcp', 'udp'];

function startsWithAny(value: string, prefixes: string[]): boolean {
  const v = (value.startsWith('!') ? value.slice(1) : value).toLowerCase();
  return prefixes.some((p) => v.startsWith(p));
}

export function validateRuleFieldPrefixes(rules: RoutingRule[]): string | null {
  for (let i = 0; i < rules.length; i++) {
    const rule = rules[i];
    const n = i + 1;
    for (const entry of rule.domain ?? []) {
      if (startsWithAny(entry, IP_ONLY_PREFIXES)) {
        return `Rule #${n}: "${entry}" is an IP category — move it to the IPS field, not Domains`;
      }
    }
    for (const [field, label] of [
      ['ip', 'IPS'],
      ['source', 'Source IP'],
    ] as const) {
      for (const entry of rule[field] ?? []) {
        if (startsWithAny(entry, DOMAIN_ONLY_PREFIXES)) {
          return `Rule #${n}: "${entry}" is a domain matcher — move it to the Domains field, not ${label}`;
        }
      }
    }
    for (const entry of rule.protocol ?? []) {
      if (!ALLOWED_PROTOCOLS.includes(entry.toLowerCase())) {
        return `Rule #${n}: unknown protocol "${entry}" — allowed: ${ALLOWED_PROTOCOLS.join(', ')}`;
      }
    }
    for (const token of (rule.network ?? '').split(',')) {
      const t = token.trim().toLowerCase();
      if (t && !ALLOWED_NETWORKS.includes(t)) {
        return `Rule #${n}: unknown network "${t}" — allowed: ${ALLOWED_NETWORKS.join(', ')}`;
      }
    }
  }
  return null;
}
