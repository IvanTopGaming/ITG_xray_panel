package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"
)

func envMap(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

const routesYAML = `
sni_routes:
  - name: proxy
    match: "${PROXY_DOMAIN}"
    upstream: "xray:443"
  - name: panel
    match: "${PANEL_DOMAIN}"
    upstream: "frontend:80"
    tls: true
  - name: sub
    match: "${SUB_DOMAIN}"
    upstream: "backend:5000"
    tls: true
    only_paths:
      - "/api/sub/"
`

func TestLoadConfig_DropsEmptySub(t *testing.T) {
	cfg, err := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
	}))
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if len(cfg.SNIRoutes) != 2 {
		t.Fatalf("want 2 routes (sub dropped), got %d", len(cfg.SNIRoutes))
	}
	for _, r := range cfg.SNIRoutes {
		if r.Name == "sub" {
			t.Fatal("sub route should have been dropped")
		}
	}
}

func TestLoadConfig_KeepsSubWhenSet(t *testing.T) {
	cfg, err := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
	}))
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if len(cfg.SNIRoutes) != 3 {
		t.Fatalf("want 3 routes, got %d", len(cfg.SNIRoutes))
	}
	var sub *Route
	for i := range cfg.SNIRoutes {
		if cfg.SNIRoutes[i].Name == "sub" {
			sub = &cfg.SNIRoutes[i]
		}
	}
	if sub == nil {
		t.Fatal("sub route missing")
	}
	if sub.Match != "sub.example.com" || sub.Upstream != "backend:5000" {
		t.Fatalf("sub route not interpolated: %+v", sub)
	}
	if !sub.TLS || len(sub.OnlyPaths) != 1 {
		t.Fatalf("sub route flags wrong: %+v", sub)
	}
}

func jsonValid(t *testing.T, b []byte) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	return out
}

func TestGenerate_WithSub(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
	}))
	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	root := jsonValid(t, b)

	apps := root["apps"].(map[string]any)
	l4 := apps["layer4"].(map[string]any)
	servers := l4["servers"].(map[string]any)
	main := servers["main"].(map[string]any)
	routes := main["routes"].([]any)
	if len(routes) != 3 {
		t.Fatalf("want 3 layer4 routes, got %d", len(routes))
	}

	httpApp := apps["http"].(map[string]any)
	httpServers := httpApp["servers"].(map[string]any)

	if len(httpServers) != 3 {
		t.Fatalf("want 3 http servers, got %d", len(httpServers))
	}
}

func TestGenerate_WithoutSub(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
	}))
	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	root := jsonValid(t, b)
	apps := root["apps"].(map[string]any)
	l4 := apps["layer4"].(map[string]any)
	main := l4["servers"].(map[string]any)["main"].(map[string]any)
	if len(main["routes"].([]any)) != 2 {
		t.Fatal("want 2 layer4 routes without sub")
	}
	httpServers := apps["http"].(map[string]any)["servers"].(map[string]any)

	if len(httpServers) != 2 {
		t.Fatalf("want 2 http servers (panel + redirect) without sub, got %d", len(httpServers))
	}
	if containsString(string(b), "backend:5000") {
		t.Fatal("backend upstream leaked without SUB_DOMAIN")
	}
}

func TestGenerate_HTTPRedirectOn80(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
	}))
	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	redirect := apps(t, b)["http"].(map[string]any)["servers"].(map[string]any)["http_redirect"]
	if redirect == nil {
		t.Fatal("http_redirect server missing")
	}
	srv := redirect.(map[string]any)
	if listen := srv["listen"].([]any); len(listen) != 1 || listen[0] != ":80" {
		t.Fatalf("redirect server must listen on :80, got %v", srv["listen"])
	}
	str := string(b)
	if !containsString(str, "\"status_code\": 308") {
		t.Fatal("redirect must use 308")
	}
	if !containsString(str, "https://{http.request.host}{http.request.uri}") {
		t.Fatal("redirect Location target missing")
	}
}

func apps(t *testing.T, b []byte) map[string]any {
	t.Helper()
	return jsonValid(t, b)["apps"].(map[string]any)
}

func TestGenerate_SubServerPathFilters(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
	}))
	b, _ := Generate(cfg)
	str := string(b)
	if !containsString(str, "/api/sub/*") {
		t.Fatal("sub path matcher missing")
	}
	if !containsString(str, "backend:5000") {
		t.Fatal("backend upstream missing")
	}
	if !containsString(str, "\"status_code\": 404") && !containsString(str, "\"status_code\":404") {
		t.Fatal("404 catch-all missing for sub server")
	}
}

func TestGenerate_NoDeadXRealIP(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
	}))
	b, _ := Generate(cfg)
	str := string(b)

	if containsString(str, "X-Real-IP") {
		t.Fatal("dead X-Real-IP header should be gone")
	}
	if containsString(str, "client_ip") {
		t.Fatal("stale http.vars.client_ip placeholder should be gone")
	}
	if !containsString(str, "X-Forwarded-Proto") {
		t.Fatal("X-Forwarded-Proto must remain")
	}
}

func TestLoadConfig_DropsEmptyBot(t *testing.T) {
	data, err := os.ReadFile("../routes.yaml")
	if err != nil {
		t.Skipf("routes.yaml not found: %v", err)
	}
	cfg, err := LoadConfig(data, envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
	}))
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	for _, r := range cfg.SNIRoutes {
		if r.Name == "bot" {
			t.Fatal("bot route should have been dropped when BOT_DOMAIN is empty")
		}
	}
}

func TestLoadConfig_KeepsBotWhenSet(t *testing.T) {
	data, err := os.ReadFile("../routes.yaml")
	if err != nil {
		t.Skipf("routes.yaml not found: %v", err)
	}
	cfg, err := LoadConfig(data, envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
		"BOT_DOMAIN":   "bot.example.com",
	}))
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	var bot *Route
	for i := range cfg.SNIRoutes {
		if cfg.SNIRoutes[i].Name == "bot" {
			bot = &cfg.SNIRoutes[i]
		}
	}
	if bot == nil {
		t.Fatal("bot route missing")
	}
	if bot.Match != "bot.example.com" || bot.Upstream != "backend:5000" {
		t.Fatalf("bot route not interpolated: %+v", bot)
	}
	if !bot.TLS || len(bot.OnlyPaths) != 1 || bot.OnlyPaths[0] != "/api/billing/yookassa/webhook" {
		t.Fatalf("bot route flags wrong: %+v", bot)
	}
}

func TestGenerate_BotServerPathFilters(t *testing.T) {
	data, err := os.ReadFile("../routes.yaml")
	if err != nil {
		t.Skipf("routes.yaml not found: %v", err)
	}
	cfg, err := LoadConfig(data, envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
		"BOT_DOMAIN":   "bot.example.com",
	}))
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	str := string(b)
	if !containsString(str, "/api/billing/yookassa/webhook") {
		t.Fatal("bot webhook path matcher missing")
	}
	if !containsString(str, "backend:5000") {
		t.Fatal("bot backend upstream missing")
	}
	if !containsString(str, "\"status_code\": 404") && !containsString(str, "\"status_code\":404") {
		t.Fatal("404 catch-all missing for bot server")
	}
}

func TestGenerate_DefaultRoutesFileValidJSON(t *testing.T) {
	data, err := os.ReadFile("../routes.yaml")
	if err != nil {
		t.Skipf("routes.yaml not found: %v", err)
	}
	cfg, err := LoadConfig(data, envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
	}))
	if err != nil {
		t.Fatalf("LoadConfig real routes.yaml: %v", err)
	}
	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	jsonValid(t, b)
}

const routesYAMLWithAPI = `
sni_routes:
  - name: panel
    match: "${PANEL_DOMAIN}"
    upstream: "frontend:80"
    tls: true
    api_path: "/${PANEL_SECRET_PATH}/api/"
    api_upstream: "backend:5000"
`

func TestGenerate_PanelAPIRoute(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAMLWithAPI), envMap(map[string]string{
		"PANEL_DOMAIN":      "panel.example.com",
		"PANEL_SECRET_PATH": "s3cret",
	}))
	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	str := string(b)
	if !containsString(str, "/s3cret/api/*") {
		t.Fatal("panel API path matcher missing")
	}
	if !containsString(str, "strip_path_prefix") || !containsString(str, "/s3cret") {
		t.Fatal("strip_path_prefix for secret prefix missing")
	}
	if !containsString(str, "backend:5000") {
		t.Fatal("panel API backend upstream missing")
	}
	if !containsString(str, "frontend:80") {
		t.Fatal("panel SPA upstream missing")
	}
}

func TestGenerate_PanelAPIRouteSkippedWithoutSecret(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAMLWithAPI), envMap(map[string]string{
		"PANEL_DOMAIN": "panel.example.com",
	}))
	b, _ := Generate(cfg)
	if containsString(string(b), "backend:5000") {
		t.Fatal("API route must be skipped when PANEL_SECRET_PATH is empty")
	}
}

func containsString(haystack, needle string) bool {
	return len(haystack) >= len(needle) && (indexOf(haystack, needle) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func TestSecurityHeadersAllowNoExternalFontHosts(t *testing.T) {
	headers := securityHeaders()
	response := headers["response"].(map[string]any)
	set := response["set"].(map[string]any)
	csp := set["Content-Security-Policy"].([]any)[0].(string)

	for _, host := range []string{"fonts.googleapis.com", "fonts.gstatic.com"} {
		if strings.Contains(csp, host) {
			t.Fatalf("CSP still allows %s; fonts are self-hosted from ui-core/src/fonts: %s", host, csp)
		}
	}
	for _, pinned := range []struct {
		directive string
		why       string
	}{
		{
			"style-src 'self' 'unsafe-inline'",
			"Tailwind emits an inline <style> block and the SPAs render inline style attributes; " +
				"without 'unsafe-inline' every page loads unstyled.",
		},
		{
			"font-src 'self' data:",
			"the Roboto subsets are served out of the bundle itself since the fonts were self-hosted; " +
				"losing 'self' silently drops every page to the fallback system font.",
		},
		{
			"script-src 'self'",
			"this is the directive that blocks inline <script>, and three separate decisions rest on it " +
				"with nothing else holding them up. frontend/entrypoint.sh ships the panel role in a " +
				"<meta name=\"panel-role\"> tag rather than an inline script precisely because this header " +
				"killed the script form. backend/tests/test_frontend_html_shell.py forbids inline scripts " +
				"in every nginx-served shell and cites this directive as the entire reason. And the " +
				"server-rendered subscription page was retired in part because its inline copy button was " +
				"dead on any deployment behind this proxy. Drop the directive and all three become " +
				"pointless ceremony while inline script execution quietly comes back on an unauthenticated " +
				"page that renders admin-controlled brand, node and inbound-tag strings.",
		},
	} {
		if !strings.Contains(csp, pinned.directive) {
			t.Fatalf("CSP lost %q: %s\n\n%s", pinned.directive, csp, pinned.why)
		}
	}
}

func tlsAutomationPolicies(t *testing.T, root map[string]any) []map[string]any {
	t.Helper()
	apps := root["apps"].(map[string]any)
	tlsApp, ok := apps["tls"].(map[string]any)
	if !ok {
		t.Fatal("the generated config carries no tls app at all")
	}
	automation, ok := tlsApp["automation"].(map[string]any)
	if !ok {
		t.Fatalf("tls app carries no automation block, so nothing issues certificates: %v", tlsApp)
	}
	raw, ok := automation["policies"].([]any)
	if !ok {
		t.Fatalf("automation carries no policies: %v", automation)
	}
	out := make([]map[string]any, 0, len(raw))
	for _, p := range raw {
		out = append(out, p.(map[string]any))
	}
	return out
}

func policySubjects(policies []map[string]any) []string {
	var subjects []string
	for _, p := range policies {
		raw, _ := p["subjects"].([]any)
		for _, s := range raw {
			subjects = append(subjects, s.(string))
		}
	}
	return subjects
}

func policyIssuers(policies []map[string]any) []map[string]any {
	var issuers []map[string]any
	for _, p := range policies {
		raw, _ := p["issuers"].([]any)
		for _, i := range raw {
			issuers = append(issuers, i.(map[string]any))
		}
	}
	return issuers
}

func TestGenerate_OnlyTerminatedRoutesGetACertificate(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "www.google.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
	}))
	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	subjects := policySubjects(tlsAutomationPolicies(t, jsonValid(t, b)))

	for _, want := range []string{"panel.example.com", "sub.example.com"} {
		found := false
		for _, got := range subjects {
			if got == want {
				found = true
			}
		}
		if !found {
			t.Fatalf("%q terminates TLS but is not an ACME subject; subjects=%v", want, subjects)
		}
	}
	for _, got := range subjects {
		if got == "www.google.com" {
			t.Fatalf("the decoy domain became an ACME subject; subjects=%v", subjects)
		}
	}
}

func TestGenerate_NoCertificateFilesAreLoaded(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "www.google.com",
		"PANEL_DOMAIN": "panel.example.com",
	}))
	b, _ := Generate(cfg)
	for _, forbidden := range []string{"load_files", "/root/cert"} {
		if containsString(string(b), forbidden) {
			t.Fatalf("generated config still references %q", forbidden)
		}
	}
}

func TestGenerate_KeepsAServerOnPort80ForTheChallenge(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "www.google.com",
		"PANEL_DOMAIN": "panel.example.com",
	}))
	b, _ := Generate(cfg)
	root := jsonValid(t, b)
	servers := root["apps"].(map[string]any)["http"].(map[string]any)["servers"].(map[string]any)
	redirect, ok := servers["http_redirect"].(map[string]any)
	if !ok {
		t.Fatal("no http_redirect server: nothing holds :80, so no HTTP-01 challenge can be answered")
	}
	listen := redirect["listen"].([]any)
	if len(listen) != 1 || listen[0] != ":80" {
		t.Fatalf("the redirect server no longer listens on :80: %v", listen)
	}
}

func TestGenerate_LocalHostnamesUseTheInternalIssuer(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "www.google.com",
		"PANEL_DOMAIN": "panel.local",
	}))
	b, _ := Generate(cfg)
	issuers := policyIssuers(tlsAutomationPolicies(t, jsonValid(t, b)))
	if len(issuers) == 0 {
		t.Fatal("no issuers at all")
	}
	for _, issuer := range issuers {
		if issuer["module"] != "internal" {
			t.Fatalf("panel.local must use the internal issuer, got %v", issuer)
		}
	}
}

func TestGenerate_ACMEEmailReachesTheIssuer(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "www.google.com",
		"PANEL_DOMAIN": "panel.example.com",
		"ACME_EMAIL":   "ops@example.com",
	}))
	b, _ := Generate(cfg)
	issuers := policyIssuers(tlsAutomationPolicies(t, jsonValid(t, b)))
	for _, issuer := range issuers {
		if issuer["email"] != "ops@example.com" {
			t.Fatalf("ACME_EMAIL did not reach the issuer: %v", issuer)
		}
	}
}

func TestGenerate_ACMECAOverrideReachesTheIssuer(t *testing.T) {
	staging := "https://acme-staging-v02.api.letsencrypt.org/directory"
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "www.google.com",
		"PANEL_DOMAIN": "panel.example.com",
		"ACME_CA":      staging,
	}))
	b, _ := Generate(cfg)
	issuers := policyIssuers(tlsAutomationPolicies(t, jsonValid(t, b)))
	for _, issuer := range issuers {
		if issuer["ca"] != staging {
			t.Fatalf("ACME_CA did not reach the issuer: %v", issuer)
		}
	}
}

const botRoutesYAML = `
sni_routes:
  - name: bot
    match: "${BOT_DOMAIN}"
    upstream: "backend:5000"
    tls: true
    strip_prefix: "/${BOT_WEBHOOK_PATH}"
    only_paths:
      - "/${BOT_WEBHOOK_PATH}/api/billing/yookassa/webhook"
`

func botServerRoutes(t *testing.T, env map[string]string) []any {
	t.Helper()
	cfg, err := LoadConfig([]byte(botRoutesYAML), envMap(env))
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	servers := jsonValid(t, b)["apps"].(map[string]any)["http"].(map[string]any)["servers"].(map[string]any)
	server, ok := servers["bot_security_layer"].(map[string]any)
	if !ok {
		t.Fatalf("no bot_security_layer server; got %v", servers)
	}
	return server["routes"].([]any)
}

func TestGenerate_BotWebhookLivesUnderTheSecretPath(t *testing.T) {
	routes := botServerRoutes(t, map[string]string{
		"BOT_DOMAIN":       "bot.example.com",
		"BOT_WEBHOOK_PATH": "s3cr3t",
	})

	found := false
	for _, raw := range routes {
		route := raw.(map[string]any)
		match, ok := route["match"].([]any)
		if !ok {
			continue
		}
		paths := match[0].(map[string]any)["path"].([]any)
		if paths[0] == "/s3cr3t/api/billing/yookassa/webhook*" {
			found = true
		}
	}
	if !found {
		t.Fatalf("no route matches the webhook under the secret path; routes=%v", routes)
	}
}

func TestGenerate_BotSecretPathIsStrippedBeforeProxying(t *testing.T) {
	routes := botServerRoutes(t, map[string]string{
		"BOT_DOMAIN":       "bot.example.com",
		"BOT_WEBHOOK_PATH": "s3cr3t",
	})

	stripped := ""
	for _, raw := range routes {
		route := raw.(map[string]any)
		handlers, ok := route["handle"].([]any)
		if !ok {
			continue
		}
		for _, h := range handlers {
			handler := h.(map[string]any)
			if handler["handler"] == "rewrite" {
				stripped, _ = handler["strip_path_prefix"].(string)
			}
		}
	}
	if stripped != "/s3cr3t" {
		t.Fatalf("the secret prefix is not stripped before proxying (got %q)", stripped)
	}
}

func TestGenerate_BotHostAnswers404Elsewhere(t *testing.T) {
	routes := botServerRoutes(t, map[string]string{
		"BOT_DOMAIN":       "bot.example.com",
		"BOT_WEBHOOK_PATH": "s3cr3t",
	})

	last := routes[len(routes)-1].(map[string]any)
	handlers := last["handle"].([]any)
	final := handlers[len(handlers)-1].(map[string]any)
	if final["handler"] != "static_response" || fmt.Sprint(final["status_code"]) != "404" {
		t.Fatalf("the bot server does not end in a 404 catch-all: %v", final)
	}
}
