interface ImportMetaEnv {
  readonly BASE_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare const __APP_VERSIONS__: {
  master: string;
  worker: string;
  sub: string;
  bot_api: string;
  caddy: string;
  bot: string;
  xray_core_ref: string;
  [key: string]: string;
};

declare const __FRONTEND_VERSION_KEY__: string;
declare const __EXPECTED_PANEL_ROLE__: string;
