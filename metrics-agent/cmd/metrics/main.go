package main

import (
	"log"
	"net/http"
	"os"
	"time"

	"github.com/itg/metrics-agent/internal/api"
	"github.com/itg/metrics-agent/internal/collect"
	"github.com/itg/metrics-agent/internal/config"
	"github.com/itg/metrics-agent/internal/store"
	"github.com/itg/metrics-agent/internal/xray"
)

func main() {
	cfg := config.Load(os.LookupEnv)
	st, err := store.Open(cfg.DBPath)
	if err != nil {
		log.Fatalf("store: %v", err)
	}
	defer st.Close()

	smp := &collect.Sampler{
		Store:  st,
		Proc:   &collect.Proc{Root: os.DirFS(cfg.ProcPath)},
		Cgroup: &collect.Cgroup{Root: os.DirFS(cfg.SysPath + "/fs/cgroup")},
		Procs:  &collect.Procs{Root: os.DirFS(cfg.ProcPath), PageSize: int64(os.Getpagesize())},
	}
	vpn := &collect.VPNSampler{Store: st, Xray: xray.New(cfg.XrayAPIAddr)}

	go every(1*time.Second, func() { logErr("net", smp.SampleNet(now())) })
	go every(2*time.Second, func() { logErr("host", smp.SampleHost(now())) })
	go every(2*time.Second, func() { logErr("cpu", smp.SampleCPU(now())) })
	go every(5*time.Second, func() { logErr("ctr", smp.SampleContainers(now())) })
	go every(5*time.Second, func() { logErr("vpn", vpn.Sample(now())) })
	go every(15*time.Second, func() { logErr("disk", smp.SampleDisk(now())) })
	go every(15*time.Second, func() { logErr("procs", smp.SampleProcs(now())) })

	hotSec := int64(cfg.HotWindow / time.Second)
	go every(60*time.Second, func() {
		n := now()
		logErr("rollup1m", st.BuildRollup1m(n-120, n))
	})
	go every(time.Hour, func() {
		n := now()
		logErr("rollup1h", st.BuildRollup1h(n-7200, n))
	})
	go every(10*time.Minute, func() {
		logErr("compact", st.Compact(now()-hotSec))
	})
	go every(time.Hour, func() {
		n := now()
		logErr("prune", st.Prune(n-30*86400, n-7*86400))
	})

	srv := api.New(st)
	httpSrv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("metrics agent listening on %s", cfg.Listen)
	log.Fatal(httpSrv.ListenAndServe())
}

func now() int64 { return time.Now().Unix() }

func every(d time.Duration, fn func()) {
	t := time.NewTicker(d)
	defer t.Stop()
	for range t.C {
		fn()
	}
}

func logErr(name string, err error) {
	if err != nil {
		log.Printf("%s: %v", name, err)
	}
}
