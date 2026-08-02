package main

import (
	"encoding/json"
	"fmt"
	"net"
	"strings"
)

func isLocalHostname(host string) bool {
	if host == "" {
		return false
	}
	if net.ParseIP(host) != nil {
		return true
	}
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return true
	}
	if strings.HasSuffix(host, ".local") {
		return true
	}
	return !strings.Contains(host, ".")
}

func acmeIssuer(cfg *Config) map[string]any {
	issuer := map[string]any{"module": "acme"}
	if cfg.ACMEEmail != "" {
		issuer["email"] = cfg.ACMEEmail
	}
	if cfg.ACMECA != "" {
		issuer["ca"] = cfg.ACMECA
	}
	return issuer
}

func tlsAutomation(cfg *Config) map[string]any {
	var public, internal []any
	for _, r := range cfg.SNIRoutes {
		if !r.TLS {
			continue
		}
		if isLocalHostname(r.Match) {
			internal = append(internal, r.Match)
		} else {
			public = append(public, r.Match)
		}
	}
	policies := []any{}
	if len(public) > 0 {
		policies = append(policies, map[string]any{
			"subjects": public,
			"issuers":  []any{acmeIssuer(cfg)},
		})
	}
	if len(internal) > 0 {
		policies = append(policies, map[string]any{
			"subjects": internal,
			"issuers":  []any{map[string]any{"module": "internal"}},
		})
	}
	return map[string]any{"policies": policies}
}

func tlsApp(cfg *Config) map[string]any {
	app := map[string]any{"automation": tlsAutomation(cfg)}
	var automate []any
	for _, r := range cfg.SNIRoutes {
		if r.TLS {
			automate = append(automate, r.Match)
		}
	}
	if len(automate) > 0 {
		app["certificates"] = map[string]any{"automate": automate}
	}
	return app
}

func securityHeaders() map[string]any {
	return map[string]any{
		"handler": "headers",
		"response": map[string]any{
			"set": map[string]any{
				"Strict-Transport-Security": []any{"max-age=63072000; includeSubDomains; preload"},
				"X-XSS-Protection":          []any{"1; mode=block"},
				"X-Content-Type-Options":    []any{"nosniff"},
				"X-Frame-Options":           []any{"SAMEORIGIN"},
				"Referrer-Policy":           []any{"strict-origin-when-cross-origin"},
				"Content-Security-Policy": []any{
					"default-src 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'; " +
						"script-src 'self'; style-src 'self' 'unsafe-inline'; " +
						"font-src 'self' data:; img-src 'self' https: data: blob:; " +
						"connect-src 'self' https: wss:;",
				},
			},
		},
	}
}

func reverseProxy(upstream string) map[string]any {
	return map[string]any{
		"handler":   "reverse_proxy",
		"upstreams": []any{map[string]any{"dial": upstream}},
		"headers": map[string]any{
			"request": map[string]any{
				"set": map[string]any{
					"X-Forwarded-Proto": []any{"https"},
				},
			},
		},
	}
}

func httpServer(listen string, upstream string, onlyPaths []string, apiPath, apiUpstream, stripPrefix string) map[string]any {
	var routes []any
	if apiUpstream != "" && apiPath != "" {
		apiStrip := strings.TrimSuffix(apiPath, "/api/")
		if apiStrip != "" && apiStrip != "/" {
			routes = append(routes, map[string]any{
				"match": []any{map[string]any{"path": []any{apiPath + "*"}}},
				"handle": []any{
					securityHeaders(),
					map[string]any{"handler": "rewrite", "strip_path_prefix": apiStrip},
					reverseProxy(apiUpstream),
				},
			})
		}
	}
	if len(onlyPaths) > 0 {
		globs := make([]any, 0, len(onlyPaths))
		for _, p := range onlyPaths {
			globs = append(globs, p+"*")
		}

		handlers := []any{securityHeaders()}
		if stripPrefix != "" && stripPrefix != "/" {
			handlers = append(handlers, map[string]any{
				"handler":           "rewrite",
				"strip_path_prefix": stripPrefix,
			})
		}
		handlers = append(handlers, reverseProxy(upstream))
		routes = append(routes, map[string]any{
			"match":  []any{map[string]any{"path": globs}},
			"handle": handlers,
		})
		routes = append(routes, map[string]any{
			"handle": []any{map[string]any{"handler": "static_response", "status_code": 404}},
		})
	} else {
		routes = append(routes, map[string]any{
			"handle": []any{securityHeaders(), reverseProxy(upstream)},
		})
	}
	return map[string]any{
		"listen": []any{listen},
		"listener_wrappers": []any{
			map[string]any{"wrapper": "proxy_protocol", "allow": []any{"127.0.0.1/32"}},
		},
		"routes": routes,
	}
}

func httpRedirectServer() map[string]any {
	return map[string]any{
		"listen":          []any{":80"},
		"automatic_https": map[string]any{"disable": true},
		"routes": []any{
			map[string]any{
				"handle": []any{
					map[string]any{
						"handler":     "static_response",
						"status_code": 308,
						"headers": map[string]any{
							"Location": []any{"https://{http.request.host}{http.request.uri}"},
						},
					},
				},
			},
		},
	}
}

func layer4Passthrough(sni, upstream string) map[string]any {
	return map[string]any{
		"match": []any{map[string]any{"tls": map[string]any{"sni": []any{sni}}}},
		"handle": []any{map[string]any{
			"handler":        "proxy",
			"proxy_protocol": "v2",
			"upstreams":      []any{map[string]any{"dial": []any{upstream}}},
		}},
	}
}

func layer4TLS(sni, loopback string) map[string]any {
	return map[string]any{
		"match": []any{map[string]any{"tls": map[string]any{"sni": []any{sni}}}},
		"handle": []any{
			map[string]any{
				"handler": "tls",
				"connection_policies": []any{map[string]any{
					"alpn":         []any{"http/1.1"},
					"protocol_min": "tls1.2",
					"protocol_max": "tls1.3",
				}},
			},
			map[string]any{
				"handler":        "proxy",
				"proxy_protocol": "v2",
				"upstreams":      []any{map[string]any{"dial": []any{loopback}}},
			},
		},
	}
}

func Generate(cfg *Config) ([]byte, error) {
	httpServers := map[string]any{}
	var l4routes []any
	port := 8081

	for _, r := range cfg.SNIRoutes {
		if !r.TLS {
			l4routes = append(l4routes, layer4Passthrough(r.Match, r.Upstream))
			continue
		}
		loopback := fmt.Sprintf("127.0.0.1:%d", port)
		name := r.Name
		if name == "" {
			name = fmt.Sprintf("srv%d", port)
		}
		httpServers[name+"_security_layer"] = httpServer(loopback, r.Upstream, r.OnlyPaths, r.APIPath, r.APIUpstream, r.StripPrefix)
		l4routes = append(l4routes, layer4TLS(r.Match, loopback))
		port++
	}

	httpServers["http_redirect"] = httpRedirectServer()

	root := map[string]any{
		"apps": map[string]any{
			"tls": tlsApp(cfg),
			"http": map[string]any{
				"servers": httpServers,
			},
			"layer4": map[string]any{
				"servers": map[string]any{
					"main": map[string]any{
						"listen": []any{":443"},
						"routes": l4routes,
					},
				},
			},
		},
	}
	return json.MarshalIndent(root, "", "\t")
}
