interface ImportMetaEnv {
  readonly BASE_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

interface Window {
  __PANEL_BASE_URL__?: string;
}

declare const __APP_VERSIONS__: {
  backend: string;
  frontend: string;
  caddy: string;
  bot: string;
  xray_core_ref: string;
};
