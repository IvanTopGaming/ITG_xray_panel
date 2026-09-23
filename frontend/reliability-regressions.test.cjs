const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const esbuild = require('esbuild');

const React = require('react');
const renderer = require('react-test-renderer');
const { QueryClient, QueryClientProvider } = require('@tanstack/react-query');
const { act } = renderer;
const api = {};
let linkedPanels = [
  { id: 1, name: 'A' },
  { id: 2, name: 'B' },
];
const notices = [];
const motion = new Proxy({}, { get: (_, tag) => tag });
const toast = new Proxy({}, { get: (_, kind) => (message) => notices.push({ kind, message }) });
global.window = {
  location: { hostname: 'panel.test', pathname: '/api/sub/u/token', search: '' },
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  addEventListener() {},
  removeEventListener() {},
};
global.document = {
  body: { style: {} },
  querySelector: () => null,
  addEventListener() {},
  removeEventListener() {},
};
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.ResizeObserver = class {
  observe() {}
  disconnect() {}
};
global.__APP_VERSIONS__ = { master: 'dev' };
global.__FRONTEND_VERSION_KEY__ = 'frontend_admin';

async function load(relative, exports = []) {
  const filename = path.join(__dirname, 'packages', relative);
  const output = await esbuild.build({
    stdin: {
      contents:
        readFileSync(filename, 'utf8') +
        (exports.length ? `\nexport { ${exports.join(', ')} };` : ''),
      resolveDir: path.dirname(filename),
      loader: filename.endsWith('.tsx') ? 'tsx' : 'ts',
    },
    bundle: true,
    platform: 'node',
    format: 'cjs',
    jsx: 'automatic',
    write: false,
    packages: 'external',
    loader: { '.css': 'empty' },
    alias: {
      '@ui': path.join(__dirname, 'packages/ui-core/src'),
      '@': path.join(
        __dirname,
        relative.startsWith('sub-page/') ? 'packages/sub-page/src' : 'packages/admin/src'
      ),
    },
    plugins: [
      {
        name: 'boundary',
        setup(build) {
          build.onResolve(
            { filter: /(?:@ui\/lib\/api|@ui\/lib\/panelRole|@ui\/hooks\/useLinkedPanels)$/ },
            (args) => ({ path: args.path, external: true })
          );
        },
      },
    ],
  });
  const module = { exports: {} };
  const localRequire = (name) => {
    if (name.endsWith('.css')) return {};
    if (name === '@ui/lib/api') return api;
    if (name === '@ui/lib/panelRole')
      return { hasLocalXray: false, isWorker: false, isMaster: true };
    if (name === '@ui/hooks/useLinkedPanels')
      return {
        useLinkedPanels: () => ({
          data: linkedPanels,
        }),
      };
    if (name === 'framer-motion')
      return {
        motion,
        AnimatePresence: ({ children }) => children,
        useMotionValue: require('framer-motion').useMotionValue,
        animate: (_, value, options) => {
          options.onUpdate?.(value);
          return { stop() {} };
        },
      };
    if (name === 'react-dom') return { ...require(name), createPortal: (children) => children };
    if (name === 'react-toastify') return { toast };
    return require(name);
  };
  new Function('require', 'module', 'exports', output.outputFiles[0].text)(
    localRequire,
    module,
    module.exports
  );
  return module.exports;
}

async function mount(Component, props = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  let tree;
  await act(async () => {
    tree = renderer.create(
      React.createElement(QueryClientProvider, { client }, React.createElement(Component, props)),
      {
        createNodeMock: () => ({
          getBoundingClientRect: () => ({ left: 0, width: 800, top: 0, bottom: 10 }),
          focus() {},
          blur() {},
        }),
      }
    );
  });
  await settle();
  return {
    tree,
    client,
    close: () => {
      act(() => tree.unmount());
      client.clear();
    },
  };
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 5));
  });
}
const text = (node) => (typeof node === 'string' ? node : (node.children || []).map(text).join(''));
const button = (tree, label) =>
  tree.root.findAllByType('button').find((node) => text(node) === label);

test('VMess Unicode names survive UTF-8 export', async () => {
  const { generateLink } = await load('ui-core/src/lib/protocols.ts');
  const link = generateLink(
    { protocol: 'vmess', port: 443, streamSettings: { network: 'tcp' } },
    { id: 'id', email: 'Иван😎' },
    'vpn.test'
  );
  assert.equal(JSON.parse(Buffer.from(link.slice(8), 'base64').toString('utf8')).ps, 'Иван😎');
});

test('VMess gRPC export keeps the configured service name', async () => {
  const { generateLink } = await load('ui-core/src/lib/protocols.ts');
  const link = generateLink(
    {
      protocol: 'vmess',
      port: 443,
      streamSettings: { network: 'grpc', grpcSettings: { serviceName: 'vpn-grpc' } },
    },
    { id: 'id', email: 'user' },
    'vpn.test'
  );
  assert.equal(JSON.parse(Buffer.from(link.slice(8), 'base64').toString()).path, 'vpn-grpc');
});

test('WireGuard export uses the server-assigned address', async () => {
  const { generateLink } = await load('ui-core/src/lib/protocols.ts');
  const inbound = {
    protocol: 'wireguard',
    port: 443,
    streamSettings: { wgPublicKey: 'server-key' },
  };
  assert.match(
    generateLink(
      inbound,
      { id: 'private-key', email: 'user', wg_address: '172.19.34.9/32' },
      'vpn.test'
    ),
    /Address = 172\.19\.34\.9\/32/
  );
  assert.equal(generateLink(inbound, { id: 'private-key', email: 'user' }, 'vpn.test'), '');
});

test('TagInput retains every valid unique pasted routing matcher', async () => {
  const { TagInput } = await load('ui-core/src/components/ui/TagInput.tsx');
  let tags;
  function Harness() {
    const [value, setValue] = React.useState(['domain:existing.test']);
    tags = value;
    return React.createElement(TagInput, {
      value,
      onChange: setValue,
      pattern: null,
      maxLength: 100,
    });
  }
  const mounted = await mount(Harness);
  act(() =>
    mounted.tree.root
      .findByType('input')
      .props.onChange({ target: { value: 'domain:a.test,domain:b.test,domain:a.test,' } })
  );
  assert.deepEqual(tags, ['domain:existing.test', 'domain:a.test', 'domain:b.test']);
  mounted.close();
});

test('Traffic chart survives a hovered series shrinking', async () => {
  const { AreaChart } = await load('ui-core/src/pages/Statistics.tsx', ['AreaChart']);
  const points = Array.from({ length: 10 }, (_, index) => ({
    ts: 100 + index,
    up: index,
    down: index * 2,
  }));
  let tree;
  act(() => {
    tree = renderer.create(React.createElement(AreaChart, { points }), {
      createNodeMock: () => ({ getBoundingClientRect: () => ({ left: 0, width: 800 }) }),
    });
  });
  act(() => tree.root.findByType('svg').props.onMouseMove({ clientX: 790 }));
  assert.doesNotThrow(() =>
    act(() => tree.update(React.createElement(AreaChart, { points: points.slice(0, 2) })))
  );
  act(() => tree.unmount());
});

test('Text drafts preserve the other language and reset the selected language', async () => {
  const { TextsTab } = await load('admin/src/components/bot/TextsTab.tsx');
  api.get = async (url) => ({
    data: url.endsWith('/keys')
      ? {
          keys: [
            { key: 'welcome', default_ru: 'RU default', default_en: 'EN default', variables: [] },
          ],
        }
      : {
          texts: [
            { key: 'welcome', lang: 'ru', text: 'RU saved' },
            { key: 'welcome', lang: 'en', text: 'EN saved' },
          ],
        },
  });
  api.delete = async () => ({ data: {} });
  const mounted = await mount(TextsTab);
  assert.ok(
    button(mounted.tree, 'welcome'),
    mounted.client
      .getQueryCache()
      .getAll()
      .map((query) => query.state.error?.stack)
      .join('\n')
  );
  act(() => button(mounted.tree, 'welcome').props.onClick());
  act(() =>
    mounted.tree.root
      .findAllByType('textarea')[0]
      .props.onChange({ target: { value: 'RU edited' } })
  );
  assert.equal(mounted.tree.root.findAllByType('textarea')[1].props.value, 'EN saved');
  api.get = async (url) => ({
    data: url.endsWith('/keys')
      ? {
          keys: [
            { key: 'welcome', default_ru: 'RU default', default_en: 'EN default', variables: [] },
          ],
        }
      : { texts: [{ key: 'welcome', lang: 'en', text: 'EN saved' }] },
  });
  act(() => button(mounted.tree, 'Reset').props.onClick());
  await settle();
  await settle();
  assert.equal(mounted.tree.root.findAllByType('textarea')[0].props.value, 'RU default');
  mounted.close();
});

test('Payment state separates confirmed money from incomplete delivery and refund revocation', async () => {
  const { PaymentState } = await load('admin/src/components/bot/PaymentState.tsx');
  for (const fulfillment of ['pending', 'blocked', 'retry', 'review']) {
    const ui = await mount(PaymentState, {
      payment: {
        status: 'pending',
        provider_status: 'succeeded',
        fulfillment_status: fulfillment,
        checkout_status: 'ready',
        refund_status: 'none',
        fulfillment_error: 'Node unavailable',
      },
    });
    const output = JSON.stringify(ui.tree.toJSON());
    assert.match(output, /Payment received/);
    assert.match(output, new RegExp(`Delivery: ${fulfillment}`));
    assert.match(output, /Node unavailable/);
    ui.close();
  }
  const refund = await mount(PaymentState, {
    payment: {
      status: 'refunded',
      provider_status: 'succeeded',
      fulfillment_status: 'succeeded',
      refund_status: 'pending',
      refunded_amount_kopeks: 12345,
      refund_pending_targets: [{ panel_id: 2, error: 'Node timeout' }],
    },
  });
  const output = JSON.stringify(refund.tree.toJSON());
  assert.match(output, /Payment received/);
  assert.match(output, /Full refund confirmed; access revocation pending/);
  assert.match(output, /123.45/);
  assert.match(output, /Node 2: Node timeout/);
  refund.close();
  for (const [refund_status, expected] of [
    ['partial', 'Partial refund; access retained'],
    ['processing', 'Full refund confirmed; access revocation processing'],
    ['completed', 'Full refund confirmed; access revoked'],
  ]) {
    const ui = await mount(PaymentState, {
      payment: {
        status: 'succeeded',
        provider_status: 'succeeded',
        refund_status,
        checkout_status: 'review',
      },
    });
    assert.ok(JSON.stringify(ui.tree.toJSON()).includes(expected));
    assert.match(JSON.stringify(ui.tree.toJSON()), /Checkout: review/);
    ui.close();
  }
  const { PaymentsTab } = await load('admin/src/components/bot/PaymentsTab.tsx');
  api.get = async () => ({
    data: {
      items: [
        {
          id: 1,
          created_at: '2026-09-13T00:00:00Z',
          status: 'pending',
          provider_status: 'succeeded',
          fulfillment_status: 'review',
          fulfillment_error: 'Delivery requires review',
        },
      ],
      total: 1,
      stats: { month_count: 1, month_amount_rub: 100 },
    },
  });
  const page = await mount(PaymentsTab);
  assert.match(JSON.stringify(page.tree.toJSON()), /Delivery requires review/);
  assert.match(JSON.stringify(page.tree.toJSON()), /Payment received/);
  page.close();
});

test('Health separates Telegram delivery debt and paid fulfillment from a drained bus', async () => {
  const { HealthLines } = await load('ui-core/src/pages/System.tsx', ['HealthLines']);
  const ui = await mount(HealthLines, {
    isLoading: false,
    health: {
      undelivered_events: { available: true, count: 0 },
      event_delivery: {
        available: true,
        pending: 2,
        leased: 1,
        review: 1,
        permanent: 1,
        oldest_pending_ms: Date.now() - 600000,
        needs_attention: true,
      },
      stuck_payments: {
        available: true,
        processing: 0,
        pending_over_a_day: 0,
        pending_fulfillment: 2,
        pending_refunds: 1,
        review: 1,
      },
      data_tier: { database: 'ok', shared_redis: 'ok' },
      offsite_backup: { applicable: false },
    },
  });
  const output = JSON.stringify(ui.tree.toJSON());
  assert.match(output, /Telegram delivery/);
  assert.match(output, /2 pending/);
  assert.match(output, /1 review/);
  assert.match(output, /1 failed/);
  assert.match(output, /2 delivery/);
  assert.match(output, /1 refund/);
  const deliveryLabel = ui.tree.root
    .findAllByType('span')
    .find((node) => text(node) === 'Telegram delivery');
  assert.ok(
    deliveryLabel.parent
      .findAllByType('span')
      .some((node) => node.props.className?.includes('text-error'))
  );
  ui.close();
});

test('Payment badges always identify processing, refunded, and unknown statuses', async () => {
  const { PaymentStatusBadge } = await load('admin/src/components/bot/PaymentStatusBadge.tsx');
  for (const status of ['processing', 'refunded', 'future_status']) {
    const element = PaymentStatusBadge({ status });
    assert.ok(element.props.children, status);
    assert.ok(!element.props.className.includes('undefined'));
  }
});

test('Scheduler health shows failed and overdue jobs instead of process liveness', async () => {
  const { HealthLines } = await load('ui-core/src/pages/System.tsx', ['HealthLines']);
  const health = {
    undelivered_events: { available: true, count: 0 },
    stuck_payments: { available: true, processing: 0, pending_over_a_day: 0 },
    data_tier: { database: 'ok', shared_redis: 'ok' },
    offsite_backup: { applicable: false },
    jobs: {
      available: true,
      needs_attention: true,
      items: [
        {
          role: 'worker',
          job_id: 'check_limits',
          status: 'overdue',
          stale: true,
          last_success_at_ms: null,
          failures: 0,
        },
        {
          role: 'cron',
          job_id: 'poll_linked_panels',
          status: 'failed',
          stale: false,
          last_success_at_ms: Date.now(),
          last_failure_at_ms: Date.now(),
          last_error: 'RuntimeError',
          failures: 1,
        },
      ],
    },
  };
  const ui = await mount(HealthLines, { health, isLoading: false });
  const output = JSON.stringify(ui.tree.toJSON());
  assert.match(output, /1 failed/);
  assert.match(output, /1 overdue/);
  assert.match(output, /worker: check_limits/);
  assert.match(output, /never succeeded/);
  assert.match(output, /RuntimeError/);
  ui.close();
  const unknown = await mount(HealthLines, {
    isLoading: false,
    health: { ...health, jobs: { available: false, items: [], error: 'Job status unavailable' } },
  });
  assert.match(JSON.stringify(unknown.tree.toJSON()), /Job status unavailable/);
  unknown.close();
});

test('Grant editors convert naive UTC dates through the configured display timezone', async () => {
  const drawer = await load('admin/src/components/bot/UserDrawer.tsx', [
    'toIsoOrNull',
    'toLocalInputValue',
  ]);
  assert.equal(drawer.toLocalInputValue('2026-09-20T12:00:00'), '2026-09-20T15:00');
  assert.equal(drawer.toIsoOrNull('2026-09-20T15:00'), '2026-09-20T12:00:00.000Z');
  const grants = await load('admin/src/components/bot/GrantsTab.tsx', ['toIsoOrNull']);
  assert.equal(grants.toIsoOrNull('2026-09-20T15:00'), '2026-09-20T12:00:00.000Z');
});

test('Datetime conversion handles DST transition days without shifting valid wall times', async () => {
  const datetime = await load('ui-core/src/lib/datetime.ts');
  datetime.setDisplayTimezone('America/New_York');
  assert.equal(
    datetime.epochMsFromLocalDateTimeInput('2026-03-08T03:30'),
    Date.parse('2026-03-08T07:30:00Z')
  );
  assert.equal(
    datetime.epochMsFromLocalDateTimeInput('2026-11-01T02:30'),
    Date.parse('2026-11-01T07:30:00Z')
  );
  assert.ok(Number.isNaN(datetime.epochMsFromLocalDateTimeInput('2026-03-08T02:30')));
});

test('Saving one bot settings section preserves edits in another', async () => {
  const { SettingsTab } = await load('admin/src/components/bot/SettingsTab.tsx');
  let settings = {
    bot_config_version: 1,
    bot_token: 'saved-token',
    admin_ids: [123],
    display_timezone: 'Europe/Moscow',
    brand_name: 'Saved brand',
  };
  api.get = async () => ({ data: settings });
  api.put = async (_, payload) => {
    settings = { ...settings, ...payload, bot_config_version: 2 };
    return { data: {} };
  };
  const mounted = await mount(SettingsTab);
  const field = (label) =>
    mounted.tree.root
      .findAllByType('label')
      .find((node) => text(node).startsWith(label))
      .findByType('input');
  act(() => field('Brand name').props.onChange({ target: { value: 'Unsaved brand' } }));
  const devices = mounted.tree.root
    .findAllByType('section')
    .find((node) => text(node).startsWith('Subscriptions'));
  act(() =>
    devices
      .findAllByType('button')
      .find((node) => text(node) === 'Save')
      .props.onClick()
  );
  await settle();
  await settle();
  assert.equal(field('Brand name').props.value, 'Unsaved brand');
  mounted.close();
});

test('Bot settings failed initial request offers a visible error and retry', async () => {
  const { SettingsTab } = await load('admin/src/components/bot/SettingsTab.tsx');
  api.get = async () => {
    throw new Error('network down');
  };
  const mounted = await mount(SettingsTab);
  assert.match(text(mounted.tree.toJSON()), /failed|could not|error/i);
  assert.ok(button(mounted.tree, 'Retry'));
  mounted.close();
});

test('Inbound editor requests routing profiles from its target node', async () => {
  const { InboundForm } = await load('ui-core/src/components/inbound/InboundForm.tsx');
  const calls = [];
  api.get = async (url, options) => {
    calls.push({ url, options });
    return { data: [] };
  };
  const inbound = {
    tag: 'shared',
    protocol: 'vless',
    port: 443,
    panel_id: 2,
    streamSettings: { network: 'tcp', security: 'none' },
    settings: { clients: [] },
  };
  const mounted = await mount(InboundForm, { inbound, onSuccess() {}, onCancel() {} });
  const request = calls.find((call) => call.url.startsWith('/routing-profiles'));
  assert.ok(
    request.url.includes('panel_id=2') || request.options?.params?.panel_id === 2,
    JSON.stringify(request)
  );
  assert.ok(
    mounted.client
      .getQueryCache()
      .getAll()
      .some((query) => query.queryKey[0] === 'routing-profiles' && query.queryKey.includes(2))
  );
  mounted.close();
});

test('Payment date filters cover an entire display-timezone day and paginate', async () => {
  const { listPayments } = await load('admin/src/lib/bot.ts');
  let request;
  api.get = async (url) => {
    request = new URL(url, 'https://panel.test');
    return { data: { items: [], total: 0 } };
  };
  await listPayments({ from: '2026-09-13', to: '2026-09-13', limit: 50, offset: 100 });
  assert.equal(request.searchParams.get('from'), '2026-09-12T21:00:00.000Z');
  assert.equal(request.searchParams.get('to_exclusive'), '2026-09-13T21:00:00.000Z');
  assert.equal(request.searchParams.get('limit'), '50');
  assert.equal(request.searchParams.get('offset'), '100');
});

test('Tariff inbound matching distinguishes equal tags belonging to different nodes', async () => {
  const { ItemRow } = await load('admin/src/components/bot/TariffDrawer.tsx', ['ItemRow']);
  const mounted = await mount(ItemRow, {
    item: { panel_id: 2, inbound_tag: 'shared', limit_gb: 0 },
    allInbounds: [
      { panel_id: 1, tag: 'shared', protocol: 'trojan', port: 8443 },
      { panel_id: 2, tag: 'shared', protocol: 'vmess', port: 443 },
    ],
    panels: [
      { id: 1, name: 'A' },
      { id: 2, name: 'B' },
    ],
    onChange() {},
    onRemove() {},
  });
  assert.ok(!text(mounted.tree.toJSON()).includes('trojan :8443'));
  assert.match(text(mounted.tree.toJSON()), /vmess :443/);
  mounted.close();
});

test('A late text save keeps a newer language draft dirty', async () => {
  const { TextsTab } = await load('admin/src/components/bot/TextsTab.tsx');
  api.get = async (url) => ({
    data: url.endsWith('/keys')
      ? {
          keys: [
            { key: 'welcome', default_ru: 'RU default', default_en: 'EN default', variables: [] },
          ],
        }
      : { texts: [] },
  });
  let resolveSave;
  api.put = (_, payload) =>
    new Promise((resolve) => {
      resolveSave = () => resolve({ data: { key: 'welcome', ...payload } });
    });
  const mounted = await mount(TextsTab);
  act(() => button(mounted.tree, 'welcome').props.onClick());
  act(() =>
    mounted.tree.root.findAllByType('textarea')[0].props.onChange({ target: { value: 'first' } })
  );
  act(() => button(mounted.tree, 'Save').props.onClick());
  await settle();
  act(() =>
    mounted.tree.root.findAllByType('textarea')[0].props.onChange({ target: { value: 'second' } })
  );
  await act(async () => resolveSave());
  await settle();
  assert.equal(mounted.tree.root.findAllByType('textarea')[0].props.value, 'second');
  assert.equal(button(mounted.tree, 'Save').props.disabled, false);
  mounted.close();
});

test('Bulk reset reports actual partial count and retains failed targets', async () => {
  const { BulkToolbar } = await load('ui-core/src/pages/Dashboard.tsx', ['BulkToolbar']);
  const good = '1\0shared\0a';
  const failed = '2\0shared\0b';
  let selection = new Set([good, failed]);
  notices.length = 0;
  api.post = async () => ({
    data: {
      status: 'reset',
      reset: 1,
      errors: ['Node B offline'],
      failed_users: [{ panel_id: 2, tag: 'shared', email: 'b' }],
    },
  });
  const mounted = await mount(BulkToolbar, {
    selectedUsers: selection,
    clearSelection: (completed) => {
      selection = completed
        ? new Set([...selection].filter((key) => !completed.has(key)))
        : new Set();
    },
  });
  act(() => button(mounted.tree, ' Reset').props.onClick());
  const confirm = button(mounted.tree, 'Reset');
  assert.ok(confirm, mounted.tree.root.findAllByType('button').map(text).join('|'));
  act(() => confirm.props.onClick());
  await settle();
  assert.deepEqual([...selection], [failed]);
  assert.ok(notices.some((notice) => notice.kind !== 'success' && notice.message.includes('1')));
  mounted.close();
});

test('Malformed export data cannot crash the client row', async () => {
  const { UserRow } = await load('ui-core/src/pages/Dashboard.tsx', ['UserRow']);
  api.get = async () => ({ data: [] });
  const mounted = await mount(UserRow, {
    inbound: { panel_id: 2, tag: 'test', protocol: 'vmess', streamSettings: null },
    client: { id: 'id', email: 'valid-user', up: 0, down: 0, enable: true },
    now: Date.now(),
    isSelected: false,
    onToggleSelect() {},
    panelQs: '?panel_id=2',
    panels: [{ id: 2, url: 'https://vpn.test' }],
  });
  const copy = mounted.tree.root
    .findAllByType('button')
    .find(
      (node) =>
        node.props.title === 'Copy link' || /export unavailable/i.test(node.props.title || '')
    );
  assert.ok(copy?.props.disabled);
  mounted.close();
});

test('Pending grants and partial account operations never claim complete success', async () => {
  const { UserDrawer } = await load('admin/src/components/bot/UserDrawer.tsx');
  const detail = {
    telegram_id: 123,
    blocked: false,
    clients: [],
    payments: [],
    grants: [
      { id: 1, tariff_id: 1, billing: 'free', access_until: null, provisioning_status: 'pending' },
    ],
  };
  api.get = async (url) => ({
    data:
      url === '/bot/tariffs'
        ? { tariffs: [{ id: 1, name: 'VPN', enabled: true, visibility: 'public', items: [] }] }
        : detail,
  });
  api.post = async () => ({
    status: 202,
    data: {
      ...detail.grants[0],
      cancelled_grants: 0,
      disabled_clients: 0,
      panel_failures: [{ panel_id: 2, panel_name: 'B', error: 'offline' }],
    },
  });
  const ui = await mount(UserDrawer, { open: true, telegramId: 123, onClose() {} });
  assert.match(JSON.stringify(ui.tree.toJSON()), /Awaiting provisioning/);
  notices.length = 0;
  act(() => button(ui.tree, 'Block user').props.onClick());
  const confirm = ui.tree.root
    .findAllByType('button')
    .find((node) => text(node).includes('Block & cancel access'));
  assert.ok(confirm);
  act(() => confirm.props.onClick());
  await settle();
  assert.ok(notices.some((notice) => notice.kind === 'warning'));
  assert.ok(!notices.some((notice) => notice.kind === 'success'));
  act(() => button(ui.tree, 'Grant access').props.onClick());
  const tariffSelect = ui.tree.root.findAll(
    (node) => node.props.options?.[0]?.label === 'Select a tariff…'
  )[0];
  act(() => tariffSelect.props.onChange({ target: { value: '1' } }));
  notices.length = 0;
  act(() => button(ui.tree, 'Save grant').props.onClick());
  await settle();
  assert.ok(
    notices.some(
      (notice) => notice.kind === 'warning' && notice.message.includes('Awaiting provisioning')
    )
  );
  assert.ok(!notices.some((notice) => notice.kind === 'success'));
  api.patch = api.post;
  act(() => button(ui.tree, 'Edit term').props.onClick());
  notices.length = 0;
  act(() => button(ui.tree, 'Save term').props.onClick());
  await settle();
  assert.ok(notices.some((notice) => notice.kind === 'warning'));
  assert.ok(!notices.some((notice) => notice.kind === 'success'));
  ui.close();
  detail.blocked = true;
  const blocked = await mount(UserDrawer, { open: true, telegramId: 123, onClose() {} });
  notices.length = 0;
  act(() => button(blocked.tree, 'Unblock').props.onClick());
  await settle();
  assert.ok(notices.some((notice) => notice.kind === 'warning'));
  assert.ok(!notices.some((notice) => notice.kind === 'success'));
  blocked.close();
});

test('Grant revoke warns about failed nodes instead of claiming complete success', async () => {
  const { GrantsTab } = await load('admin/src/components/bot/GrantsTab.tsx');
  api.get = async (url) => ({
    data:
      url === '/bot/grants'
        ? {
            rows: [
              {
                id: 1,
                telegram_id: 123,
                tariff_id: 1,
                tariff_name: 'VPN',
                billing: 'free',
                access_until: null,
              },
            ],
          }
        : url === '/bot/tariffs'
          ? { tariffs: [] }
          : { users: [] },
  });
  api.delete = async () => ({
    data: {
      ok: false,
      disabled_clients: 0,
      revoked_grants: 0,
      panel_failures: [{ panel_id: 2, panel_name: 'Node B', error: 'offline' }],
    },
  });
  notices.length = 0;
  const mounted = await mount(GrantsTab);
  const remove = mounted.tree.root
    .findAllByType('button')
    .find((node) => node.props.title?.includes('Revoke') || text(node).includes('Revoke'));
  assert.ok(
    remove,
    mounted.tree.root
      .findAllByType('button')
      .map((node) => `${text(node)}:${node.props.title}`)
      .join('|')
  );
  act(() => remove.props.onClick());
  const confirm = mounted.tree.root
    .findAllByType('button')
    .find((node) => text(node) === 'Revoke grant');
  assert.ok(confirm);
  act(() => confirm.props.onClick());
  await settle();
  assert.ok(
    notices.some((notice) => notice.kind === 'warning' && notice.message.includes('Node B'))
  );
  assert.ok(!notices.some((notice) => notice.kind === 'success'));
  mounted.close();
});

test('An older aborted log request cannot terminate a newly started stream', async () => {
  const { useLogStore, useAuthStore } = await load('ui-core/src/stores/logStore.ts', [
    'useAuthStore',
  ]);
  useAuthStore.getState().login('token', 'admin');
  const requests = [];
  const originalFetch = global.fetch;
  global.fetch = (_, options) =>
    new Promise((resolve, reject) => requests.push({ ...options, resolve, reject }));
  try {
    const first = useLogStore.getState().toggleStream();
    await useLogStore.getState().toggleStream();
    const second = useLogStore.getState().toggleStream();
    requests[0].reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
    await first;
    assert.equal(useLogStore.getState().isStreaming, true);
    useAuthStore.getState().logout();
    assert.equal(requests[1].signal.aborted, true);
    assert.equal(useLogStore.getState().isStreaming, false);
    assert.deepEqual(useLogStore.getState().logs, []);
    requests[1].reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
    await second;
  } finally {
    for (const request of requests)
      request.reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
    global.fetch = originalFetch;
  }
});

test('Switching nodes never reuses the same-tag inbound draft', async () => {
  const { default: Dashboard } = await load('ui-core/src/pages/Dashboard.tsx');
  const inbounds = [1, 2].map((panel_id) => ({
    panel_id,
    tag: 'shared',
    protocol: 'vless',
    port: 443,
    enable: true,
    streamSettings: { network: 'tcp' },
    settings: { clients: [] },
  }));
  api.get = async (url) => ({
    data:
      url === '/inbounds' ? inbounds : url.startsWith('/stats') ? { cpu: 1, mem_percent: 1 } : [],
  });
  const mounted = await mount(Dashboard);
  const selectNode = (id) =>
    mounted.tree.root
      .findAll((node) => node.props.options?.some((option) => option.label === 'All panels'))[0]
      .props.onChange({ target: { value: String(id) } });
  act(() => selectNode(1));
  const draft = () =>
    mounted.tree.root
      .findAllByType('input')
      .find((node) => node.props.placeholder === 'New user email / username');
  act(() => draft().props.onChange({ target: { value: 'only-on-A' } }));
  act(() => selectNode(2));
  assert.equal(draft().props.value, '');
  mounted.close();
});

test('Logout clears authenticated query data in both apps', async () => {
  for (const app of ['admin', 'node']) {
    const { queryClient, useAuthStore } = await load(`${app}/src/App.tsx`, [
      'queryClient',
      'useAuthStore',
    ]);
    useAuthStore.getState().login('token', 'admin');
    queryClient.setQueryData(['inbounds'], [{ private_key: 'cached-secret' }]);
    useAuthStore.getState().logout();
    assert.equal(queryClient.getQueryData(['inbounds']), undefined, app);
    queryClient.clear();
  }
});

test('An open restart confirmation retains its original node after that node disappears', async () => {
  const { default: System } = await load('ui-core/src/pages/System.tsx');
  linkedPanels = [
    { id: 1, name: 'A' },
    { id: 2, name: 'B' },
  ];
  api.get = async (url) => ({
    data: url.startsWith('/outbounds')
      ? []
      : url.includes('version')
        ? { running: {}, latest: null }
        : {},
  });
  let restartUrl;
  api.post = async (url) => {
    restartUrl = url;
    return { data: {} };
  };
  const mounted = await mount(System);
  try {
    act(() => button(mounted.tree, 'Maintenance').props.onClick());
    act(() => button(mounted.tree, 'Restart Core').props.onClick());
    linkedPanels = [{ id: 2, name: 'B' }];
    await act(async () =>
      mounted.client.setQueryData(['system-settings', 1], { xrayLogLevel: 'debug' })
    );
    await settle();
    act(() => button(mounted.tree, 'Restart').props.onClick());
    await settle();
    assert.equal(restartUrl, '/restart?panel_id=1');
  } finally {
    mounted.close();
    linkedPanels = [
      { id: 1, name: 'A' },
      { id: 2, name: 'B' },
    ];
  }
});

test('Controlled confirmation stays open until its mutation finishes', async () => {
  const { ConfirmationModal } = await load('ui-core/src/components/ui/ConfirmationModal.tsx');
  let closes = 0;
  const mounted = await mount(ConfirmationModal, {
    isOpen: true,
    isLoading: false,
    onClose: () => {
      closes++;
    },
    onConfirm() {},
    title: 'Delete',
    description: 'Delete selected target?',
  });
  act(() => button(mounted.tree, 'Confirm').props.onClick());
  assert.equal(closes, 0);
  mounted.close();
});

test('A failed traffic query is shown as an error rather than empty history', async () => {
  const { default: Statistics } = await load('ui-core/src/pages/Statistics.tsx');
  api.get = async (url) => {
    if (url.includes('/stats/traffic')) throw new Error('node unavailable');
    return { data: { inbounds: [], top_users: [], users: [] } };
  };
  const mounted = await mount(Statistics);
  try {
    await settle();
    assert.ok(!text(mounted.tree.toJSON()).includes('No data for this period'));
    assert.match(text(mounted.tree.toJSON()), /unreachable|failed|error/i);
  } finally {
    mounted.close();
  }
});

test('Subscription page aborts pending work on unmount and classifies HTTP failures', async () => {
  const { useSubInfo } = await load('sub-page/src/hooks/useSubInfo.ts');
  let state;
  function Harness() {
    state = useSubInfo();
    return null;
  }
  const originalFetch = global.fetch;
  let signal;
  global.fetch = (_, options) => {
    signal = options.signal;
    return new Promise(() => {});
  };
  let mounted = await mount(Harness);
  try {
    mounted.close();
    assert.equal(signal.aborted, true);
    for (const [status, expected] of [
      [404, 'not_found'],
      [503, 'unavailable'],
    ]) {
      global.fetch = async () => ({ ok: false, status });
      mounted = await mount(Harness);
      await settle();
      assert.equal(state.error, expected);
      mounted.close();
    }
  } finally {
    global.fetch = originalFetch;
  }
});

test('Subscription page rejects malformed responses and presents unknown expiry honestly', async () => {
  const { useSubInfo } = await load('sub-page/src/hooks/useSubInfo.ts');
  const { default: Summary } = await load('sub-page/src/components/Summary.tsx');
  let state;
  function Harness() {
    state = useSubInfo();
    return null;
  }
  const originalFetch = global.fetch;
  global.fetch = async () => ({ ok: true, json: async () => ({ nodes: 'malformed' }) });
  const mounted = await mount(Harness);
  try {
    assert.equal(state.data, null);
    assert.equal(state.error, 'invalid_response');
    let tree;
    act(() => {
      tree = renderer.create(
        React.createElement(Summary, { lang: 'en', data: { expiry_at: null, devices: null } })
      );
    });
    assert.match(text(tree.toJSON()), /unknown/i);
    assert.ok(!text(tree.toJSON()).includes('no expiry'));
    act(() => tree.unmount());
  } finally {
    mounted.close();
    global.fetch = originalFetch;
  }
});

test('A late tariff save cannot close a newly opened tariff editor', async () => {
  const { TariffDrawer } = await load('admin/src/components/bot/TariffDrawer.tsx');
  let finishSave;
  let closed = 0;
  const tariff = (id) => ({
    id,
    name: `Tariff ${id}`,
    price_rub: 10,
    period_days: 30,
    visibility: 'public',
    is_trial: false,
    enabled: true,
    sort_order: 0,
    items: [{ panel_id: 1, inbound_tag: 'shared', label: '', traffic_gb: 1, sort_order: 0 }],
  });
  const props = {
    open: true,
    tariff: tariff(1),
    stats: null,
    inbounds: [],
    panels: linkedPanels,
    saving: false,
    onClose: () => {
      closed++;
    },
    onSave: () =>
      new Promise((resolve) => {
        finishSave = resolve;
      }),
  };
  let tree;
  act(() => {
    tree = renderer.create(React.createElement(TariffDrawer, props));
  });
  const save = tree.root.findAllByType('button').find((node) => text(node).includes('Save'));
  assert.ok(save);
  act(() => {
    save.props.onClick();
  });
  act(() => tree.update(React.createElement(TariffDrawer, { ...props, tariff: tariff(2) })));
  await act(async () => finishSave());
  assert.equal(closed, 0);
  act(() => tree.unmount());
});

test('A trial editor starts with the trial period instead of a paid tariff default', async () => {
  const { TariffDrawer } = await load('admin/src/components/bot/TariffDrawer.tsx');
  const mounted = await mount(TariffDrawer, {
    open: true,
    tariff: null,
    stats: null,
    inbounds: [],
    panels: linkedPanels,
    saving: false,
    isTrial: true,
    onClose() {},
    onSave: async () => {},
  });
  const field = mounted.tree.root
    .findAllByType('label')
    .find((node) => text(node).startsWith('Period (days)'))
    .findByType('input');
  assert.equal(field.props.value, '1');
  mounted.close();
});

test('An unmounted client form cannot close a newer editor after its save returns', async () => {
  const { UserForm } = await load('ui-core/src/components/inbound/UserForm.tsx');
  let finish;
  let closes = 0;
  api.put = () =>
    new Promise((resolve) => {
      finish = resolve;
    });
  const mounted = await mount(UserForm, {
    inbound: {
      panel_id: 1,
      tag: 'shared',
      protocol: 'vless',
      streamSettings: { network: 'tcp', security: 'none' },
    },
    client: { id: 'id', email: 'a', limit_bytes: 0, expiry_time: 0, reset_day: 0, enable: true },
    panelQs: '?panel_id=1',
    onClose: () => {
      closes++;
    },
  });
  await act(async () =>
    mounted.tree.root.findByType('form').props.onSubmit({ preventDefault() {}, persist() {} })
  );
  mounted.close();
  await act(async () => finish({ data: {} }));
  assert.equal(closes, 0);
});

test('Subscription data refreshes periodically, on focus, and at a known expiry', async () => {
  const { useSubInfo } = await load('sub-page/src/hooks/useSubInfo.ts');
  const originals = {
    fetch: global.fetch,
    setTimeout: window.setTimeout,
    clearTimeout: window.clearTimeout,
    setInterval: window.setInterval,
    clearInterval: window.clearInterval,
    addEventListener: window.addEventListener,
    removeEventListener: window.removeEventListener,
  };
  const timers = new Map();
  const intervals = new Map();
  const listeners = new Map();
  let id = 0;
  window.setTimeout = (callback, delay) => {
    timers.set(++id, { callback, delay });
    return id;
  };
  window.clearTimeout = (key) => timers.delete(key);
  window.setInterval = (callback) => {
    intervals.set(++id, callback);
    return id;
  };
  window.clearInterval = (key) => intervals.delete(key);
  window.addEventListener = (name, callback) => listeners.set(name, callback);
  window.removeEventListener = (name) => listeners.delete(name);
  let requests = 0;
  let state;
  const body = {
    brand: 'VPN',
    sub_url: 'https://sub.test/api/sub/u/token',
    status: 'active',
    reason: 'active',
    expiry_at: Date.now() + 30000,
    devices: null,
    nodes: [],
    update_interval_hours: 24,
  };
  global.fetch = async () => {
    requests++;
    return { ok: true, json: async () => body };
  };
  function Harness() {
    state = useSubInfo();
    return null;
  }
  let mounted;
  try {
    mounted = await mount(Harness);
    assert.equal(requests, 1);
    await act(async () => [...intervals.values()][0]());
    await settle();
    assert.equal(requests, 2);
    await act(async () => listeners.get('focus')());
    await settle();
    assert.equal(requests, 3);
    const expiry = [...timers.values()].find(
      (timer) => timer.delay > 10000 && timer.delay <= 30050
    );
    assert.ok(expiry);
    global.fetch = async () => ({
      ok: true,
      json: async () => ({ ...body, status: 'disabled', reason: 'expired' }),
    });
    await act(async () => expiry.callback());
    await settle();
    assert.equal(state.data.reason, 'expired');
    mounted.close();
    mounted = null;
    assert.equal(timers.size, 0);
    assert.equal(intervals.size, 0);
    assert.equal(listeners.size, 0);
  } finally {
    mounted?.close();
    global.fetch = originals.fetch;
    for (const key of [
      'setTimeout',
      'clearTimeout',
      'setInterval',
      'clearInterval',
      'addEventListener',
      'removeEventListener',
    ])
      window[key] = originals[key];
  }
});

test('A newly generated bot service token is available once for copying', async () => {
  const { SettingsTab } = await load('admin/src/components/bot/SettingsTab.tsx');
  api.get = async () => ({ data: { has_bot_service_token: false, admin_ids: [] } });
  api.post = async () => ({ data: { token: 'new-service-token' } });
  const mounted = await mount(SettingsTab);
  try {
    act(() => button(mounted.tree, 'Generate token').props.onClick());
    await settle();
    await settle();
    const section = mounted.tree.root
      .findAllByType('section')
      .find((node) => text(node).startsWith('Bot service token'));
    const show = section.findAllByType('button').find((node) => node.props.title === 'Show');
    assert.equal(show.props.disabled, false);
    act(() => show.props.onClick());
    assert.match(text(section), /new-service-token/);
  } finally {
    mounted.close();
  }
});
