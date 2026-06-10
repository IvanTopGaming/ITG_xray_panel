interface ImportMetaEnv {
  readonly BASE_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare const __APP_VERSIONS__: {
  backend: string;
  frontend: string;
  caddy: string;
  bot: string;
  xray_core_ref: string;
};
